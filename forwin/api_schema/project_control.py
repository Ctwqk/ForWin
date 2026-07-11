from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from forwin.planning.constraints import NarrativeConstraintInfo
from forwin.planning.contracts import PlanTaskItem
from .project import ProjectAutomationPublishSettings, ProjectAutomationSettings


class ManualCheckpointRequest(BaseModel):
    boundary_kind: str
    boundary_chapter: int = 0
    reason: str = ""


class BandCheckpointApproveRequest(BaseModel):
    status: Literal["pass", "overridden"] = "overridden"
    reason: str = ""


class NarrativeConstraintCreateRequest(BaseModel):
    constraint_type: str
    level: str = "hard"
    subject_name: str = ""
    description: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    arc_id: str = ""
    band_id: str = ""
    effective_from_chapter: int = 1
    protect_until_chapter: int = 0
    status: str = "active"
    reason: str = ""


class NarrativeConstraintUpdateRequest(BaseModel):
    constraint_type: str | None = None
    level: str | None = None
    subject_name: str | None = None
    description: str | None = None
    payload: dict[str, Any] | None = None
    arc_id: str | None = None
    band_id: str | None = None
    effective_from_chapter: int | None = None
    protect_until_chapter: int | None = None
    status: str | None = None
    reason: str = ""


class NarrativeConstraintsResponse(BaseModel):
    items: list[NarrativeConstraintInfo] = Field(default_factory=list)


class TaskContractUpdateRequest(BaseModel):
    items: list[PlanTaskItem] = Field(default_factory=list)
    reason: str = ""


class TaskContractResponse(BaseModel):
    ok: bool = True
    project_id: str
    scope: str
    chapter_number: int = 0
    band_id: str = ""
    items: list[PlanTaskItem] = Field(default_factory=list)
    message: str = ""


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
    "NarrativeConstraintCreateRequest",
    "NarrativeConstraintUpdateRequest",
    "NarrativeConstraintsResponse",
    "TaskContractUpdateRequest",
    "TaskContractResponse",
    "ProjectAutomationUpdateRequest",
    "ProjectAutomationUpdateResponse",
]
