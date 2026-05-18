from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from forwin.config import DEFAULT_MINIMAX_BASE_URL, DEFAULT_MINIMAX_MODEL
from forwin.governance import (
    BandCheckpointDetail,
    BlockingReasonInfo,
    DecisionEventInfo,
    NarrativeConstraintInfo,
    PlanTaskItem,
    ProjectGovernanceSettings,
)
from forwin.protocol.subworld import SubWorldSummary
from .project import ProjectAutomationPublishSettings, ProjectAutomationSettings


class ProjectGovernanceUpdateRequest(BaseModel):
    default_operation_mode: str | None = None
    review_interval_chapters: int | None = None
    progression_mode: str | None = None
    auto_band_checkpoint: bool | None = None
    band_warn_action: str | None = None
    manual_checkpoints_enabled: bool | None = None
    future_constraints_enabled: bool | None = None
    generation_audit_interval_chapters: int | None = Field(default=None, ge=0)
    generation_audit_pause_enabled: bool | None = None
    reason: str = ""


class ProjectGovernanceResponse(BaseModel):
    ok: bool
    project_id: str
    governance: ProjectGovernanceSettings
    message: str = ""


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
    'ProjectGovernanceUpdateRequest',
    'ProjectGovernanceResponse',
    'ManualCheckpointRequest',
    'BandCheckpointApproveRequest',
    'NarrativeConstraintCreateRequest',
    'NarrativeConstraintUpdateRequest',
    'NarrativeConstraintsResponse',
    'TaskContractUpdateRequest',
    'TaskContractResponse',
    'ProjectAutomationUpdateRequest',
    'ProjectAutomationUpdateResponse',
]
