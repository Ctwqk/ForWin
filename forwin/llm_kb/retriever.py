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
    ) -> list[dict[str, Any]]:
        return [
            record.as_dict()
            for record in self.index.search(
                project_id,
                query,
                role=role,
                limit=limit,
                as_of_chapter=as_of_chapter,
                visibility_scope=visibility_scope,
            )
        ]
