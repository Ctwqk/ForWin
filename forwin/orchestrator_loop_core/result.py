from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class RunResult:
    """Summary for a single orchestrator run."""

    project_id: str
    requested_chapters: int
    completed_chapters: list[int] = field(default_factory=list)
    failed_chapters: list[int] = field(default_factory=list)
    paused_chapters: list[int] = field(default_factory=list)
    frozen_artifacts: list[str] = field(default_factory=list)
    system_block_chapters: list[int] = field(default_factory=list)
    cancelled: bool = False
    paused: bool = False

    @property
    def status(self) -> str:
        if self.paused:
            return "paused"
        if self.cancelled:
            return "cancelled"
        if self.paused_chapters:
            return "needs_review"
        if self.failed_chapters and not self.completed_chapters:
            return "failed"
        if self.failed_chapters:
            return "partial_failed"
        return "completed"


@dataclass(slots=True)
class ProvisionalGateSnapshot:
    """The latest persisted provisional execution used to gate canon writing."""

    id: str
    aggregate_verdict: str
    failure_count: int
    issue_count: int
    chapter_numbers: list[int]
