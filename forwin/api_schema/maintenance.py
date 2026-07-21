from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


PostCanonRunStatus = Literal["pending", "running", "succeeded", "failed"]
PostCanonChapterStatus = Literal[
    "pending",
    "running",
    "stale",
    "failed",
    "blocked",
    "ready",
]


class PostCanonMaintenanceRunInfo(BaseModel):
    id: str
    step_name: str
    status: PostCanonRunStatus
    attempts: int = 0
    worker_id: str = ""
    lease_epoch: int = 0
    lease_expires_at: datetime | None = None
    lease_active: bool = False
    reclaimable: bool = False
    heartbeat_at: datetime | None = None
    last_error: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime | None = None


class PostCanonMaintenanceChapterInfo(BaseModel):
    canon_commit_id: str
    candidate_id: str
    chapter_number: int
    status: PostCanonChapterStatus
    phase3_complete: bool = False
    controls_complete: bool = False
    order_controls_status: str = "pending"
    checkpoint_status: str = ""
    ready: bool = False
    blocking_reasons: list[str] = Field(default_factory=list)
    runs: list[PostCanonMaintenanceRunInfo] = Field(default_factory=list)


class PostCanonMaintenanceStatusResponse(BaseModel):
    project_id: str
    ready: bool = False
    chapters: list[PostCanonMaintenanceChapterInfo] = Field(default_factory=list)


__all__ = [
    "PostCanonChapterStatus",
    "PostCanonMaintenanceChapterInfo",
    "PostCanonMaintenanceRunInfo",
    "PostCanonMaintenanceStatusResponse",
    "PostCanonRunStatus",
]
