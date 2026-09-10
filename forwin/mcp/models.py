from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

StageKey = Literal[
    "brief",
    "world",
    "map",
    "story_engine",
    "book_blueprint",
    "bootstrap",
]

STAGE_KEY_ORDER: tuple[str, ...] = (
    "brief",
    "world",
    "map",
    "story_engine",
    "book_blueprint",
    "bootstrap",
)


class StageStateView(BaseModel):
    stage_key: str
    status: str = "todo"
    locked: bool = False
    updated_at: str = ""
    last_trace_id: str = ""


class BlockingReasonView(BaseModel):
    code: str = ""
    message: str = ""
    chapter_number: int = 0
    band_id: str = ""
    decision_event_id: str = ""
    detail: str = ""


class DecisionEventView(BaseModel):
    id: str = ""
    project_id: str = ""
    task_id: str = ""
    band_id: str = ""
    chapter_number: int = 0
    scope: str = "project"
    event_family: str = ""
    event_type: str = ""
    actor_type: str = ""
    summary: str = ""
    reason: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    related_object_type: str = ""
    related_object_id: str = ""
    created_at: str = ""


class ProjectDecisionEventsView(BaseModel):
    items: list[DecisionEventView] = Field(default_factory=list)


class GateLedgerMetricView(BaseModel):
    gate_id: str
    gate_versions: list[str] = Field(default_factory=list)
    responsibility_domains: list[str] = Field(default_factory=list)
    opportunities: int | Literal["unknown"] = 0
    evaluations: int = 0
    fires: int = 0
    blocks: int = 0
    pauses: int = 0
    approvals: int = 0
    overrides: int = 0
    post_override_incident_proxy: int = 0
    post_pass_incident_proxy: int = 0
    unknown_legacy_count: int = 0
    fire_rate: float | None = None
    block_rate: float | None = None
    override_rate: float | None = None


class GateLedgerReportView(BaseModel):
    schema_version: Literal[1] = 1
    scope: Literal["project", "band", "cross_project"]
    project_id: str = ""
    band_id: str = ""
    project_count: int = 0
    event_count: int = 0
    checkpoint_count: int = 0
    metrics: list[GateLedgerMetricView] = Field(default_factory=list)


class CostMetricsView(BaseModel):
    attempts: int = 0
    successes: int = 0
    retries: int = 0
    fallbacks: int = 0
    input_chars: int = 0
    output_chars: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    duration_ms: int = 0
    provider_usage_attempts: int = 0
    codex_usage_attempts: int = 0
    estimated_usage_attempts: int = 0
    missing_usage_attempts: int = 0


class CostDimensionMetricView(BaseModel):
    dimension: Literal[
        "project",
        "chapter",
        "band",
        "candidate",
        "task_family",
        "stage_key",
        "model",
        "provider",
    ]
    value: str
    metrics: CostMetricsView = Field(default_factory=CostMetricsView)


class GateCostMetricView(BaseModel):
    gate_id: str
    metrics: CostMetricsView = Field(default_factory=CostMetricsView)


class ManualActionMetricView(BaseModel):
    action_type: str
    actor_type: str
    actor_id: str = ""
    source: str = ""
    count: int = 0
    duration_ms: int = 0
    unknown_duration_count: int = 0


class CostLedgerReportView(BaseModel):
    schema_version: Literal[1] = 1
    project_id: str = ""
    chapter_number: int = 0
    band_id: str = ""
    candidate_id: str = ""
    project_count: int = 0
    trace_count: int = 0
    event_count: int = 0
    totals: CostMetricsView = Field(default_factory=CostMetricsView)
    dimensions: list[CostDimensionMetricView] = Field(default_factory=list)
    gate_costs: list[GateCostMetricView] = Field(default_factory=list)
    manual_action_count: int = 0
    manual_action_duration_ms: int = 0
    unknown_manual_duration_count: int = 0
    manual_actions: list[ManualActionMetricView] = Field(default_factory=list)


class RuleProvenanceEntryView(BaseModel):
    rule_key: str
    summary: str = ""
    scope: Literal["global", "genre_candidate", "project"]
    status: Literal["observing", "active", "suspended", "retired"]
    origin_project_id: str = ""
    origin_event_id: str = ""
    valid_from_chapter: int = 0
    valid_until_chapter: int | None = None
    promotion_evidence: list[str] = Field(default_factory=list)
    code_owner: str = ""


class RuleRecommendationView(BaseModel):
    rule_key: str
    action: Literal[
        "activate",
        "suspend",
        "retire",
        "global_promotion_recommended",
    ]
    project_ids: list[str] = Field(default_factory=list)
    current_status: Literal["observing", "active", "suspended", "retired"] | None = None
    recommended_status: Literal["observing", "active", "suspended", "retired"] | None = None
    reason: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    required_actions: list[str] = Field(default_factory=list)


class RuleProvenanceReportView(BaseModel):
    schema_version: Literal[1] = 1
    project_id: str = ""
    project_count: int = 0
    global_code_backed_rules: list[RuleProvenanceEntryView] = Field(
        default_factory=list
    )
    genre_rule_candidates: list[RuleProvenanceEntryView] = Field(
        default_factory=list
    )
    project_rules: list[RuleProvenanceEntryView] = Field(default_factory=list)
    recommendations: list[RuleRecommendationView] = Field(default_factory=list)


class GenerationControlView(BaseModel):
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
    next_gate: str = ""
    blocking_reason: BlockingReasonView = Field(default_factory=BlockingReasonView)


class ChapterSummaryView(BaseModel):
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


class ChapterDetailView(ChapterSummaryView):
    chapter_plan_id: str = ""
    active_commit_id: str = ""
    candidate_id: str = ""
    body_sha256: str = ""
    book_revision: int = 0
    acceptance_revision: int = 0
    body: str = ""
    version: int = 1
    residual_review_issues: list[dict[str, Any]] = Field(default_factory=list)


class ChapterListView(BaseModel):
    chapters: list[ChapterSummaryView] = Field(default_factory=list)


class ProjectView(BaseModel):
    id: str
    title: str
    genre: str
    premise: str = ""
    setting_summary: str = ""
    creation_status: str = "creating"
    active_genesis_revision_id: str = ""
    can_start_writing: bool = False
    target_total_chapters: int = 0
    materialized_chapter_count: int = 0
    chapter_count: int = 0
    generated_chapter_count: int = 0
    accepted_chapter_count: int = 0
    needs_review_chapter_count: int = 0
    gate_delegate: str = "human"
    runtime_policy_version: int = 0
    book_revision: int = 0
    latest_stage: str = ""
    next_gate: str = ""
    genesis_stage_overview: list[StageStateView] = Field(default_factory=list)
    generation_control: GenerationControlView = Field(default_factory=GenerationControlView)
    blocking_reason: BlockingReasonView = Field(default_factory=BlockingReasonView)
    chapters: list[ChapterSummaryView] = Field(default_factory=list)


class ProjectListView(BaseModel):
    projects: list[ProjectView] = Field(default_factory=list)


class PromptTraceSummaryView(BaseModel):
    id: str
    trace_scope: str = ""
    stage_key: str = ""
    template_id: str = ""
    created_at: str = ""


class GenesisView(BaseModel):
    project_id: str
    creation_status: str = "creating"
    active_genesis_revision_id: str = ""
    revision: int = 1
    can_start_writing: bool = False
    stage_states: list[StageStateView] = Field(default_factory=list)
    pack: dict[str, Any] = Field(default_factory=dict)
    prompt_traces: list[PromptTraceSummaryView] = Field(default_factory=list)


class TaskView(BaseModel):
    long_run_mode: str = "daily_serial"
    isolated: bool = False
    capacity_config_version: int = 0
    task_id: str
    status: str = ""
    title: str = ""
    subtitle: str = ""
    project_id: str | None = None
    message: str = ""
    error: str | None = None
    current_stage: str = ""
    requested_chapters: int = 0
    run_until_chapter: int = 0
    current_chapter: int = 0
    completed_chapters: list[int] = Field(default_factory=list)
    failed_chapters: list[int] = Field(default_factory=list)
    paused_chapters: list[int] = Field(default_factory=list)
    pause_requested: bool = False
    lease_owner: str = ""
    lease_expires_at: str = ""
    heartbeat_at: str = ""
    pausable: bool = False
    resumable: bool = False
    terminable: bool = False
    deletable: bool = False
    next_gate: str = ""
    recovery_suggestion: str = ""
    generation_control: GenerationControlView = Field(default_factory=GenerationControlView)
    created_at: str = ""
    updated_at: str = ""


class TaskListView(BaseModel):
    tasks: list[TaskView] = Field(default_factory=list)


class ChapterReviewApproveView(BaseModel):
    ok: bool = True
    project_id: str
    chapter_number: int
    status: str
    candidate_id: str = ""
    book_revision: int = 0
    message: str = ""
    task_id: str = ""
    frozen_artifact: str = ""
    project: ProjectView | None = None
    task: TaskView | None = None


class BandCheckpointView(BaseModel):
    id: str
    project_id: str
    arc_id: str = ""
    band_id: str = ""
    chapter_start: int = 0
    chapter_end: int = 0
    trigger_source: str = ""
    boundary_kind: str = ""
    boundary_chapter: int = 0
    status: str = ""
    summary: str = ""
    reason: str = ""
    issues: list[dict[str, Any]] = Field(default_factory=list)
    decision_refs: list[dict[str, Any]] = Field(default_factory=list)
    related_task_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    resolved_at: str = ""


class ActiveTaskCheckView(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    has_active_generation_task: bool
    active_task_ids: list[str]
    active_count: int
    safe_to_restart: bool
    message: str

    @model_validator(mode="after")
    def validate_public_state(self) -> Self:
        if any(not task_id.strip() for task_id in self.active_task_ids):
            raise ValueError("active task IDs must not be empty")
        if (
            self.active_count != len(self.active_task_ids)
            or self.has_active_generation_task is not (self.active_count > 0)
            or self.safe_to_restart is self.has_active_generation_task
        ):
            raise ValueError("active generation state is inconsistent")
        return self


class WorldModelSnapshotView(BaseModel):
    id: str
    project_id: str
    as_of_chapter: int = 0
    version: int = 1
    status: str = "live"
    source_digest: str = ""
    snapshot: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


class WorldModelPageView(BaseModel):
    id: str
    project_id: str
    page_key: str
    page_type: str = "overview"
    title: str
    vault_path: str = ""
    markdown: str = ""
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    content_hash: str = ""
    revision: int = 1
    status: str = "canon_live"
    as_of_chapter: int = 0
    updated_at: str = ""


class WorldModelConflictView(BaseModel):
    id: str
    project_id: str
    conflict_type: str
    severity: str = "warning"
    subject_key: str = ""
    description: str = ""
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    status: str = "open"
    created_at: str = ""
    resolved_at: str = ""


class WorldModelConflictListView(BaseModel):
    conflicts: list[WorldModelConflictView] = Field(default_factory=list)


class WorldModelExportView(BaseModel):
    ok: bool = True
    project_id: str = ""
    vault_root: str = ""
    exported_count: int = 0
    message: str = ""


class MutationResult(BaseModel):
    ok: bool = True
    message: str = ""
    workspace_url: str = ""
    project: ProjectView | None = None
    genesis: GenesisView | None = None
    task: TaskView | None = None
