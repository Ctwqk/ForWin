from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from hashlib import sha1
from pathlib import Path
from typing import Any
from uuid import UUID

from forwin.config import DEFAULT_QDRANT_URL
from forwin.llm_kb.store import ROOT_FILE_KEYS
from forwin.retrieval.memory_index import (
    HashTextEmbedder,
    TextEmbedder,
    _create_qdrant_client,
    _qdrant_models,
)


LLM_KB_PROJECTION_VERSION = "llm_kb_v2"


@dataclass
class LLMKBVectorRecord:
    project_id: str
    file_key: str
    section_key: str
    role_scope: str
    text: str
    source_refs: list[str]
    source_digest: str
    index_kind: str = "llm_kb"
    as_of_chapter: int = 0
    projection_version: str = LLM_KB_PROJECTION_VERSION
    visibility_scope: str = "writer_safe"
    canon_status: str = "canon_projection"
    node_refs: list[str] = field(default_factory=list)
    edge_refs: list[str] = field(default_factory=list)
    fact_refs: list[str] = field(default_factory=list)
    map_refs: list[str] = field(default_factory=list)
    chapter_refs: list[str] = field(default_factory=list)
    score: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "index_kind": self.index_kind,
            "as_of_chapter": self.as_of_chapter,
            "projection_version": self.projection_version,
            "file_key": self.file_key,
            "section_key": self.section_key,
            "role_scope": self.role_scope,
            "visibility_scope": self.visibility_scope,
            "canon_status": self.canon_status,
            "node_refs": list(self.node_refs),
            "edge_refs": list(self.edge_refs),
            "fact_refs": list(self.fact_refs),
            "map_refs": list(self.map_refs),
            "chapter_refs": list(self.chapter_refs),
            "text": self.text,
            "source_refs": list(self.source_refs),
            "source_digest": self.source_digest,
            "score": self.score,
        }


def _default_qdrant_url() -> str:
    return os.environ.get("FORWIN_QDRANT_URL", DEFAULT_QDRANT_URL)


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

    def _project_filter(
        self,
        project_id: str,
        *,
        roles: set[str] | None = None,
        visibility_scopes: set[str] | None = None,
        as_of_chapter: int | None = None,
        index_kind: str = "llm_kb",
    ) -> Any:
        must = [
            self._rest.FieldCondition(
                key="project_id",
                match=self._rest.MatchValue(value=project_id),
            ),
            self._rest.FieldCondition(
                key="index_kind",
                match=self._rest.MatchValue(value=index_kind),
            ),
        ]
        if roles is not None:
            must.append(
                self._rest.FieldCondition(
                    key="role_scope",
                    match=self._rest.MatchAny(any=sorted(roles)),
                )
            )
        if visibility_scopes is not None:
            must.append(
                self._rest.FieldCondition(
                    key="visibility_scope",
                    match=self._rest.MatchAny(any=sorted(visibility_scopes)),
                )
            )
        if as_of_chapter is not None and int(as_of_chapter) > 0:
            must.append(
                self._rest.FieldCondition(
                    key="as_of_chapter",
                    match=self._rest.MatchValue(value=int(as_of_chapter)),
                )
            )
        return self._rest.Filter(must=must)

    def rebuild_project(
        self,
        project_id: str,
        *,
        source_digest: str = "",
        as_of_chapter: int = 0,
        projection_version: str = LLM_KB_PROJECTION_VERSION,
    ) -> dict[str, Any]:
        project_root = self.root / project_id
        sections = _collect_project_sections(
            project_root,
            source_digest=source_digest,
            as_of_chapter=as_of_chapter,
            projection_version=projection_version,
        )
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
                        "index_kind": section["index_kind"],
                        "as_of_chapter": section["as_of_chapter"],
                        "projection_version": section["projection_version"],
                        "file_key": section["file_key"],
                        "section_key": section["section_key"],
                        "role_scope": section["role_scope"],
                        "visibility_scope": section["visibility_scope"],
                        "canon_status": section["canon_status"],
                        "node_refs": section["node_refs"],
                        "edge_refs": section["edge_refs"],
                        "fact_refs": section["fact_refs"],
                        "map_refs": section["map_refs"],
                        "chapter_refs": section["chapter_refs"],
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
        as_of_chapter: int | None = None,
        visibility_scope: str | None = None,
    ) -> list[LLMKBVectorRecord]:
        query_text = str(query or "").strip()
        if not query_text:
            return []
        limit_value = max(1, int(limit or 5))
        allowed_visibility = _allowed_visibility_scopes(role)
        requested_visibility = str(visibility_scope or "").strip()
        if requested_visibility:
            allowed_visibility = allowed_visibility.intersection({requested_visibility})
            if not allowed_visibility:
                return []
        response = self.client.query_points(
            collection_name=self.collection_name,
            query=self.embedder.embed([query_text])[0],
            query_filter=self._project_filter(
                project_id,
                roles=_allowed_role_scopes(role),
                visibility_scopes=allowed_visibility,
                as_of_chapter=as_of_chapter,
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
                    index_kind=str(payload.get("index_kind") or "llm_kb"),
                    as_of_chapter=int(payload.get("as_of_chapter") or 0),
                    projection_version=str(payload.get("projection_version") or LLM_KB_PROJECTION_VERSION),
                    visibility_scope=str(payload.get("visibility_scope") or "writer_safe"),
                    canon_status=str(payload.get("canon_status") or "canon_projection"),
                    node_refs=_source_refs(payload.get("node_refs")),
                    edge_refs=_source_refs(payload.get("edge_refs")),
                    fact_refs=_source_refs(payload.get("fact_refs")),
                    map_refs=_source_refs(payload.get("map_refs")),
                    chapter_refs=_source_refs(payload.get("chapter_refs")),
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


def _collect_project_sections(
    project_root: Path,
    *,
    source_digest: str,
    as_of_chapter: int,
    projection_version: str,
) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    if not project_root.exists():
        return sections
    for file_key in sorted(ROOT_FILE_KEYS - {"retrieval_index.json"}):
        path = project_root / file_key
        if not path.exists() or not path.is_file():
            continue
        if file_key.endswith(".jsonl"):
            sections.extend(
                _jsonl_sections(
                    file_key,
                    path,
                    source_digest=source_digest,
                    as_of_chapter=as_of_chapter,
                    projection_version=projection_version,
                )
            )
        else:
            sections.extend(
                _markdown_sections(
                    file_key,
                    path,
                    source_digest=source_digest,
                    as_of_chapter=as_of_chapter,
                    projection_version=projection_version,
                )
            )
    packs_root = project_root / "packs"
    for role in ("reviewer", "planner", "compiler"):
        path = packs_root / role / "context.json"
        if path.exists() and path.is_file():
            raw_text = path.read_text(encoding="utf-8")
            text = _trim(raw_text, 6000)
            sections.append(
                _section_record(
                    file_key=f"packs/{role}/context.json",
                    section_key="context",
                    role_scope=role,
                    text=text,
                    source_refs=[f"llm_kb:pack:{role}"],
                    source_digest=source_digest,
                    as_of_chapter=as_of_chapter,
                    projection_version=projection_version,
                )
            )
            sections.extend(
                _active_personality_sections(
                    role,
                    raw_text,
                    source_digest=source_digest,
                    as_of_chapter=as_of_chapter,
                    projection_version=projection_version,
                )
            )
    return [section for section in sections if section["text"].strip()]


def _markdown_sections(
    file_key: str,
    path: Path,
    *,
    source_digest: str,
    as_of_chapter: int,
    projection_version: str,
) -> list[dict[str, Any]]:
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
        _section_record(
            file_key=file_key,
            section_key=key,
            role_scope=_role_scope_for_file(file_key),
            text=_trim(chunk, 3000),
            source_refs=[f"llm_kb:{file_key}#{key}"],
            source_digest=source_digest,
            as_of_chapter=as_of_chapter,
            projection_version=projection_version,
        )
        for key, chunk in chunks
        if chunk.strip()
    ]


def _jsonl_sections(
    file_key: str,
    path: Path,
    *,
    source_digest: str,
    as_of_chapter: int,
    projection_version: str,
) -> list[dict[str, Any]]:
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
            _section_record(
                file_key=file_key,
                section_key=str(payload.get("id") or index) if isinstance(payload, dict) else str(index),
                role_scope=_role_scope_for_file(file_key),
                text=_trim(text, 2400),
                source_refs=[f"llm_kb:{file_key}:{index}"],
                source_digest=source_digest,
                as_of_chapter=as_of_chapter,
                projection_version=projection_version,
                raw_payload=payload if isinstance(payload, dict) else None,
            )
        )
    return sections


def _active_personality_sections(
    role: str,
    raw_text: str,
    *,
    source_digest: str,
    as_of_chapter: int,
    projection_version: str,
) -> list[dict[str, Any]]:
    try:
        payload = json.loads(raw_text or "{}")
    except json.JSONDecodeError:
        return []
    contexts = payload.get("active_personality_contexts") if isinstance(payload, dict) else None
    if not isinstance(contexts, list):
        return []
    sections: list[dict[str, Any]] = []
    for index, context in enumerate(contexts, start=1):
        if not isinstance(context, dict):
            continue
        character_id = str(context.get("character_id") or index)
        text = json.dumps(
            {
                "character_id": context.get("character_id", ""),
                "character_name": context.get("character_name", ""),
                "active_skills": context.get("active_skills", {}),
                "current_behavior_bias": context.get("current_behavior_bias", {}),
                "constraints": context.get("constraints", []),
                "source_refs": context.get("source_refs", []),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        sections.append(
            _section_record(
                file_key=f"packs/{role}/active_personality_context.json",
                section_key=character_id,
                role_scope=role,
                text=_trim(text, 2400),
                source_refs=[f"llm_kb:pack:{role}:active_personality_context:{character_id}"],
                source_digest=source_digest,
                as_of_chapter=as_of_chapter,
                projection_version=projection_version,
            )
        )
    return sections


def _section_record(
    *,
    file_key: str,
    section_key: str,
    role_scope: str,
    text: str,
    source_refs: list[str],
    source_digest: str,
    as_of_chapter: int,
    projection_version: str,
    raw_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    refs = _reference_fields(file_key, text, source_refs, raw_payload=raw_payload)
    return {
        "project_id": "",
        "index_kind": "llm_kb",
        "file_key": file_key,
        "section_key": section_key,
        "role_scope": role_scope,
        "visibility_scope": _visibility_scope_for_role(role_scope),
        "canon_status": "canon_projection",
        "text": text,
        "source_refs": source_refs,
        "source_digest": source_digest,
        "as_of_chapter": int(as_of_chapter or 0),
        "projection_version": projection_version or LLM_KB_PROJECTION_VERSION,
        **refs,
    }


def _reference_fields(
    file_key: str,
    text: str,
    source_refs: list[str],
    *,
    raw_payload: dict[str, Any] | None = None,
) -> dict[str, list[str]]:
    blob = "\n".join([text, *source_refs])
    node_refs = set(_extract_ref_ids(blob, "node"))
    edge_refs = set(_extract_ref_ids(blob, "edge"))
    fact_refs = set(_extract_ref_ids(blob, "fact"))
    map_refs = set(_extract_map_refs(blob))
    chapter_refs = set(re.findall(r"\bchapter:\d+\b", blob))
    if raw_payload:
        item_id = str(raw_payload.get("id") or "").strip()
        if item_id:
            if file_key == "facts.jsonl":
                fact_refs.add(item_id)
            elif file_key == "graph_deltas.jsonl":
                target_type = str(raw_payload.get("target_type") or "").strip()
                target_id = str(raw_payload.get("target_id") or "").strip()
                if target_type == "node" and target_id:
                    node_refs.add(target_id)
                elif target_type == "edge" and target_id:
                    edge_refs.add(target_id)
            else:
                node_refs.add(item_id)
        raw_chapter = raw_payload.get("as_of_chapter") or raw_payload.get("chapter_number")
        if raw_chapter:
            chapter_refs.add(f"chapter:{int(raw_chapter)}")
    return {
        "node_refs": sorted(node_refs),
        "edge_refs": sorted(edge_refs),
        "fact_refs": sorted(fact_refs),
        "map_refs": sorted(map_refs),
        "chapter_refs": sorted(chapter_refs),
    }


def _extract_ref_ids(blob: str, ref_type: str) -> list[str]:
    return re.findall(rf"(?:book_state:{ref_type}:|{ref_type}:)([A-Za-z0-9_.-]+)", blob)


def _extract_map_refs(blob: str) -> list[str]:
    refs = re.findall(r"(?:book_state:(map_node|map_edge):|(map_node|map_edge):)([A-Za-z0-9_.-]+)", blob)
    return [f"{left or right}:{item_id}" for left, right, item_id in refs]


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


def _visibility_scope_for_role(role_scope: str) -> str:
    normalized = str(role_scope or "writer").strip().lower()
    if normalized == "reviewer":
        return "reviewer_only"
    if normalized == "planner":
        return "planner_only"
    if normalized == "compiler":
        return "compiler_only"
    return "writer_safe"


def _allowed_visibility_scopes(role: str) -> set[str]:
    normalized = str(role or "writer").strip().lower()
    if normalized == "reviewer":
        return {"writer_safe", "reviewer_only"}
    if normalized == "planner":
        return {"writer_safe", "planner_only"}
    if normalized == "compiler":
        return {"writer_safe", "compiler_only"}
    return {"writer_safe"}


def _trim(text: str, limit: int) -> str:
    normalized = str(text or "").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)].rstrip() + "..."
