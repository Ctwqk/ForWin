from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from .project import ProjectAutomationPublishSettings, ProjectAutomationSettings


class ManualCheckpointRequest(BaseModel):
    boundary_kind: str
    boundary_chapter: int = 0
    reason: str = ""


class BandCheckpointApproveRequest(BaseModel):
    status: Literal["pass", "overridden"] = "overridden"
    reason: str = ""


class ProjectAutomationUpdateRequest(BaseModel):
    enabled: bool = False
    daily_start_time: str = "09:00"
    daily_chapter_quota: int = 1
    daily_plan_quota: int = 0
    daily_write_quota: int = 0
    daily_review_quota: int = 0
    daily_publish_quota: int = 0
    stop_when_review_pending: bool = True
    auto_publish: bool = False
    publish: ProjectAutomationPublishSettings | None = None
    publish_bindings: list[ProjectAutomationPublishSettings] | None = None


class ProjectAutomationUpdateResponse(BaseModel):
    ok: bool
    project_id: str
    automation: ProjectAutomationSettings
    message: str


__all__ = [
    "ManualCheckpointRequest",
    "BandCheckpointApproveRequest",
    "ProjectAutomationUpdateRequest",
    "ProjectAutomationUpdateResponse",
]
