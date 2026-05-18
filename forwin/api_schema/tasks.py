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


class CodexBridgeStatusResponse(BaseModel):
    enabled: bool = False
    bridge_url: str = ""
    healthy: bool = False
    status: str = "disabled"
    backend: str = "codex_bridge"
    message: str = ""
    health: dict[str, Any] = Field(default_factory=dict)


class GenerationControlInfo(BaseModel):
    plan_state: str = "none"
    writing_state: str = "not_started"
    review_state: str = "none"
    current_stage: str = ""
    current_chapter: int = 0
    next_chapter: int = 0
    accepted_chapters: list[int] = Field(default_factory=list)
    drafted_chapters: list[int] = Field(default_factory=list)
    generated_chapters: list[int] = Field(default_factory=list)
    planned_chapters: list[int] = Field(default_factory=list)
    failed_chapters: list[int] = Field(default_factory=list)
    pending_review_chapters: list[int] = Field(default_factory=list)
    can_pause: bool = False
    can_resume: bool = False
    pause_requested: bool = False
    review_interval_chapters: int = 0
    chapters_until_review: int = 0
    chapters_until_replan_eligible: int = 0
    blocking_reason: BlockingReasonInfo = Field(default_factory=BlockingReasonInfo)
    latest_band_checkpoint: BandCheckpointDetail | None = None
    next_gate: str = ""


class TaskResponse(BaseModel):
    task_kind: str = "generation"
    task_id: str
    status: str
    title: str = ""
    subtitle: str = ""
    project_id: str | None = None
    extension_client_id: str = ""
    error: str | None = None
    message: str = ""
    current_stage: str = "queued"
    stage_history: list[dict[str, Any]] = Field(default_factory=list)
    requested_chapters: int = 0
    current_chapter: int = 0
    completed_chapters: list[int] = Field(default_factory=list)
    failed_chapters: list[int] = Field(default_factory=list)
    paused_chapters: list[int] = Field(default_factory=list)
    frozen_artifacts: list[str] = Field(default_factory=list)
    pause_requested: bool = False
    pausable: bool = False
    resumable: bool = False
    generation_control: GenerationControlInfo = Field(default_factory=GenerationControlInfo)
    terminable: bool = False
    deletable: bool = False
    interrupted_by_restart: bool = False
    recovery_suggestion: str = ""
    persistence_degraded: bool = False
    persistence_error: str | None = None
    created_at: str = ""
    updated_at: str = ""


class TaskSummaryResponse(TaskResponse):
    pass


class TaskCenterItemResponse(BaseModel):
    task_kind: str
    task_id: str
    status: str
    title: str = ""
    subtitle: str = ""
    project_id: str | None = None
    extension_client_id: str = ""
    message: str = ""
    error: str | None = None
    current_stage: str = ""
    stage_history: list[dict[str, Any]] = Field(default_factory=list)
    requested_chapters: int = 0
    current_chapter: int = 0
    completed_chapters: list[int] = Field(default_factory=list)
    failed_chapters: list[int] = Field(default_factory=list)
    paused_chapters: list[int] = Field(default_factory=list)
    frozen_artifacts: list[str] = Field(default_factory=list)
    current_url: str = ""
    upload_url: str | None = None
    platform: str = ""
    display_name: str = ""
    publish: bool | None = None
    result_payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    claimed_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    abort_requested: bool = False
    pause_requested: bool = False
    pausable: bool = False
    resumable: bool = False
    generation_control: GenerationControlInfo = Field(default_factory=GenerationControlInfo)
    terminable: bool = False
    deletable: bool = False
    interrupted_by_restart: bool = False
    recovery_suggestion: str = ""


class ActiveGenerationTaskCheckResponse(BaseModel):
    has_active_generation_task: bool = False
    active_task_ids: list[str] = Field(default_factory=list)
    active_count: int = 0
    safe_to_restart: bool = True
    message: str = ""


class TaskMutationResponse(BaseModel):
    ok: bool
    task_kind: str
    task_id: str
    status: str
    message: str


class BulkDeleteResponse(BaseModel):
    ok: bool
    deleted_count: int = 0
    skipped_count: int = 0
    deleted_ids: list[str] = Field(default_factory=list)
    skipped_ids: list[str] = Field(default_factory=list)
    message: str = ""
    operation_id: str = ""


class TaskBulkDeleteItem(BaseModel):
    task_kind: str
    task_id: str


class TaskBulkDeleteRequest(BaseModel):
    items: list[TaskBulkDeleteItem] = Field(default_factory=list)


class ProjectBulkDeleteRequest(BaseModel):
    project_ids: list[str] = Field(default_factory=list)


__all__ = [
    'CodexBridgeStatusResponse',
    'GenerationControlInfo',
    'TaskResponse',
    'TaskSummaryResponse',
    'TaskCenterItemResponse',
    'ActiveGenerationTaskCheckResponse',
    'TaskMutationResponse',
    'BulkDeleteResponse',
    'TaskBulkDeleteItem',
    'TaskBulkDeleteRequest',
    'ProjectBulkDeleteRequest',
]
