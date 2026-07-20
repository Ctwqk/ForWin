from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


ProjectionComponentKind = Literal["obsidian", "llm_kb", "chapter_memory"]
ProjectionHealthStatus = Literal["never", "running", "healthy", "degraded"]


class ProjectionComponentStatus(BaseModel):
    projection_kind: ProjectionComponentKind
    status: ProjectionHealthStatus
    healthy: bool = False
    target_canon_commit_id: str | None = None
    target_chapter_number: int = 0
    projected_canon_commit_id: str | None = None
    projected_chapter_number: int = 0
    lag: int = 0
    last_event_id: str = ""
    source_digest: str = ""
    last_error: str = ""
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime | None = None


class ProjectionStatusResponse(BaseModel):
    project_id: str
    status: ProjectionHealthStatus
    healthy: bool = False
    target_canon_commit_id: str | None = None
    target_chapter_number: int = 0
    components: list[ProjectionComponentStatus] = Field(default_factory=list)


class ProjectionRefreshResponse(BaseModel):
    ok: bool = True
    deferred: bool = False
    project_id: str
    projection_kind: str = "all"
    target_canon_commit_id: str | None = None
    target_chapter_number: int = 0
    as_of_chapter: int = 0
    trigger: str = ""
    event_type: str = ""
    outbox_event_id: str = ""
    outbox_row_id: str = ""
    components: dict[str, dict[str, Any]] = Field(default_factory=dict)


__all__ = [
    "ProjectionComponentKind",
    "ProjectionComponentStatus",
    "ProjectionHealthStatus",
    "ProjectionRefreshResponse",
    "ProjectionStatusResponse",
]
