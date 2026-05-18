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
    preferred_client_state: dict[str, Any] = Field(default_factory=dict)
    latest_client_state: dict[str, Any] = Field(default_factory=dict)
    global_platform_state: dict[str, Any] = Field(default_factory=dict)
    browser_session_state: dict[str, Any] = Field(default_factory=dict)
    fallback_available: bool = False
    fallback_client_id: str = ""


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
    raw_state: dict[str, Any] = Field(default_factory=dict)


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


class PublisherBrowserSessionSummaryResponse(BaseModel):
    platform: str
    client_id: str = ""
    cookie_count: int = 0
    cookie_names: list[str] = Field(default_factory=list)
    cookies_redacted: bool = True
    synced_at: str = ""
    last_error: str = ""
    connected: bool = False


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


__all__ = [
    'PublisherPlatformInfo',
    'PublisherBookMetaRequest',
    'PublisherUploadJobCreateRequest',
    'ProjectChapterPublishRequest',
    'PublisherUploadJobResponse',
    'ExtensionBrowserCookie',
    'ExtensionPlatformHeartbeat',
    'ExtensionHeartbeatRequest',
    'ExtensionHeartbeatResponse',
    'ExtensionSessionSyncRequest',
    'ExtensionSessionSyncResponse',
    'ExtensionBrowserSessionResponse',
    'PublisherBrowserSessionSummaryResponse',
    'ExtensionClaimUploadJobRequest',
    'ExtensionClaimUploadJobResponse',
    'ExtensionClaimCommentSyncJobRequest',
    'ExtensionClaimCommentSyncJobResponse',
    'UploadJobResultRequest',
    'CommentSyncJobResultRequest',
    'PublisherCommentSyncJobRequest',
    'PublisherCommentSyncJobResponse',
    'PublisherRawCommentInput',
    'ExtensionCommentsBatchRequest',
    'ExtensionCommentsBatchResponse',
]
