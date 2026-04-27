from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forwin.llm_kb.store import ROOT_FILE_KEYS
from forwin.retrieval.memory_index import HashTextEmbedder, TextEmbedder, _cosine_similarity


@dataclass
class LLMKBVectorRecord:
    project_id: str
    file_key: str
    section_key: str
    role_scope: str
    text: str
    source_refs: list[str]
    source_digest: str
    score: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "file_key": self.file_key,
            "section_key": self.section_key,
            "role_scope": self.role_scope,
            "text": self.text,
            "source_refs": list(self.source_refs),
            "source_digest": self.source_digest,
            "score": self.score,
        }


class LLMKBVectorIndex:
    def __init__(self, root: Path, *, embedder: TextEmbedder | None = None) -> None:
        self.root = root
        self.index_path = root / "_index" / "llm_kb_vectors.sqlite3"
        self.embedder = embedder or HashTextEmbedder(dims=96)
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def rebuild_project(self, project_id: str, *, source_digest: str = "") -> dict[str, Any]:
        project_root = self.root / project_id
        sections = _collect_project_sections(project_root, source_digest=source_digest)
        texts = [section["text"] for section in sections]
        embeddings = self.embedder.embed(texts) if texts else []
        with sqlite3.connect(self.index_path) as conn:
            conn.execute("DELETE FROM llm_kb_vectors WHERE project_id = ?", (project_id,))
            conn.executemany(
                """
                INSERT INTO llm_kb_vectors(
                    project_id, file_key, section_key, role_scope, text,
                    source_refs_json, source_digest, embedding_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                [
                    (
                        project_id,
                        section["file_key"],
                        section["section_key"],
                        section["role_scope"],
                        section["text"],
                        json.dumps(section["source_refs"], ensure_ascii=False),
                        section["source_digest"],
                        json.dumps(embedding),
                    )
                    for section, embedding in zip(sections, embeddings)
                ],
            )
            conn.commit()
        return {
            "index_path": str(self.index_path),
            "section_count": len(sections),
            "dims": self.embedder.dims,
        }

    def search(
        self,
        project_id: str,
        query: str,
        *,
        role: str = "writer",
        limit: int = 5,
    ) -> list[LLMKBVectorRecord]:
        query_text = str(query or "").strip()
        if not query_text:
            return []
        allowed_roles = _allowed_role_scopes(role)
        query_embedding = self.embedder.embed([query_text])[0]
        with sqlite3.connect(self.index_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT project_id, file_key, section_key, role_scope, text,
                       source_refs_json, source_digest, embedding_json
                FROM llm_kb_vectors
                WHERE project_id = ? AND role_scope IN ({})
                """.format(",".join("?" for _ in allowed_roles)),
                (project_id, *sorted(allowed_roles)),
            ).fetchall()
        scored: list[LLMKBVectorRecord] = []
        for row in rows:
            embedding = json.loads(row["embedding_json"] or "[]")
            score = _cosine_similarity(query_embedding, embedding)
            scored.append(
                LLMKBVectorRecord(
                    project_id=row["project_id"],
                    file_key=row["file_key"],
                    section_key=row["section_key"],
                    role_scope=row["role_scope"],
                    text=_trim(row["text"], 900),
                    source_refs=json.loads(row["source_refs_json"] or "[]"),
                    source_digest=row["source_digest"],
                    score=score,
                )
            )
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[: max(1, int(limit or 5))]

    def _ensure_schema(self) -> None:
        with sqlite3.connect(self.index_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_kb_vectors(
                    project_id TEXT NOT NULL,
                    file_key TEXT NOT NULL,
                    section_key TEXT NOT NULL,
                    role_scope TEXT NOT NULL,
                    text TEXT NOT NULL,
                    source_refs_json TEXT NOT NULL DEFAULT '[]',
                    source_digest TEXT NOT NULL DEFAULT '',
                    embedding_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(project_id, file_key, section_key, role_scope)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_llm_kb_vectors_project_role "
                "ON llm_kb_vectors(project_id, role_scope)"
            )
            conn.commit()


def _collect_project_sections(project_root: Path, *, source_digest: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    if not project_root.exists():
        return sections
    for file_key in sorted(ROOT_FILE_KEYS - {"retrieval_index.json"}):
        path = project_root / file_key
        if not path.exists() or not path.is_file():
            continue
        if file_key.endswith(".jsonl"):
            sections.extend(_jsonl_sections(file_key, path, source_digest=source_digest))
        else:
            sections.extend(_markdown_sections(file_key, path, source_digest=source_digest))
    packs_root = project_root / "packs"
    for role in ("reviewer", "planner", "compiler"):
        path = packs_root / role / "context.json"
        if path.exists() and path.is_file():
            text = _trim(path.read_text(encoding="utf-8"), 6000)
            sections.append(
                {
                    "file_key": f"packs/{role}/context.json",
                    "section_key": "context",
                    "role_scope": role,
                    "text": text,
                    "source_refs": [f"llm_kb:pack:{role}"],
                    "source_digest": source_digest,
                }
            )
    return [section for section in sections if section["text"].strip()]


def _markdown_sections(file_key: str, path: Path, *, source_digest: str) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    source_refs = _extract_refs(text)
    chunks: list[tuple[str, str]] = []
    current_key = "header"
    current_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current_lines:
                chunks.append((current_key, "\n".join(current_lines).strip()))
            current_key = _slug(line[3:].strip()) or "section"
            current_lines = [line]
        else:
            current_lines.append(line)
    if current_lines:
        chunks.append((current_key, "\n".join(current_lines).strip()))
    return [
        {
            "file_key": file_key,
            "section_key": section_key,
            "role_scope": "writer",
            "text": body,
            "source_refs": source_refs or [f"llm_kb:file:{file_key}"],
            "source_digest": source_digest or _extract_digest(text),
        }
        for section_key, body in chunks
    ]


def _jsonl_sections(file_key: str, path: Path, *, source_digest: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            payload = {"raw": line}
        sections.append(
            {
                "file_key": file_key,
                "section_key": str(payload.get("id") or f"line_{index}"),
                "role_scope": "writer",
                "text": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                "source_refs": list(payload.get("source_refs") or [f"llm_kb:file:{file_key}:line:{index}"])
                if isinstance(payload, dict)
                else [f"llm_kb:file:{file_key}:line:{index}"],
                "source_digest": str(payload.get("source_digest") or source_digest) if isinstance(payload, dict) else source_digest,
            }
        )
    return sections


def _allowed_role_scopes(role: str) -> set[str]:
    role_key = str(role or "writer").strip().lower()
    if role_key == "compiler":
        return {"writer", "reviewer", "planner", "compiler"}
    if role_key in {"reviewer", "planner"}:
        return {"writer", role_key}
    return {"writer"}


def _extract_refs(text: str) -> list[str]:
    refs: list[str] = []
    in_refs = False
    for line in text.splitlines():
        if line.strip() == "source_refs:":
            in_refs = True
            continue
        if in_refs and line.startswith("- "):
            refs.append(line[2:].strip())
            continue
        if in_refs and line and not line.startswith(" "):
            break
    return refs


def _extract_digest(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("source_digest:"):
            return line.split(":", 1)[1].strip()
    return ""


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]+", "_", value).strip("_")
    return slug[:80] or "section"


def _trim(value: str, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."
