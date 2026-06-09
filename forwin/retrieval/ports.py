from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class KnowledgeRebuildRequest:
    project_id: str
    as_of_chapter: int = 0
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KnowledgeSearchRequest:
    project_id: str
    query: str
    role: str = "writer"
    limit: int = 5
    chapter_number: int = 0
    payload: dict[str, Any] = field(default_factory=dict)


class KnowledgeIndexPort(Protocol):
    def rebuild(self, request: KnowledgeRebuildRequest) -> dict[str, Any]:
        ...

    def search(self, request: KnowledgeSearchRequest) -> list[dict[str, Any]]:
        ...


class CallableKnowledgeIndexPort:
    def __init__(
        self,
        *,
        rebuild: Callable[[KnowledgeRebuildRequest], dict[str, Any]],
        search: Callable[[KnowledgeSearchRequest], list[dict[str, Any]]],
    ) -> None:
        self._rebuild = rebuild
        self._search = search

    def rebuild(self, request: KnowledgeRebuildRequest) -> dict[str, Any]:
        return self._rebuild(request)

    def search(self, request: KnowledgeSearchRequest) -> list[dict[str, Any]]:
        return self._search(request)


__all__ = [
    "CallableKnowledgeIndexPort",
    "KnowledgeIndexPort",
    "KnowledgeRebuildRequest",
    "KnowledgeSearchRequest",
]
