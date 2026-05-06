from __future__ import annotations

from typing import Protocol

from .request import ContextDraft, ContextIssue, ContextRequest


class ContextProvider(Protocol):
    name: str

    def contribute(self, request: ContextRequest, draft: ContextDraft) -> None:
        ...


class ContextGate(Protocol):
    name: str

    def validate(self, request: ContextRequest, draft: ContextDraft) -> list[ContextIssue]:
        ...
