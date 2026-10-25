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
    runtime: Any | None = None
    share_runtime: bool = True
    _runtimes: dict[int, Any] = field(default_factory=dict)

    def runtime_as_of(self, chapter: int):
        from forwin.book_state import BookStateProjection
        from forwin.book_state.runtime import require_runtime_as_of

        if not self.share_runtime or self.session is None:
            return None
        if self.runtime is not None:
            require_runtime_as_of(self.runtime, self.project_id, max(int(self.chapter_plan.chapter_number) - 1, 0))
            self._runtimes[self.runtime.as_of_chapter] = self.runtime
        if chapter not in self._runtimes:
            self._runtimes[chapter] = BookStateProjection(self.session).load_runtime_as_of(
                self.project_id, as_of_chapter=chapter,
            )
        return self._runtimes[chapter]


@dataclass(slots=True)
class ContextDraft:
    data: dict[str, Any] = field(default_factory=dict)
    issues: list[ContextIssue] = field(default_factory=list)
