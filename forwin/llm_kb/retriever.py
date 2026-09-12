from __future__ import annotations

from pathlib import Path
from typing import Any

from .store import DEFAULT_LLM_KB_ROOT
from .vector_index import LLMKBVectorIndex


class LLMKnowledgeBaseRetriever:
    def __init__(
        self,
        *,
        root: Path | None = None,
        qdrant_url: str | None = None,
        qdrant_collection: str | None = None,
        qdrant_client: Any | None = None,
        qdrant_models: Any | None = None,
    ) -> None:
        self.root = root or DEFAULT_LLM_KB_ROOT
        self.index = LLMKBVectorIndex(
            self.root,
            qdrant_url=qdrant_url,
            collection_name=qdrant_collection,
            qdrant_client=qdrant_client,
            qdrant_models=qdrant_models,
        )

    def search(
        self,
        project_id: str,
        query: str,
        *,
        role: str = "writer",
        limit: int = 5,
        as_of_chapter: int | None = None,
        visibility_scope: str | None = None,
        session=None,
        baseline=None,
    ) -> list[dict[str, Any]]:
        from .source_validation import compiled_manifest, validated_manifest, validated_file
        from .vector_index import _collect_project_sections

        manifest = (
            validated_manifest(self.root, project_id, session, baseline)
            if baseline is not None
            else (compiled_manifest(self.root, project_id) or None)
        )
        if manifest is not None and not manifest:
            return []
        authoritative = {}
        if manifest:
            authoritative = {
                (row["file_key"], row["section_key"]): row
                for row in _collect_project_sections(
                    self.root / project_id,
                    source_digest=manifest["source_digest"],
                    as_of_chapter=manifest["as_of_chapter"],
                    projection_version=manifest["projection_version"],
                )
                if validated_file(self.root, project_id, row.get("source_file_key", row["file_key"]), manifest)
                is not None
            }
        records = [
            record.as_dict()
            for record in self.index.search(
                project_id,
                query,
                role=role,
                limit=limit,
                as_of_chapter=baseline.as_of_chapter
                if baseline is not None
                else (as_of_chapter or None),
                visibility_scope=visibility_scope,
                source_sections=authoritative if manifest is not None else None,
            )
        ]

        if baseline is not None:
            baseline.assert_current(session)
        return records

    def close(self) -> None:
        self.index.close()
