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
from forwin.long_run_policy import LongRunPolicy
from forwin.protocol.subworld import SubWorldSummary
from .genesis import BookGenesisStageState
from .publisher import PublisherBookMetaRequest
from .tasks import GenerationControlInfo


class ProjectArcSnapshotFields(BaseModel):
    active_arc_id: str = ""
    active_arc_policy_tier: str = ""
    active_arc_target_size: int = 0
    active_arc_soft_min: int = 0
    active_arc_soft_max: int = 0
    active_arc_detailed_band_size: int = 0
    active_arc_frozen_zone_size: int = 0
    active_arc_confidence: float = 0.0
    active_arc_recommendation: str = ""
    active_arc_analysis_confidence: float = 0.0
    active_arc_evidence: list[str] = Field(default_factory=list)
    active_arc_expansion_signals: list[str] = Field(default_factory=list)
    active_arc_compression_signals: list[str] = Field(default_factory=list)
    provisional_band_id: str = ""
    provisional_aggregate_verdict: str = ""
    provisional_preview_char_count: int = 0
    provisional_issue_count: int = 0
    provisional_failure_count: int = 0
    scenario_rehearsal_band_id: str = ""
    scenario_rehearsal_recommendation: str = ""
    scenario_rehearsal_risk_count: int = 0
    scenario_rehearsal_blocker_count: int = 0
    scenario_rehearsal_required_patch_count: int = 0
    scenario_rehearsal_resolution_status: str = ""
    scenario_rehearsal_trigger_reasons: list[str] = Field(default_factory=list)
    scenario_rehearsal_patch_attempt_count: int = 0
    scenario_rehearsal_checkpoint_id: str = ""
    scenario_rehearsal_replan_event_id: str = ""
    active_reader_promise: dict[str, Any] = Field(default_factory=dict)
    active_band_reward_mix: list[str] = Field(default_factory=list)
    active_band_stall_guard: int = 0
    active_revelation_layers: list[dict[str, Any]] = Field(default_factory=list)
    active_band_curiosity_beats: list[dict[str, Any]] = Field(default_factory=list)
    active_band_template_ids: list[str] = Field(default_factory=list)


class ProjectAutomationPublishSettings(BaseModel):
    platform: str = ""
    book_name: str = ""
    upload_url: str = ""
    create_if_missing: bool = False
    book_meta: "PublisherBookMetaRequest" = Field(
        default_factory=lambda: PublisherBookMetaRequest()
    )


class ProjectAutomationSettings(BaseModel):
    enabled: bool = False
    daily_start_time: str = "09:00"
    daily_chapter_quota: int = 1
    daily_plan_quota: int = 0
    daily_write_quota: int = 0
    daily_review_quota: int = 0
    daily_publish_quota: int = 0
    stop_when_review_pending: bool = True
    auto_publish: bool = False
    publish: ProjectAutomationPublishSettings = Field(default_factory=ProjectAutomationPublishSettings)
    publish_bindings: list[ProjectAutomationPublishSettings] = Field(default_factory=list)
    long_run_policy: LongRunPolicy = Field(default_factory=LongRunPolicy)
    last_scheduler_date: str = ""
    last_scheduler_at: str = ""
    last_scheduler_action: str = ""
    last_scheduler_message: str = ""
    last_scheduler_task_id: str = ""


class ProjectSummary(ProjectArcSnapshotFields):
    id: str
    title: str
    genre: str
    premise: str = ""
    created_at: str = ""
    target_total_chapters: int = 50
    chapter_count: int = 0
    generated_chapter_count: int = 0
    accepted_chapter_count: int = 0
    needs_review_chapter_count: int = 0
    upload_task_count: int = 0
    uploaded_chapter_count: int = 0
    creation_status: str = "legacy"
    active_genesis_revision_id: str = ""
    genesis_stage_overview: list[BookGenesisStageState] = Field(default_factory=list)
    can_start_writing: bool = False
    automation: ProjectAutomationSettings = Field(default_factory=ProjectAutomationSettings)
    governance: ProjectGovernanceSettings = Field(default_factory=ProjectGovernanceSettings)
    latest_stage: str = ""
    pacing_verdict: str = ""
    pacing_summary: str = ""
    last_replan_status: str = ""
    last_replan_strategy: str = ""
    last_replan_reason: str = ""
    current_time_label: str = ""
    world_pressure_level: str = ""
    world_pressure_summary: str = ""
    generation_control: GenerationControlInfo = Field(default_factory=GenerationControlInfo)
    chapters: list[dict[str, object]] = Field(default_factory=list)
    latest_band_checkpoint: BandCheckpointDetail | None = None
    blocking_reason: BlockingReasonInfo = Field(default_factory=BlockingReasonInfo)
    next_gate: str = ""


class EntityInfo(BaseModel):
    id: str
    kind: str
    name: str
    description: str
    importance: int


class ThreadInfo(BaseModel):
    id: str
    name: str
    description: str
    status: str
    priority: int


class ChapterInfo(BaseModel):
    chapter_number: int
    title: str
    status: str
    char_count: int = 0
    summary: str = ""
    has_draft: bool = False
    has_review: bool = False
    acceptance_mode: str = ""
    repair_attempt_count: int = 0
    canon_risk_level: str = ""
    latest_repair_scope: str = ""


class ChapterListResponse(BaseModel):
    project_id: str
    total: int = 0
    offset: int = 0
    limit: int = 60
    has_more: bool = False
    chapters: list[ChapterInfo] = Field(default_factory=list)


class ProjectDetail(ProjectArcSnapshotFields):
    id: str
    title: str
    premise: str
    genre: str
    setting_summary: str
    target_total_chapters: int = 50
    creation_status: str = "legacy"
    active_genesis_revision_id: str = ""
    genesis_stage_overview: list[BookGenesisStageState] = Field(default_factory=list)
    can_start_writing: bool = False
    chapter_count: int = 0
    generated_chapter_count: int = 0
    accepted_chapter_count: int = 0
    needs_review_chapter_count: int = 0
    upload_task_count: int = 0
    uploaded_chapter_count: int = 0
    automation: ProjectAutomationSettings = Field(default_factory=ProjectAutomationSettings)
    governance: ProjectGovernanceSettings = Field(default_factory=ProjectGovernanceSettings)
    characters: list[EntityInfo] = []
    locations: list[EntityInfo] = []
    factions: list[EntityInfo] = []
    subworlds: list[SubWorldSummary] = []
    threads: list[ThreadInfo] = []
    chapters: list[ChapterInfo] = []
    latest_stage: str = ""
    progress_ratio: float = 0.0
    pacing_verdict: str = ""
    pacing_summary: str = ""
    current_time_label: str = ""
    recent_replans: list[dict[str, object]] = []
    world_pressure_level: str = ""
    world_pressure_summary: str = ""
    npc_intent_count: int = 0
    recent_npc_intents: list[dict[str, object]] = []
    generation_control: GenerationControlInfo = Field(default_factory=GenerationControlInfo)
    latest_band_checkpoint: BandCheckpointDetail | None = None
    blocking_reason: BlockingReasonInfo = Field(default_factory=BlockingReasonInfo)
    next_gate: str = ""
    decision_timeline: list[DecisionEventInfo] = Field(default_factory=list)
    narrative_constraints: list[NarrativeConstraintInfo] = Field(default_factory=list)


class ProjectDeleteResponse(BaseModel):
    ok: bool
    project_id: str
    message: str
    operation_id: str = ""


class ProjectCreateRequest(BaseModel):
    title: str
    premise: str
    genre: str = "玄幻"
    setting_summary: str = ""
    target_total_chapters: int = Field(default=50, ge=1, le=5000)
    audience_hint: str = ""
    core_emotion: str = ""
    core_delight: str = ""
    inspiration_notes: str = ""
    content_guardrails: list[str] = Field(default_factory=list)
    publish_bindings: list[ProjectAutomationPublishSettings] = Field(default_factory=list)
    publish_platform: str = ""
    publish_book_name: str = ""
    publish_upload_url: str = ""
    platform_has_existing_book: bool = True


class ProjectCreateResponse(BaseModel):
    ok: bool
    project_id: str
    title: str
    target_total_chapters: int = 50
    creation_status: str = "creating"
    active_genesis_revision_id: str = ""
    workspace_url: str = ""
    message: str


class ProjectContinueGenerationRequest(BaseModel):
    max_chapters: int | None = Field(default=None, ge=1)
    auto_continue: bool | None = None
    run_until_chapter: int | None = Field(default=None, ge=1)
    operation_mode: str | None = None
    review_interval_chapters: int | None = None
    progression_mode: str | None = None
    auto_band_checkpoint: bool | None = None
    band_warn_action: str | None = None
    manual_checkpoints_enabled: bool | None = None
    future_constraints_enabled: bool | None = None
    generation_audit_interval_chapters: int | None = Field(default=None, ge=0)
    generation_audit_pause_enabled: bool | None = None


class ProjectExtendGenerationRequest(BaseModel):
    additional_chapters: int = Field(default=50, ge=1, le=500)
    arc_title: str = ""
    arc_synopsis: str = ""
    continuity_guard: str = ""
    reason: str = ""


__all__ = [
    'ProjectArcSnapshotFields',
    'ProjectAutomationPublishSettings',
    'ProjectAutomationSettings',
    'ProjectSummary',
    'EntityInfo',
    'ThreadInfo',
    'ChapterInfo',
    'ChapterListResponse',
    'ProjectDetail',
    'ProjectDeleteResponse',
    'ProjectCreateRequest',
    'ProjectCreateResponse',
    'ProjectContinueGenerationRequest',
    'ProjectExtendGenerationRequest',
]
