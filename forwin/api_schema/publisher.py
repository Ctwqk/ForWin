from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
    create_if_missing: bool = False
    cover_generation_enabled: bool = True
    cover_confirmation_required: bool = False
    cover_candidate_count: int = 4
    cover_style_hint: str = ""
    auto_cover_upload_enabled: bool = True
    publisher_compliance_required: bool = False
    book_meta: PublisherBookMetaRequest | None = None


class ProjectChapterPublishRequest(BaseModel):
    platform: str
    chapter_number: int
    book_name: str
    upload_url: str | None = None
    publish: bool = True
    create_if_missing: bool = False
    cover_generation_enabled: bool = True
    cover_confirmation_required: bool = False
    cover_candidate_count: int = 4
    cover_style_hint: str = ""
    auto_cover_upload_enabled: bool = True
    publisher_compliance_required: bool = False
    book_meta: PublisherBookMetaRequest | None = None


class PublisherUploadJobResponse(BaseModel):
    task_kind: str = "chapter_upload"
    job_id: str
    project_id: str = ""
    canon_commit_id: str = ""
    candidate_id: str = ""
    chapter_number: int = 0
    idempotency_key: str = ""
    body_sha256: str = ""
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
    model_config = ConfigDict(extra="allow")

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


class ExtensionLoginQrNotifyRequest(BaseModel):
    client_id: str
    platform: str
    current_url: str = ""
    image_data_url: str = Field(max_length=6_000_000)
    source: str = ""
    captured_at: str = ""


class ExtensionLoginQrNotifyResponse(BaseModel):
    ok: bool
    message: str
    server_time: str
    dispatched: bool = False
    disabled: bool = False


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


class _StrictPublisherProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExtensionClaimUploadJobRequest(_StrictPublisherProtocolModel):
    client_id: str = Field(min_length=1, max_length=200)
    connected_platforms: list[str]


class ExtensionChapterUploadInput(_StrictPublisherProtocolModel):
    book_name: str
    chapter_title: str
    body: str
    publish: bool
    create_if_missing: bool = False
    upload_url: str | None = None
    book_meta: PublisherBookMetaRequest | None = None


class ExtensionCoverUploadInput(_StrictPublisherProtocolModel):
    book_name: str
    work_binding_id: str = Field(min_length=1)
    remote_book_id: str = Field(min_length=1)
    cover_asset_id: str = Field(min_length=1)
    file_path: str = Field(min_length=1)
    upload_url: str | None = None
    remote_url: str | None = None


class ExtensionAuditSyncInput(_StrictPublisherProtocolModel):
    book_name: str
    work_binding_id: str = Field(min_length=1)
    remote_book_id: str = Field(min_length=1)
    upload_url: str | None = None
    remote_url: str | None = None


class ExtensionChapterUploadClaimJob(_StrictPublisherProtocolModel):
    job_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    task_kind: Literal["chapter_upload"]
    platform: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input: ExtensionChapterUploadInput


class ExtensionCoverUploadClaimJob(_StrictPublisherProtocolModel):
    job_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    task_kind: Literal["cover_upload"]
    platform: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input: ExtensionCoverUploadInput


class ExtensionAuditSyncClaimJob(_StrictPublisherProtocolModel):
    job_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    task_kind: Literal["audit_sync"]
    platform: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input: ExtensionAuditSyncInput


ExtensionUploadClaimJob = Annotated[
    ExtensionChapterUploadClaimJob
    | ExtensionCoverUploadClaimJob
    | ExtensionAuditSyncClaimJob,
    Field(discriminator="task_kind"),
]


class ExtensionUploadClaimAttempt(_StrictPublisherProtocolModel):
    attempt_id: str = Field(min_length=1)
    attempt_number: int = Field(gt=0)
    lease_epoch: int = Field(gt=0)
    phase: Literal["claimed"]
    lease_expires_at: str = Field(min_length=1)
    heartbeat_interval_seconds: int = Field(gt=0)


class ExtensionUploadClaim(_StrictPublisherProtocolModel):
    execution_mode: Literal["execute", "reconcile"]
    job: ExtensionUploadClaimJob
    attempt: ExtensionUploadClaimAttempt


class ExtensionClaimUploadJobResponse(_StrictPublisherProtocolModel):
    found: bool
    server_time: str = Field(min_length=1)
    retry_after_seconds: int = Field(ge=0)
    claim: ExtensionUploadClaim | None = None

    @model_validator(mode="after")
    def validate_found_claim(self):
        if self.found != (self.claim is not None):
            raise ValueError("found must match claim presence")
        return self


class ExtensionClaimCommentSyncJobRequest(BaseModel):
    client_id: str
    connected_platforms: list[str] = Field(default_factory=list)


class ExtensionClaimCommentSyncJobResponse(BaseModel):
    found: bool
    job: PublisherCommentSyncJobResponse | None = None


class _AttemptFenceRequest(_StrictPublisherProtocolModel):
    client_id: str = Field(min_length=1, max_length=200)
    lease_epoch: int = Field(gt=0)


class UploadAttemptHeartbeatRequest(_AttemptFenceRequest):
    pass


class UploadAttemptPhaseRequest(_AttemptFenceRequest):
    phase: Literal["mutation_started", "observation_started"]
    current_url: str = Field(default="", max_length=4000)


class ExtensionChapterUploadResultDetails(_StrictPublisherProtocolModel):
    pass


class ExtensionCoverUploadResultDetails(_StrictPublisherProtocolModel):
    cover_state: Literal["uploaded", "under_review", "approved", "rejected"]
    audit_state: Literal["", "unknown", "under_review", "approved", "rejected"] = ""
    platform_message: str = Field(default="", max_length=1000)


class ExtensionAuditWorkObservation(_StrictPublisherProtocolModel):
    work_binding_id: str = Field(min_length=1, max_length=500)
    remote_book_id: str = Field(min_length=1, max_length=500)
    remote_url: str = Field(default="", max_length=4000)
    audit_state: Literal["unknown", "under_review", "approved", "rejected"]
    official_status: str = Field(default="", max_length=500)
    platform_message: str = Field(default="", max_length=1000)


class ExtensionAuditChapterObservation(_StrictPublisherProtocolModel):
    chapter_number: int = Field(ge=0)
    chapter_title: str = Field(default="", max_length=500)
    remote_chapter_id: str = Field(default="", max_length=500)
    remote_chapter_url: str = Field(default="", max_length=4000)
    publish_state: str = Field(default="", max_length=200)
    audit_state: Literal["unknown", "under_review", "approved", "rejected"]
    audit_reason: str = Field(default="", max_length=1000)
    word_count: int = Field(default=0, ge=0)


class ExtensionAuditCoverObservation(_StrictPublisherProtocolModel):
    cover_state: Literal["unknown", "uploaded", "under_review", "approved", "rejected"]


class ExtensionAuditMilestoneObservation(_StrictPublisherProtocolModel):
    milestone_type: str = Field(min_length=1, max_length=200)
    state: str = Field(default="open", max_length=200)
    message: str = Field(default="", max_length=1000)


class ExtensionAuditSyncResultDetails(_StrictPublisherProtocolModel):
    work: ExtensionAuditWorkObservation
    chapters: list[ExtensionAuditChapterObservation] = Field(max_length=1000)
    cover: ExtensionAuditCoverObservation
    milestones: list[ExtensionAuditMilestoneObservation] = Field(max_length=100)


ExtensionUploadResultDetails = (
    ExtensionChapterUploadResultDetails
    | ExtensionCoverUploadResultDetails
    | ExtensionAuditSyncResultDetails
)


class UploadAttemptResultRequest(_AttemptFenceRequest):
    outcome: Literal["succeeded", "failed", "cancelled"]
    message: str = Field(default="", max_length=2000)
    current_url: str = Field(default="", max_length=4000)
    error_code: str = Field(default="", max_length=200)
    error_message: str = Field(default="", max_length=8000)
    details: ExtensionUploadResultDetails = Field(
        default_factory=ExtensionChapterUploadResultDetails
    )

    @model_validator(mode="after")
    def validate_failure(self):
        if self.outcome == "failed" and (
            not self.error_code.strip() or not self.error_message.strip()
        ):
            raise ValueError("failed result requires error_code and error_message")
        return self


class PublisherReceiptEvidenceDetails(_StrictPublisherProtocolModel):
    selector: str = Field(default="", max_length=500)
    heading: str = Field(default="", max_length=500)
    matched: bool | None = None
    content_sha256: str = Field(
        default="",
        pattern=r"^(?:|[0-9a-f]{64})$",
    )
    confirmation_text: str = Field(default="", max_length=1000)
    platform_message: str = Field(default="", max_length=1000)
    screenshot_sha256: str = Field(
        default="",
        pattern=r"^(?:|[0-9a-f]{64})$",
    )


class PublisherReconciliationEvidence(_StrictPublisherProtocolModel):
    procedure: str = Field(default="", max_length=200)
    match: str = Field(default="", max_length=500)
    match_basis: list[str] = Field(default_factory=list, max_length=20)
    selector: str = Field(default="", max_length=500)
    matched: bool | None = None
    matched_content_sha256: str = Field(
        default="",
        pattern=r"^(?:|[0-9a-f]{64})$",
    )
    pagination_complete: bool | None = None
    reason: str = Field(default="", max_length=1000)
    platform_message: str = Field(default="", max_length=1000)


class PublisherUploadReceiptEvidence(_StrictPublisherProtocolModel):
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    remote_book_id: str = Field(default="", max_length=500)
    remote_chapter_id: str = Field(default="", max_length=500)
    remote_url: str = Field(default="", max_length=4000)
    official_state: str = Field(min_length=1, max_length=200)
    observed_at: str = Field(min_length=1, max_length=100)
    evidence: PublisherReceiptEvidenceDetails

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: str) -> str:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError("observed_at must be a UTC RFC 3339 timestamp")
        return value

    @model_validator(mode="after")
    def validate_remote_identity(self):
        if not any(
            (
                self.remote_book_id.strip(),
                self.remote_chapter_id.strip(),
                self.remote_url.strip(),
            )
        ):
            raise ValueError("receipt requires stable remote identity")
        if self.evidence.content_sha256 != self.content_sha256:
            raise ValueError("receipt evidence content hash must match receipt content")
        return self


class UploadAttemptReceiptRequest(_AttemptFenceRequest, PublisherUploadReceiptEvidence):
    pass


class UploadAttemptReconcileRequest(_AttemptFenceRequest):
    outcome: Literal["matched", "absent", "indeterminate", "risk_pause"]
    observed_at: str = Field(min_length=1, max_length=100)
    current_url: str = Field(default="", max_length=4000)
    evidence: PublisherReconciliationEvidence
    receipt: PublisherUploadReceiptEvidence | None = None
    error_code: str = Field(default="", max_length=200)
    error_message: str = Field(default="", max_length=8000)

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: str) -> str:
        return PublisherUploadReceiptEvidence.validate_observed_at(value)

    @model_validator(mode="after")
    def validate_receipt(self):
        if self.outcome == "matched" and self.receipt is None:
            raise ValueError("matched reconciliation requires a receipt")
        if self.outcome != "matched" and self.receipt is not None:
            raise ValueError("receipt is only valid for matched reconciliation")
        if (
            self.outcome == "risk_pause"
            and not self.evidence.reason.strip()
            and not self.error_message.strip()
        ):
            raise ValueError("risk_pause requires an evidence reason")
        return self


class PublisherUploadReceiptResponse(_StrictPublisherProtocolModel):
    receipt_id: str
    receipt_key: str
    remote_book_id: str = ""
    remote_chapter_id: str = ""
    remote_url: str = ""
    official_state: str
    content_sha256: str
    observed_at: str


class UploadAttemptStateResponse(_StrictPublisherProtocolModel):
    ok: Literal[True] = True
    server_time: str
    job_id: str
    job_status: Literal[
        "running",
        "pending",
        "reconciling",
        "paused",
        "succeeded",
        "cancelled",
    ]
    attempt_id: str
    attempt_status: Literal[
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "expired",
    ]
    lease_epoch: int = Field(gt=0)
    phase: Literal[
        "claimed",
        "mutation_started",
        "observation_started",
        "receipt_observed",
    ]
    lease_expires_at: str | None = None
    abort_requested: bool
    execution_mode: Literal["execute", "reconcile"]
    next_action: Literal[
        "heartbeat",
        "execute",
        "reconcile",
        "submit_result",
        "retry_after",
        "stop",
        "operator_review",
    ]


class UploadAttemptResultResponse(UploadAttemptStateResponse):
    disposition: Literal["applied", "idempotent"] = "applied"
    available_at: str | None = None
    reconcile_after: str | None = None
    pause_reason: str = ""


class UploadAttemptReceiptResponse(UploadAttemptStateResponse):
    disposition: Literal["created", "duplicate", "late_applied"]
    receipt: PublisherUploadReceiptResponse


class UploadAttemptReconcileResponse(UploadAttemptResultResponse):
    outcome: Literal["matched", "absent", "indeterminate", "risk_pause"]
    receipt: PublisherUploadReceiptResponse | None = None


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


class PublisherWorkBindingResponse(BaseModel):
    id: str
    project_id: str = ""
    platform: str
    book_name: str = ""
    remote_book_id: str = ""
    remote_url: str = ""
    audit_state: str = "unknown"
    audit_reason: str = ""
    platform_status: str = ""
    cover_asset_id: str = ""
    cover_state: str = "none"
    last_synced_at: str = ""
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


class PublisherChapterBindingResponse(BaseModel):
    id: str
    work_binding_id: str
    project_id: str = ""
    platform: str
    chapter_number: int = 0
    chapter_title: str = ""
    remote_chapter_id: str = ""
    remote_url: str = ""
    publish_state: str = "unknown"
    audit_state: str = "unknown"
    audit_reason: str = ""
    word_count: int = 0
    last_synced_at: str = ""
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


class PublisherCoverAssetResponse(BaseModel):
    id: str
    project_id: str = ""
    work_binding_id: str = ""
    source: str = ""
    prompt: str = ""
    source_meta: dict[str, Any] = Field(default_factory=dict)
    status: str = ""
    selection_state: str = ""
    score: float = 0.0
    score_reasons: list[Any] = Field(default_factory=list)
    width: int = 0
    height: int = 0
    file_size_bytes: int = 0
    file_path: str = ""
    mime_type: str = ""
    platform_validation: dict[str, Any] = Field(default_factory=dict)
    minimax_request_id: str = ""
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class PublisherCoverGenerateRequest(BaseModel):
    project_id: str = ""
    platform: str
    book_name: str
    book_meta: PublisherBookMetaRequest | None = None
    cover_candidate_count: int = 4
    cover_style_hint: str = ""
    cover_confirmation_required: bool = False


class PublisherCoverSelectRequest(BaseModel):
    cover_asset_id: str


class PublisherCoverUploadRequest(BaseModel):
    cover_asset_id: str


class PublisherAuditSyncRequest(BaseModel):
    project_id: str = ""
    platform: str
    work_binding_id: str = ""
    book_name: str = ""


class PublisherPreflightRequest(BaseModel):
    platform: str
    book_name: str
    chapter_title: str = ""
    body: str = ""
    create_if_missing: bool = False
    book_meta: PublisherBookMetaRequest | None = None


class PublisherPreflightResponse(BaseModel):
    ok: bool
    blocking: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    platform_meta: dict[str, Any] = Field(default_factory=dict)
    requires_reviewer: bool = False
    publisher_compliance: dict[str, Any] = Field(default_factory=dict)


class PublisherLoginQrOneShotRequest(BaseModel):
    platform: str
    webhook_url: str = Field(max_length=2000)
    ttl_seconds: int = 300
    max_dispatches: int = 1


class PublisherLoginQrOneShotResponse(BaseModel):
    ok: bool
    message: str
    server_time: str
    platform: str
    expires_at: str
    allowed_until_ms: int
    remaining_dispatches: int
    login_qr_notifications_allowed: bool


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
    "PublisherPlatformInfo",
    "PublisherBookMetaRequest",
    "PublisherUploadJobCreateRequest",
    "ProjectChapterPublishRequest",
    "PublisherUploadJobResponse",
    "ExtensionBrowserCookie",
    "ExtensionPlatformHeartbeat",
    "ExtensionHeartbeatRequest",
    "ExtensionHeartbeatResponse",
    "ExtensionLoginQrNotifyRequest",
    "ExtensionLoginQrNotifyResponse",
    "ExtensionSessionSyncRequest",
    "ExtensionSessionSyncResponse",
    "ExtensionBrowserSessionResponse",
    "PublisherBrowserSessionSummaryResponse",
    "ExtensionClaimUploadJobRequest",
    "ExtensionClaimUploadJobResponse",
    "ExtensionUploadClaim",
    "ExtensionUploadClaimAttempt",
    "ExtensionChapterUploadClaimJob",
    "ExtensionCoverUploadClaimJob",
    "ExtensionAuditSyncClaimJob",
    "UploadAttemptHeartbeatRequest",
    "UploadAttemptPhaseRequest",
    "ExtensionClaimCommentSyncJobRequest",
    "ExtensionClaimCommentSyncJobResponse",
    "UploadAttemptResultRequest",
    "PublisherUploadReceiptEvidence",
    "UploadAttemptReceiptRequest",
    "UploadAttemptReconcileRequest",
    "UploadAttemptStateResponse",
    "UploadAttemptResultResponse",
    "UploadAttemptReceiptResponse",
    "UploadAttemptReconcileResponse",
    "CommentSyncJobResultRequest",
    "PublisherCommentSyncJobRequest",
    "PublisherCommentSyncJobResponse",
    "PublisherWorkBindingResponse",
    "PublisherChapterBindingResponse",
    "PublisherCoverAssetResponse",
    "PublisherCoverGenerateRequest",
    "PublisherCoverSelectRequest",
    "PublisherCoverUploadRequest",
    "PublisherAuditSyncRequest",
    "PublisherPreflightRequest",
    "PublisherPreflightResponse",
    "PublisherLoginQrOneShotRequest",
    "PublisherLoginQrOneShotResponse",
    "PublisherRawCommentInput",
    "ExtensionCommentsBatchRequest",
    "ExtensionCommentsBatchResponse",
]
