from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from hashlib import sha1
from pathlib import Path
from typing import Any
from uuid import UUID

from forwin.llm_kb.store import ROOT_FILE_KEYS
from forwin.retrieval.memory_index import (
    HashTextEmbedder,
    TextEmbedder,
    _create_qdrant_client,
    _qdrant_models,
)


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


def _default_qdrant_url() -> str:
    return os.environ.get("FORWIN_QDRANT_URL", "http://localhost:6333")


def _default_collection_name() -> str:
    return os.environ.get("FORWIN_LLM_KB_QDRANT_COLLECTION", "llm_kb_vectors")


def _point_id(project_id: str, file_key: str, section_key: str, role_scope: str) -> str:
    digest = sha1(
        f"{project_id}:{file_key}:{section_key}:{role_scope}".encode("utf-8")
    ).hexdigest()[:32]
    return str(UUID(digest))


class LLMKBVectorIndex:
    def __init__(
        self,
        root: Path,
        *,
        qdrant_url: str | None = None,
        collection_name: str | None = None,
        embedder: TextEmbedder | None = None,
        qdrant_client: Any | None = None,
        qdrant_models: Any | None = None,
    ) -> None:
        self.root = root
        self.collection_name = collection_name or _default_collection_name()
        self.embedder = embedder or HashTextEmbedder(dims=96)
        self._rest = qdrant_models or _qdrant_models()
        self.client = qdrant_client or _create_qdrant_client(qdrant_url or _default_qdrant_url())
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        collections = {item.name for item in self.client.get_collections().collections}
        if self.collection_name in collections:
            return
        self.client.create_collection(
            collection_name=self.collection_name,
            vectors_config=self._rest.VectorParams(
                size=self.embedder.dims,
                distance=self._rest.Distance.COSINE,
            ),
        )

    def _project_filter(self, project_id: str, *, roles: set[str] | None = None) -> Any:
        must = [
            self._rest.FieldCondition(
                key="project_id",
                match=self._rest.MatchValue(value=project_id),
            )
        ]
        if roles is not None:
            must.append(
                self._rest.FieldCondition(
                    key="role_scope",
                    match=self._rest.MatchAny(any=sorted(roles)),
                )
            )
        return self._rest.Filter(must=must)

    def rebuild_project(self, project_id: str, *, source_digest: str = "") -> dict[str, Any]:
        project_root = self.root / project_id
        sections = _collect_project_sections(project_root, source_digest=source_digest)
        texts = [section["text"] for section in sections]
        embeddings = self.embedder.embed(texts) if texts else []
        self.client.delete(
            collection_name=self.collection_name,
            points_selector=self._rest.FilterSelector(
                filter=self._project_filter(project_id),
            ),
            wait=True,
        )
        points = []
        for section, embedding in zip(sections, embeddings):
            points.append(
                self._rest.PointStruct(
                    id=_point_id(
                        project_id,
                        section["file_key"],
                        section["section_key"],
                        section["role_scope"],
                    ),
                    vector=embedding,
                    payload={
                        "project_id": project_id,
                        "file_key": section["file_key"],
                        "section_key": section["section_key"],
                        "role_scope": section["role_scope"],
                        "text": section["text"],
                        "source_refs": section["source_refs"],
                        "source_digest": section["source_digest"],
                    },
                )
            )
        if points:
            self.client.upsert(collection_name=self.collection_name, points=points)
        return {
            "backend": "qdrant",
            "collection": self.collection_name,
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
        limit_value = max(1, int(limit or 5))
        response = self.client.query_points(
            collection_name=self.collection_name,
            query=self.embedder.embed([query_text])[0],
            query_filter=self._project_filter(
                project_id,
                roles=_allowed_role_scopes(role),
            ),
            limit=limit_value,
        )
        points = getattr(response, "points", response)
        records: list[LLMKBVectorRecord] = []
        for point in points:
            payload = dict(getattr(point, "payload", {}) or {})
            records.append(
                LLMKBVectorRecord(
                    project_id=str(payload.get("project_id") or project_id),
                    file_key=str(payload.get("file_key") or ""),
                    section_key=str(payload.get("section_key") or ""),
                    role_scope=str(payload.get("role_scope") or "writer"),
                    text=_trim(str(payload.get("text") or ""), 900),
                    source_refs=_source_refs(payload.get("source_refs")),
                    source_digest=str(payload.get("source_digest") or ""),
                    score=float(getattr(point, "score", 0.0) or 0.0),
                )
            )
        return records


def _source_refs(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return [raw]
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    return []


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
    chunks: list[tuple[str, str]] = []
    current_key = "root"
    current_lines: list[str] = []
    for raw_line in text.splitlines():
        match = re.match(r"^(#{1,4})\s+(.+?)\s*$", raw_line)
        if match and current_lines:
            chunks.append((current_key, "\n".join(current_lines).strip()))
            current_lines = []
        if match:
            current_key = re.sub(r"[^A-Za-z0-9_.-]+", "-", match.group(2).strip()).strip("-")[:80] or "section"
        current_lines.append(raw_line)
    if current_lines:
        chunks.append((current_key, "\n".join(current_lines).strip()))
    return [
        {
            "file_key": file_key,
            "section_key": key,
            "role_scope": _role_scope_for_file(file_key),
            "text": _trim(chunk, 3000),
            "source_refs": [f"llm_kb:{file_key}#{key}"],
            "source_digest": source_digest,
        }
        for key, chunk in chunks
        if chunk.strip()
    ]


def _jsonl_sections(file_key: str, path: Path, *, source_digest: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for index, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw}
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        sections.append(
            {
                "file_key": file_key,
                "section_key": str(payload.get("id") or index) if isinstance(payload, dict) else str(index),
                "role_scope": _role_scope_for_file(file_key),
                "text": _trim(text, 2400),
                "source_refs": [f"llm_kb:{file_key}:{index}"],
                "source_digest": source_digest,
            }
        )
    return sections


def _role_scope_for_file(file_key: str) -> str:
    if "review" in file_key or "risk" in file_key:
        return "reviewer"
    if "plan" in file_key or "outline" in file_key:
        return "planner"
    return "writer"


def _allowed_role_scopes(role: str) -> set[str]:
    normalized = str(role or "writer").strip().lower()
    if normalized == "reviewer":
        return {"writer", "reviewer"}
    if normalized == "planner":
        return {"writer", "planner"}
    if normalized == "compiler":
        return {"writer", "compiler"}
    return {"writer"}


def _trim(text: str, limit: int) -> str:
    normalized = str(text or "").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)].rstrip() + "..."
