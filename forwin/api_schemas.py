from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from forwin.config import DEFAULT_MINIMAX_BASE_URL, DEFAULT_MINIMAX_MODEL


class GenerateRequest(BaseModel):
    premise: str
    genre: str = "玄幻"
    num_chapters: int = 3
    api_key: str | None = None
    base_url: str = DEFAULT_MINIMAX_BASE_URL
    model: str = DEFAULT_MINIMAX_MODEL
    operation_mode: str | None = None
    freeze_failed_candidates: bool | None = None


class LLMSettingsRequest(BaseModel):
    api_key: str = ""
    base_url: str = DEFAULT_MINIMAX_BASE_URL
    model: str = DEFAULT_MINIMAX_MODEL
    operation_mode: str = "blackbox"
    freeze_failed_candidates: bool = True


class LLMSettingsResponse(BaseModel):
    has_api_key: bool
    base_url: str
    model: str
    operation_mode: str = "blackbox"
    freeze_failed_candidates: bool = True
    message: str = ""


class TaskResponse(BaseModel):
    task_id: str
    status: str
    project_id: str | None = None
    error: str | None = None
    message: str = ""
    failed_chapters: list[int] = Field(default_factory=list)
    paused_chapters: list[int] = Field(default_factory=list)
    frozen_artifacts: list[str] = Field(default_factory=list)


class ProjectArcSnapshotFields(BaseModel):
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


class ProjectSummary(ProjectArcSnapshotFields):
    id: str
    title: str
    genre: str
    premise: str = ""
    created_at: str = ""
    latest_stage: str = ""
    pacing_verdict: str = ""
    pacing_summary: str = ""
    last_replan_status: str = ""
    last_replan_strategy: str = ""
    last_replan_reason: str = ""
    current_time_label: str = ""
    world_pressure_level: str = ""
    world_pressure_summary: str = ""
    chapters: list[dict[str, object]] = Field(default_factory=list)


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


class ProjectDetail(ProjectArcSnapshotFields):
    id: str
    title: str
    premise: str
    genre: str
    setting_summary: str
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


class ChapterReviewApproveRequest(BaseModel):
    continue_generation: bool = False


class ChapterReviewApproveResponse(BaseModel):
    ok: bool
    project_id: str
    chapter_number: int
    status: str
    message: str
    task_id: str = ""
    frozen_artifact: str = ""


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
    platform: str
    book_name: str
    chapter_title: str
    body: str
    upload_url: str | None = None
    publish: bool = True
    prefer_extension: bool = False
    create_if_missing: bool = False
    book_meta: PublisherBookMetaRequest | None = None


class PublisherUploadJobResponse(BaseModel):
    job_id: str
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
    created_at: str = ""
    claimed_at: str = ""
    started_at: str = ""
    finished_at: str = ""


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


class ExtensionClaimUploadJobRequest(BaseModel):
    client_id: str
    connected_platforms: list[str] = Field(default_factory=list)


class ExtensionClaimUploadJobResponse(BaseModel):
    found: bool
    job: PublisherUploadJobResponse | None = None


class UploadJobResultRequest(BaseModel):
    client_id: str
    status: str
    message: str = ""
    current_url: str = ""
    error: str = ""
    result_payload: dict[str, Any] = Field(default_factory=dict)


class PublisherCommentSyncJobRequest(BaseModel):
    platform: str
    work_id: str = ""
    work_name: str = ""
    chapter_id: str = ""
    chapter_title: str = ""
    limit: int = 100


class PublisherCommentSyncJobResponse(BaseModel):
    job_id: str
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
