from __future__ import annotations

import json
import os
from dataclasses import dataclass
from hashlib import sha1
from pathlib import Path
from typing import Any
from uuid import UUID

from forwin.config import DEFAULT_QDRANT_URL
from forwin.obsidian.frontmatter import EDITABLE_FIELDS, parse_frontmatter, parse_sections
from forwin.retrieval.memory_index import (
    HashTextEmbedder,
    TextEmbedder,
    _create_qdrant_client,
    _qdrant_models,
)


@dataclass
class ObsidianHumanVectorRecord:
    project_id: str
    vault_path: str
    page_key: str
    node_id: str
    section_name: str
    section_type: str
    canon_status: str
    as_of_chapter: int
    source_digest: str
    section_digest: str
    text: str
    score: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "index_kind": "obsidian_human",
            "vault_path": self.vault_path,
            "page_key": self.page_key,
            "node_id": self.node_id,
            "section_name": self.section_name,
            "section_type": self.section_type,
            "canon_status": self.canon_status,
            "as_of_chapter": self.as_of_chapter,
            "source_digest": self.source_digest,
            "section_digest": self.section_digest,
            "text": self.text,
            "score": self.score,
        }


def _default_qdrant_url() -> str:
    return os.environ.get("FORWIN_QDRANT_URL", DEFAULT_QDRANT_URL)


def _default_collection_name() -> str:
    return os.environ.get("FORWIN_OBSIDIAN_HUMAN_QDRANT_COLLECTION", "obsidian_human_vectors")


def _point_id(project_id: str, vault_path: str, section_name: str) -> str:
    digest = sha1(f"{project_id}:{vault_path}:{section_name}".encode("utf-8")).hexdigest()[:32]
    return str(UUID(digest))


class ObsidianHumanVectorIndex:
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

    def rebuild_project(self, project_id: str, *, vault_root: Path) -> dict[str, Any]:
        sections = _collect_human_sections(project_id, vault_root)
        existing_payloads = _existing_payloads_by_point_id(
            self.client,
            self.collection_name,
            project_id,
        )
        sections_to_upsert = []
        skipped = 0
        for section in sections:
            point_id = _point_id(project_id, section["vault_path"], section["section_name"])
            existing = existing_payloads.get(point_id)
            if existing and existing.get("section_digest") == section.get("section_digest"):
                skipped += 1
                continue
            sections_to_upsert.append((point_id, section))
        embeddings = self.embedder.embed([section["text"] for _, section in sections_to_upsert]) if sections_to_upsert else []
        points = [
            self._rest.PointStruct(
                id=point_id,
                vector=embedding,
                payload=section,
            )
            for (point_id, section), embedding in zip(sections_to_upsert, embeddings)
        ]
        if points:
            self.client.upsert(collection_name=self.collection_name, points=points)
        return {
            "backend": "qdrant",
            "collection": self.collection_name,
            "section_count": len(sections),
            "upserted_section_count": len(points),
            "skipped_section_count": skipped,
            "dims": self.embedder.dims,
        }

    def search(
        self,
        project_id: str,
        query: str,
        *,
        limit: int = 5,
        as_of_chapter: int = 0,
        section_type: str = "",
    ) -> list[dict[str, Any]]:
        query_text = str(query or "").strip()
        if not query_text:
            return []
        response = self.client.query_points(
            collection_name=self.collection_name,
            query=self.embedder.embed([query_text])[0],
            query_filter=self._project_filter(
                project_id,
                as_of_chapter=as_of_chapter,
                section_type=section_type,
            ),
            limit=max(1, int(limit or 5)),
        )
        return [
            _record_from_payload(dict(getattr(point, "payload", {}) or {}), float(getattr(point, "score", 0.0) or 0.0))
            for point in getattr(response, "points", response)
        ]

    def _project_filter(
        self,
        project_id: str,
        *,
        as_of_chapter: int = 0,
        section_type: str = "",
    ) -> Any:
        must = [
            self._rest.FieldCondition(
                key="project_id",
                match=self._rest.MatchValue(value=project_id),
            ),
            self._rest.FieldCondition(
                key="index_kind",
                match=self._rest.MatchValue(value="obsidian_human"),
            ),
        ]
        if int(as_of_chapter or 0) > 0:
            must.append(
                self._rest.FieldCondition(
                    key="as_of_chapter",
                    match=self._rest.MatchValue(value=int(as_of_chapter)),
                )
            )
        if str(section_type or "").strip():
            must.append(
                self._rest.FieldCondition(
                    key="section_type",
                    match=self._rest.MatchValue(value=str(section_type).strip()),
                )
            )
        return self._rest.Filter(must=must)


def _collect_human_sections(project_id: str, vault_root: Path) -> list[dict[str, Any]]:
    root = Path(vault_root)
    if not root.exists():
        return []
    sections: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.md")):
        if path.name == "AGENTS.md":
            continue
        markdown = path.read_text(encoding="utf-8")
        frontmatter, _ = parse_frontmatter(markdown)
        if not frontmatter or str(frontmatter.get("project_id", project_id)) != project_id:
            continue
        parsed_sections = parse_sections(markdown)
        vault_path = path.relative_to(root).as_posix()
        for section_name in EDITABLE_FIELDS:
            text = parsed_sections.get(section_name, "").strip()
            if not text or text == "_empty_":
                continue
            sections.append(
                {
                    "project_id": project_id,
                    "index_kind": "obsidian_human",
                    "vault_path": vault_path,
                    "page_key": str(frontmatter.get("forwin_id") or frontmatter.get("node_id") or vault_path),
                    "node_id": str(frontmatter.get("node_id") or ""),
                    "section_name": section_name,
                    "section_type": "editable",
                    "canon_status": _canon_status(section_name),
                    "as_of_chapter": int(frontmatter.get("as_of_chapter") or 0),
                    "source_digest": str(frontmatter.get("source_digest") or ""),
                    "section_digest": _section_digest(
                        project_id=project_id,
                        vault_path=vault_path,
                        section_name=section_name,
                        text=text,
                        source_digest=str(frontmatter.get("source_digest") or ""),
                    ),
                    "text": text,
                }
            )
    return sections


def _canon_status(section_name: str) -> str:
    if section_name == "Proposed Correction":
        return "proposal_pending"
    return "human_unreviewed"


def _record_from_payload(payload: dict[str, Any], score: float) -> dict[str, Any]:
    return ObsidianHumanVectorRecord(
        project_id=str(payload.get("project_id") or ""),
        vault_path=str(payload.get("vault_path") or ""),
        page_key=str(payload.get("page_key") or ""),
        node_id=str(payload.get("node_id") or ""),
        section_name=str(payload.get("section_name") or ""),
        section_type=str(payload.get("section_type") or ""),
        canon_status=str(payload.get("canon_status") or "human_unreviewed"),
        as_of_chapter=int(payload.get("as_of_chapter") or 0),
        source_digest=str(payload.get("source_digest") or ""),
        section_digest=str(payload.get("section_digest") or ""),
        text=str(payload.get("text") or ""),
        score=score,
    ).as_dict()


def _existing_payloads_by_point_id(client: Any, collection_name: str, project_id: str) -> dict[str, dict[str, Any]]:
    if hasattr(client, "collections"):
        collection = getattr(client, "collections", {}).get(collection_name, {})
        points = collection.get("points", {}) if isinstance(collection, dict) else {}
        return {
            str(point_id): dict(getattr(point, "payload", {}) or {})
            for point_id, point in points.items()
            if getattr(point, "payload", {}).get("project_id") == project_id
            and getattr(point, "payload", {}).get("index_kind") == "obsidian_human"
        }
    if hasattr(client, "scroll"):
        try:
            response = client.scroll(
                collection_name=collection_name,
                scroll_filter=None,
                limit=10_000,
                with_payload=True,
                with_vectors=False,
            )
        except TypeError:
            return {}
        points = response[0] if isinstance(response, tuple) else response
        return {
            str(getattr(point, "id", "")): dict(getattr(point, "payload", {}) or {})
            for point in points
            if getattr(point, "id", None)
            and getattr(point, "payload", {}).get("project_id") == project_id
            and getattr(point, "payload", {}).get("index_kind") == "obsidian_human"
        }
    return {}


def _section_digest(
    *,
    project_id: str,
    vault_path: str,
    section_name: str,
    text: str,
    source_digest: str,
) -> str:
    return sha1(
        json.dumps(
            {
                "project_id": project_id,
                "vault_path": vault_path,
                "section_name": section_name,
                "text": text,
                "source_digest": source_digest,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
