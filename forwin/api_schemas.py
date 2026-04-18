from __future__ import annotations

from typing import Any

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


class GenerateRequest(BaseModel):
    premise: str
    genre: str = "玄幻"
    num_chapters: int = 3
    project_id: str | None = None
    model_profile_id: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    operation_mode: str | None = None
    freeze_failed_candidates: bool | None = None
    min_chapter_chars: int | None = None
    review_interval_chapters: int | None = None
    progression_mode: str | None = None
    auto_band_checkpoint: bool | None = None
    band_warn_action: str | None = None
    manual_checkpoints_enabled: bool | None = None
    future_constraints_enabled: bool | None = None


class LLMSettingsRequest(BaseModel):
    api_key: str = ""
    base_url: str = DEFAULT_MINIMAX_BASE_URL
    model: str = DEFAULT_MINIMAX_MODEL
    operation_mode: str = "blackbox"
    freeze_failed_candidates: bool = True
    min_chapter_chars: int = 2500
    review_interval_chapters: int = 0
    progression_mode: str = "serial_canon_band_guard"
    auto_band_checkpoint: bool = True
    band_warn_action: str = "pause"
    manual_checkpoints_enabled: bool = True
    future_constraints_enabled: bool = True


class ModelProfile(BaseModel):
    id: str
    name: str
    has_api_key: bool
    base_url: str
    model: str


class LLMProfileUpsertRequest(BaseModel):
    profile_id: str | None = None
    name: str
    api_key: str = ""
    base_url: str = DEFAULT_MINIMAX_BASE_URL
    model: str = DEFAULT_MINIMAX_MODEL
    set_as_default: bool = False


class LLMDefaultProfileRequest(BaseModel):
    profile_id: str


class LLMPreferencesRequest(BaseModel):
    operation_mode: str = "blackbox"
    freeze_failed_candidates: bool = True
    min_chapter_chars: int = 2500
    review_interval_chapters: int = 0
    progression_mode: str = "serial_canon_band_guard"
    auto_band_checkpoint: bool = True
    band_warn_action: str = "pause"
    manual_checkpoints_enabled: bool = True
    future_constraints_enabled: bool = True


class LLMSettingsResponse(BaseModel):
    has_api_key: bool
    base_url: str
    model: str
    profiles: list[ModelProfile] = Field(default_factory=list)
    default_profile_id: str = ""
    operation_mode: str = "blackbox"
    freeze_failed_candidates: bool = True
    min_chapter_chars: int = 2500
    review_interval_chapters: int = 0
    progression_mode: str = "serial_canon_band_guard"
    auto_band_checkpoint: bool = True
    band_warn_action: str = "pause"
    manual_checkpoints_enabled: bool = True
    future_constraints_enabled: bool = True
    message: str = ""


class GenerationControlInfo(BaseModel):
    plan_state: str = "none"
    writing_state: str = "not_started"
    review_state: str = "none"
    current_stage: str = ""
    current_chapter: int = 0
    next_chapter: int = 0
    accepted_chapters: list[int] = Field(default_factory=list)
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


class TaskSummaryResponse(TaskResponse):
    created_at: str = ""
    updated_at: str = ""


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


class TaskBulkDeleteItem(BaseModel):
    task_kind: str
    task_id: str


class TaskBulkDeleteRequest(BaseModel):
    items: list[TaskBulkDeleteItem] = Field(default_factory=list)


class ProjectBulkDeleteRequest(BaseModel):
    project_ids: list[str] = Field(default_factory=list)


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
    auto_publish: bool = False
    publish: ProjectAutomationPublishSettings = Field(default_factory=ProjectAutomationPublishSettings)
    publish_bindings: list[ProjectAutomationPublishSettings] = Field(default_factory=list)
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
    target_total_chapters: int = 3
    chapter_count: int = 0
    generated_chapter_count: int = 0
    accepted_chapter_count: int = 0
    needs_review_chapter_count: int = 0
    upload_task_count: int = 0
    uploaded_chapter_count: int = 0
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


class ProjectDetail(ProjectArcSnapshotFields):
    id: str
    title: str
    premise: str
    genre: str
    setting_summary: str
    target_total_chapters: int = 3
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


class ProjectCreateRequest(BaseModel):
    title: str
    premise: str
    genre: str = "玄幻"
    setting_summary: str = ""
    target_total_chapters: int = Field(default=3, ge=1, le=200)
    publish_bindings: list[ProjectAutomationPublishSettings] = Field(default_factory=list)
    publish_platform: str = ""
    publish_book_name: str = ""
    publish_upload_url: str = ""
    platform_has_existing_book: bool = True


class ProjectCreateResponse(BaseModel):
    ok: bool
    project_id: str
    title: str
    target_total_chapters: int = 3
    message: str


class ProjectContinueGenerationRequest(BaseModel):
    max_chapters: int | None = None
    operation_mode: str | None = None
    review_interval_chapters: int | None = None
    progression_mode: str | None = None
    auto_band_checkpoint: bool | None = None
    band_warn_action: str | None = None
    manual_checkpoints_enabled: bool | None = None
    future_constraints_enabled: bool | None = None


class ProjectGovernanceUpdateRequest(BaseModel):
    default_operation_mode: str | None = None
    review_interval_chapters: int | None = None
    progression_mode: str | None = None
    auto_band_checkpoint: bool | None = None
    band_warn_action: str | None = None
    manual_checkpoints_enabled: bool | None = None
    future_constraints_enabled: bool | None = None
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
    status: str = "overridden"
    reason: str = ""


class DecisionEventsResponse(BaseModel):
    items: list[DecisionEventInfo] = Field(default_factory=list)


class CausalReplayResponse(BaseModel):
    root_event: DecisionEventInfo | None = None
    timeline: list[DecisionEventInfo] = Field(default_factory=list)
    branches: dict[str, list[DecisionEventInfo]] = Field(default_factory=dict)
    current_outcome: str = ""
    linked_review_refs: list[DecisionEventInfo] = Field(default_factory=list)
    linked_checkpoint_refs: list[DecisionEventInfo] = Field(default_factory=list)


class GovernanceInsightsResponse(BaseModel):
    top_override_rule_types: list[dict[str, Any]] = Field(default_factory=list)
    top_override_reasons: list[dict[str, Any]] = Field(default_factory=list)
    top_warn_but_allowed_issue_types: list[dict[str, Any]] = Field(default_factory=list)
    top_constraint_false_positive_types: list[dict[str, Any]] = Field(default_factory=list)
    forced_accept_frequency: int = 0
    most_common_blocking_reasons: list[dict[str, Any]] = Field(default_factory=list)
    recent_band_checkpoint_distribution: list[dict[str, Any]] = Field(default_factory=list)
    issue_group_distribution: list[dict[str, Any]] = Field(default_factory=list)
    recent_action_effectiveness: list[dict[str, Any]] = Field(default_factory=list)
    recommended_adjustments: list[dict[str, Any]] = Field(default_factory=list)
    recent_examples: list[dict[str, Any]] = Field(default_factory=list)


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
    auto_publish: bool = False
    publish: ProjectAutomationPublishSettings | None = None
    publish_bindings: list[ProjectAutomationPublishSettings] | None = None


class ProjectAutomationUpdateResponse(BaseModel):
    ok: bool
    project_id: str
    automation: ProjectAutomationSettings
    message: str


class ChapterDetail(BaseModel):
    chapter_number: int
    title: str
    body: str
    char_count: int
    summary: str
    status: str
    version: int = 1


class ChapterReviewIssueInfo(BaseModel):
    rule_name: str
    severity: str
    description: str
    entity_names: list[str] = Field(default_factory=list)
    issue_type: str = ""
    target_scope: str = ""
    issue_group: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    suggested_fix: str = ""


class LintSignalInfo(BaseModel):
    tool: str
    code: str = ""
    severity: str = "warning"
    message: str
    line: int = 0
    column: int = 0
    evidence_refs: list[str] = Field(default_factory=list)


class ChapterReviewDetail(BaseModel):
    project_id: str
    chapter_number: int
    title: str
    status: str
    draft_id: str
    version: int
    body: str
    summary: str
    verdict: str
    issues: list[ChapterReviewIssueInfo] = Field(default_factory=list)
    artifact_meta_path: str = ""
    recommended_action: str = ""
    review_summary: str = ""
    planned_reward_tags: list[str] = Field(default_factory=list)
    delivered_reward_tags: list[str] = Field(default_factory=list)
    experience_scores: dict[str, float] = Field(default_factory=dict)
    review_notes: list[str] = Field(default_factory=list)
    lint_signals: list[LintSignalInfo] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    confirmed_signal_refs: list[str] = Field(default_factory=list)
    reviewer_mode: str = ""
    proposed_design_patch: dict[str, Any] = Field(default_factory=dict)
    rewrite_attempt_count: int = 0
    latest_repair_scope: str = ""
    forced_accept_applied: bool = False
    decision_refs: list[DecisionEventInfo] = Field(default_factory=list)


class ChapterReviewApproveRequest(BaseModel):
    continue_generation: bool = False
    reason: str = ""


class ChapterReviewApproveResponse(BaseModel):
    ok: bool
    project_id: str
    chapter_number: int
    status: str
    message: str
    task_id: str = ""
    frozen_artifact: str = ""


class TropeTemplateInfo(BaseModel):
    template_id: str
    display_name: str = ""
    category: str
    setup_requirement: str = ""
    payoff_shape: str = ""
    risk_flags: list[str] = Field(default_factory=list)
    best_window: str = ""
    recommended_hook_types: list[str] = Field(default_factory=list)


class TropeRegistrySummaryResponse(BaseModel):
    total_count: int = 0
    category_counts: dict[str, int] = Field(default_factory=dict)
    version: str = "starter"
    source: str = "seed"
    is_full_library: bool = False
    validation_errors: list[str] = Field(default_factory=list)


class TropeTemplateValidationRequest(BaseModel):
    templates: list[dict[str, Any]] = Field(default_factory=list)
    require_full: bool = True


class TropeTemplateValidationResponse(BaseModel):
    ok: bool
    total_count: int = 0
    category_counts: dict[str, int] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)


class BandExperienceOverrideRequest(BaseModel):
    scheduled_rewards: list[dict[str, Any]] = Field(default_factory=list)
    curiosity_beats: list[dict[str, Any]] = Field(default_factory=list)
    immersion_anchor_scene_goal: str = ""


class BandExperienceOverrideResponse(BaseModel):
    ok: bool
    project_id: str
    band_id: str
    chapter_start: int
    chapter_end: int
    message: str


class ProvisionalChapterLedgerInfo(BaseModel):
    chapter_number: int
    title: str
    summary: str = ""
    verdict: str
    char_count: int = 0
    artifact_meta_path: str = ""
    draft_blob_path: str = ""
    current_time_label: str = ""
    projected_time_label: str = ""
    state_changes: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    thread_beats: list[dict[str, Any]] = Field(default_factory=list)
    time_advance: dict[str, Any] = Field(default_factory=dict)
    issues: list[dict[str, Any]] = Field(default_factory=list)
    error: str = ""
    created_at: str = ""


class ProvisionalBandDetail(BaseModel):
    project_id: str
    arc_id: str
    band_id: str
    aggregate_verdict: str
    preview_char_count: int = 0
    issue_count: int = 0
    failure_count: int = 0
    artifact_path: str = ""
    chapter_numbers: list[int] = Field(default_factory=list)
    created_at: str = ""
    chapters: list[ProvisionalChapterLedgerInfo] = Field(default_factory=list)


class PublisherPlatformInfo(BaseModel):
    platform_id: str
    display_name: str
    login_url: str
    dashboard_url: str
    publish_url: str
    supported_login_methods: list[str] = Field(default_factory=list)
    supported_actions: list[str] = Field(default_factory=list)
    connected: bool = False
    extension_online: bool = False
    last_heartbeat_at: str = ""
    last_error: str = ""
    extension_client_id: str = ""


class PublisherBookMetaRequest(BaseModel):
    audience: str = ""
    primary_category: str = ""
    theme_tags: list[str] = Field(default_factory=list)
    role_tags: list[str] = Field(default_factory=list)
    plot_tags: list[str] = Field(default_factory=list)
    protagonist_names: list[str] = Field(default_factory=list)
    intro: str = ""


class PublisherUploadJobCreateRequest(BaseModel):
    project_id: str | None = None
    platform: str
    book_name: str
    chapter_title: str
    body: str
    upload_url: str | None = None
    publish: bool = True
    prefer_extension: bool = False
    create_if_missing: bool = False
    book_meta: PublisherBookMetaRequest | None = None


class ProjectChapterPublishRequest(BaseModel):
    platform: str
    chapter_number: int
    book_name: str
    upload_url: str | None = None
    publish: bool = True
    create_if_missing: bool = False
    book_meta: PublisherBookMetaRequest | None = None


class PublisherUploadJobResponse(BaseModel):
    task_kind: str = "upload"
    job_id: str
    project_id: str = ""
    platform: str
    display_name: str
    status: str
    book_name: str
    chapter_title: str
    body: str
    upload_url: str | None = None
    publish: bool
    extension_client_id: str = ""
    current_url: str = ""
    message: str
    error: str = ""
    result_payload: dict[str, Any] = Field(default_factory=dict)
    abort_requested: bool = False
    created_at: str = ""
    updated_at: str = ""
    claimed_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    terminable: bool = False
    deletable: bool = False


class ExtensionBrowserCookie(BaseModel):
    name: str
    value: str = ""
    domain: str = ""
    path: str = "/"
    secure: bool = False
    httpOnly: bool = False
    sameSite: str = "Lax"
    expirationDate: float | None = None


class ExtensionPlatformHeartbeat(BaseModel):
    platform: str
    connected: bool = False
    login_method: str = "scan"
    last_error: str = ""
    cookies: list[ExtensionBrowserCookie] = Field(default_factory=list)
    raw_state: dict[str, Any] = Field(default_factory=dict)


class ExtensionHeartbeatRequest(BaseModel):
    client_id: str
    extension_version: str = ""
    browser_name: str = ""
    browser_version: str = ""
    backend_base_url: str = ""
    platforms: list[ExtensionPlatformHeartbeat] = Field(default_factory=list)


class ExtensionHeartbeatResponse(BaseModel):
    ok: bool
    message: str
    server_time: str


class ExtensionSessionSyncRequest(BaseModel):
    client_id: str
    platform: str
    cookies: list[ExtensionBrowserCookie] = Field(default_factory=list)


class ExtensionSessionSyncResponse(BaseModel):
    ok: bool
    message: str
    server_time: str
    cookie_count: int = 0


class ExtensionBrowserSessionResponse(BaseModel):
    platform: str
    client_id: str = ""
    cookie_count: int = 0
    cookies: list[ExtensionBrowserCookie] = Field(default_factory=list)
    synced_at: str = ""
    last_error: str = ""


class ExtensionClaimUploadJobRequest(BaseModel):
    client_id: str
    connected_platforms: list[str] = Field(default_factory=list)


class ExtensionClaimUploadJobResponse(BaseModel):
    found: bool
    job: PublisherUploadJobResponse | None = None


class ExtensionClaimCommentSyncJobRequest(BaseModel):
    client_id: str
    connected_platforms: list[str] = Field(default_factory=list)


class ExtensionClaimCommentSyncJobResponse(BaseModel):
    found: bool
    job: PublisherCommentSyncJobResponse | None = None


class UploadJobResultRequest(BaseModel):
    client_id: str
    status: str
    message: str = ""
    current_url: str = ""
    error: str = ""
    result_payload: dict[str, Any] = Field(default_factory=dict)


class CommentSyncJobResultRequest(BaseModel):
    client_id: str
    status: str
    message: str = ""
    error: str = ""
    result_payload: dict[str, Any] = Field(default_factory=dict)


class PublisherCommentSyncJobRequest(BaseModel):
    project_id: str = ""
    platform: str
    work_id: str = ""
    work_name: str = ""
    chapter_id: str = ""
    chapter_title: str = ""
    limit: int = 100


class PublisherCommentSyncJobResponse(BaseModel):
    job_id: str
    project_id: str = ""
    platform: str
    status: str
    work_id: str = ""
    work_name: str = ""
    chapter_id: str = ""
    chapter_title: str = ""
    limit: int
    created_at: str


class PublisherRawCommentInput(BaseModel):
    remote_comment_id: str
    work_id: str = ""
    work_name: str = ""
    chapter_id: str = ""
    chapter_title: str = ""
    author_id: str = ""
    author_name: str = ""
    body: str = ""
    parent_remote_comment_id: str = ""
    created_at: str = ""
    like_count: int = 0
    reply_count: int = 0
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class ExtensionCommentsBatchRequest(BaseModel):
    client_id: str
    platform: str
    job_id: str = ""
    comments: list[PublisherRawCommentInput] = Field(default_factory=list)


class ExtensionCommentsBatchResponse(BaseModel):
    ok: bool
    message: str
    inserted: int
    updated: int
    synced_at: str


ProjectAutomationPublishSettings.model_rebuild()
