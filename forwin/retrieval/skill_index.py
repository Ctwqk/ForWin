from __future__ import annotations

import os
from dataclasses import dataclass
from hashlib import sha1
from pathlib import Path
from typing import Any
from uuid import UUID

from forwin.config import DEFAULT_QDRANT_URL
from forwin.retrieval.memory_index import (
    HashTextEmbedder,
    TextEmbedder,
    _create_qdrant_client,
    _qdrant_models,
)
from forwin.skills.loader import load_skill_manifest


@dataclass
class SkillVectorRecord:
    skill_id: str
    skill_type: str
    section_key: str
    role_scope: str
    text: str
    source_refs: list[str]
    score: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "index_kind": "skill",
            "skill_id": self.skill_id,
            "skill_type": self.skill_type,
            "section_key": self.section_key,
            "role_scope": self.role_scope,
            "text": self.text,
            "source_refs": list(self.source_refs),
            "score": self.score,
        }


def _default_qdrant_url() -> str:
    return os.environ.get("FORWIN_QDRANT_URL", DEFAULT_QDRANT_URL)


def _default_collection_name() -> str:
    return os.environ.get("FORWIN_SKILL_QDRANT_COLLECTION", "skill_vectors")


def _point_id(skill_id: str, section_key: str) -> str:
    digest = sha1(f"skill:{skill_id}:{section_key}".encode("utf-8")).hexdigest()[:32]
    return str(UUID(digest))


class SkillVectorIndex:
    def __init__(
        self,
        *,
        qdrant_url: str | None = None,
        collection_name: str | None = None,
        embedder: TextEmbedder | None = None,
        qdrant_client: Any | None = None,
        qdrant_models: Any | None = None,
    ) -> None:
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

    def rebuild(self, skill_root: Path) -> dict[str, Any]:
        records = _collect_skill_records(Path(skill_root))
        self.client.delete(
            collection_name=self.collection_name,
            points_selector=self._rest.FilterSelector(filter=self._index_filter()),
            wait=True,
        )
        embeddings = self.embedder.embed([record["text"] for record in records]) if records else []
        points = [
            self._rest.PointStruct(
                id=_point_id(record["skill_id"], record["section_key"]),
                vector=embedding,
                payload=record,
            )
            for record, embedding in zip(records, embeddings)
        ]
        if points:
            self.client.upsert(collection_name=self.collection_name, points=points)
        return {
            "backend": "qdrant",
            "collection": self.collection_name,
            "section_count": len(records),
            "dims": self.embedder.dims,
        }

    def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        query_text = str(query or "").strip()
        if not query_text:
            return []
        response = self.client.query_points(
            collection_name=self.collection_name,
            query=self.embedder.embed([query_text])[0],
            query_filter=self._index_filter(),
            limit=max(1, int(limit or 5)),
        )
        return [
            _record_from_payload(dict(getattr(point, "payload", {}) or {}), float(getattr(point, "score", 0.0) or 0.0))
            for point in getattr(response, "points", response)
        ]

    def _index_filter(self) -> Any:
        return self._rest.Filter(
            must=[
                self._rest.FieldCondition(
                    key="index_kind",
                    match=self._rest.MatchValue(value="skill"),
                ),
                self._rest.FieldCondition(
                    key="role_scope",
                    match=self._rest.MatchValue(value="skill_maintenance"),
                ),
            ]
        )


def _collect_skill_records(skill_root: Path) -> list[dict[str, Any]]:
    if not skill_root.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(skill_root.rglob("SKILL.md")):
        manifest = load_skill_manifest(path, root=skill_root)
        text = "\n".join(
            item
            for item in [
                manifest.name,
                manifest.description,
                manifest.forwin_scope,
                manifest.body,
            ]
            if item
        )
        records.append(
            {
                "index_kind": "skill",
                "skill_id": manifest.name,
                "skill_type": manifest.forwin_scope or manifest.group,
                "section_key": "prompt_compression",
                "role_scope": "skill_maintenance",
                "text": text,
                "source_refs": [manifest.path],
            }
        )
    return records


def _record_from_payload(payload: dict[str, Any], score: float) -> dict[str, Any]:
    source_refs = payload.get("source_refs") if isinstance(payload.get("source_refs"), list) else []
    return SkillVectorRecord(
        skill_id=str(payload.get("skill_id") or ""),
        skill_type=str(payload.get("skill_type") or ""),
        section_key=str(payload.get("section_key") or ""),
        role_scope=str(payload.get("role_scope") or "skill_maintenance"),
        text=str(payload.get("text") or ""),
        source_refs=[str(ref) for ref in source_refs],
        score=score,
    ).as_dict()
