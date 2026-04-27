from __future__ import annotations

from pathlib import Path
from typing import Any

from .store import DEFAULT_LLM_KB_ROOT
from .vector_index import LLMKBVectorIndex


class LLMKnowledgeBaseRetriever:
    def __init__(self, *, root: Path | None = None) -> None:
        self.root = root or DEFAULT_LLM_KB_ROOT
        self.index = LLMKBVectorIndex(self.root)

    def search(
        self,
        project_id: str,
        query: str,
        *,
        role: str = "writer",
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        return [
            record.as_dict()
            for record in self.index.search(project_id, query, role=role, limit=limit)
        ]
