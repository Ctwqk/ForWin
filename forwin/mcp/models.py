from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

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
    creation_status: str = "legacy"
    active_genesis_revision_id: str = ""
    can_start_writing: bool = False
    chapter_count: int = 0
    generated_chapter_count: int = 0
    accepted_chapter_count: int = 0
    needs_review_chapter_count: int = 0
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
    task_id: str
    status: str = ""
    title: str = ""
    subtitle: str = ""
    project_id: str | None = None
    message: str = ""
    error: str | None = None
    current_stage: str = ""
    requested_chapters: int = 0
    current_chapter: int = 0
    completed_chapters: list[int] = Field(default_factory=list)
    failed_chapters: list[int] = Field(default_factory=list)
    paused_chapters: list[int] = Field(default_factory=list)
    pause_requested: bool = False
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
    message: str = ""
    task_id: str = ""
    frozen_artifact: str = ""
    project: ProjectView | None = None
    task: TaskView | None = None


class ActiveTaskCheckView(BaseModel):
    has_active_generation_task: bool = False
    active_task_ids: list[str] = Field(default_factory=list)
    active_count: int = 0
    safe_to_restart: bool = True
    message: str = ""


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
