from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ContextIssue:
    code: str
    severity: str = "warning"
    message: str = ""
    provider: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ContextRequest:
    project_id: str
    chapter_plan: Any
    repo: Any
    session: Any | None = None


@dataclass(slots=True)
class ContextDraft:
    data: dict[str, Any] = field(default_factory=dict)
    issues: list[ContextIssue] = field(default_factory=list)
