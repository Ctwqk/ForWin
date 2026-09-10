from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class PublisherExtensionClient(Base):
    __tablename__ = "publisher_extension_clients"

    client_id: Mapped[str] = mapped_column(String, primary_key=True)
    extension_version: Mapped[str] = mapped_column(String, default="")
    browser_name: Mapped[str] = mapped_column(String, default="")
    browser_version: Mapped[str] = mapped_column(String, default="")
    backend_base_url: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class PublisherConnectionState(Base):
    __tablename__ = "publisher_connection_states"

    platform_id: Mapped[str] = mapped_column(String, primary_key=True)
    extension_client_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("publisher_extension_clients.client_id"),
        default="",
    )
    connected: Mapped[bool] = mapped_column(Boolean, default=False)
    login_method: Mapped[str] = mapped_column(String, default="")
    status_json: Mapped[str] = mapped_column(Text, default="{}")
    last_error: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class PublisherExtensionPlatformState(Base):
    __tablename__ = "publisher_extension_platform_states"
    __table_args__ = (
        Index(
            "ix_publisher_extension_platform_states_platform",
            "platform_id",
            "connected",
        ),
    )

    client_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("publisher_extension_clients.client_id"),
        primary_key=True,
    )
    platform_id: Mapped[str] = mapped_column(String, primary_key=True)
    connected: Mapped[bool] = mapped_column(Boolean, default=False)
    login_method: Mapped[str] = mapped_column(String, default="")
    status_json: Mapped[str] = mapped_column(Text, default="{}")
    last_error: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class PublisherBrowserSession(Base):
    __tablename__ = "publisher_browser_sessions"

    platform_id: Mapped[str] = mapped_column(String, primary_key=True)
    extension_client_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("publisher_extension_clients.client_id"),
        default="",
    )
    cookie_count: Mapped[int] = mapped_column(Integer, default=0)
    cookies_json: Mapped[str] = mapped_column(Text, default="[]")
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


class PublisherBrowserSessionEntry(Base):
    __tablename__ = "publisher_browser_session_entries"
    __table_args__ = (
        Index(
            "ix_publisher_browser_session_entries_platform_synced",
            "platform_id",
            "synced_at",
        ),
    )

    client_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("publisher_extension_clients.client_id"),
        primary_key=True,
    )
    platform_id: Mapped[str] = mapped_column(String, primary_key=True)
    cookie_count: Mapped[int] = mapped_column(Integer, default=0)
    cookies_json: Mapped[str] = mapped_column(Text, default="[]")
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


class PublisherUploadJob(Base):
    __tablename__ = "publisher_upload_jobs"
    __table_args__ = (
        Index(
            "ix_publisher_upload_jobs_task_status",
            "task_kind",
            "status",
            "platform_id",
        ),
        Index(
            "ux_publisher_upload_jobs_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key <> ''"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, default="")
    canon_commit_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("canon_commit_records.id"),
        nullable=True,
    )
    candidate_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
        server_default="",
    )
    chapter_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    idempotency_key: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
        server_default="",
    )
    platform_id: Mapped[str] = mapped_column(String, nullable=False)
    task_kind: Mapped[str] = mapped_column(
        String, default="chapter_upload", nullable=False
    )
    status: Mapped[str] = mapped_column(String, default="pending")
    book_name: Mapped[str] = mapped_column(String, default="")
    chapter_title: Mapped[str] = mapped_column(String, default="")
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    body_sha256: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
        server_default="",
    )
    upload_url: Mapped[str] = mapped_column(String, default="")
    publish: Mapped[bool] = mapped_column(Boolean, default=True)
    abort_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    extension_client_id: Mapped[str] = mapped_column(String, default="")
    current_attempt_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
        server_default="",
    )
    available_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reconcile_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pause_reason: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
        server_default="",
    )
    current_url: Mapped[str] = mapped_column(String, default="")
    result_message: Mapped[str] = mapped_column(Text, default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    result_payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


class PublisherUploadAttempt(Base):
    __tablename__ = "publisher_upload_attempts"
    __table_args__ = (
        UniqueConstraint(
            "upload_job_id",
            "attempt_number",
            name="uq_publisher_upload_attempts_job_number",
        ),
        Index(
            "ix_publisher_upload_attempts_job_status",
            "upload_job_id",
            "status",
            "attempt_number",
        ),
        Index(
            "ix_publisher_upload_attempts_status_lease_expires",
            "status",
            "lease_expires_at",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    upload_job_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("publisher_upload_jobs.id"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_kind: Mapped[str] = mapped_column(String, nullable=False)
    worker_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
        server_default="",
    )
    lease_epoch: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="pending",
        server_default="pending",
    )
    phase: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
        server_default="",
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    content_sha256: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
        server_default="",
    )
    error_code: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
        server_default="",
    )
    error_message: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
        server_default="",
    )
    result_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="{}",
        server_default="{}",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=func.now(),
        onupdate=func.now(),
    )


class PublisherUploadReceipt(Base):
    __tablename__ = "publisher_upload_receipts"
    __table_args__ = (
        UniqueConstraint(
            "receipt_key",
            name="uq_publisher_upload_receipts_receipt_key",
        ),
        Index(
            "ix_publisher_upload_receipts_job_created",
            "upload_job_id",
            "created_at",
        ),
        Index(
            "ix_publisher_upload_receipts_platform_remote",
            "platform_id",
            "remote_book_id",
            "remote_chapter_id",
        ),
        Index(
            "ix_publisher_upload_receipts_idempotency_key",
            "idempotency_key",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    upload_job_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("publisher_upload_jobs.id"),
        nullable=False,
    )
    upload_attempt_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("publisher_upload_attempts.id"),
        nullable=False,
    )
    receipt_key: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
        server_default="",
    )
    platform_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
        server_default="",
    )
    remote_book_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
        server_default="",
    )
    remote_chapter_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
        server_default="",
    )
    remote_url: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
        server_default="",
    )
    official_state: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
        server_default="",
    )
    content_sha256: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
        server_default="",
    )
    evidence_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="{}",
        server_default="{}",
    )
    source: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
        server_default="",
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=func.now(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=func.now(),
    )


class PublisherOperatorAction(Base):
    __tablename__ = "publisher_operator_actions"
    __table_args__ = (
        UniqueConstraint(
            "upload_job_id",
            "action",
            "pause_token",
            name="uq_publisher_operator_actions_job_action_pause",
        ),
        Index(
            "ix_publisher_operator_actions_job_created",
            "upload_job_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    upload_job_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("publisher_upload_jobs.id"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String, nullable=False)
    pause_token: Mapped[str] = mapped_column(String, nullable=False)
    actor_id: Mapped[str] = mapped_column(String, nullable=False)
    auth_method: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    old_state_json: Mapped[str] = mapped_column(Text, nullable=False)
    new_state_json: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=func.now(),
    )


class PublisherWorkBinding(Base):
    __tablename__ = "publisher_work_bindings"
    __table_args__ = (
        Index(
            "ix_publisher_work_bindings_project_platform",
            "project_id",
            "platform_id",
        ),
        Index(
            "ux_publisher_work_bindings_project_platform",
            "project_id",
            "platform_id",
            unique=True,
            postgresql_where=text("project_id <> ''"),
        ),
        Index(
            "ix_publisher_work_bindings_platform_book",
            "platform_id",
            "book_name",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, default="")
    platform_id: Mapped[str] = mapped_column(String, nullable=False)
    book_name: Mapped[str] = mapped_column(String, default="")
    remote_book_id: Mapped[str] = mapped_column(String, default="")
    remote_url: Mapped[str] = mapped_column(String, default="")
    audit_state: Mapped[str] = mapped_column(String, default="unknown")
    audit_reason: Mapped[str] = mapped_column(Text, default="")
    platform_status: Mapped[str] = mapped_column(String, default="")
    cover_asset_id: Mapped[str] = mapped_column(String, default="")
    cover_state: Mapped[str] = mapped_column(String, default="none")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw_payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


class PublisherChapterBinding(Base):
    __tablename__ = "publisher_chapter_bindings"
    __table_args__ = (
        Index(
            "ix_publisher_chapter_bindings_work",
            "work_binding_id",
            "chapter_number",
        ),
        Index(
            "ux_publisher_chapter_bindings_work_number",
            "work_binding_id",
            "chapter_number",
            unique=True,
            postgresql_where=text("chapter_number > 0"),
        ),
        Index(
            "ix_publisher_chapter_bindings_project_platform",
            "project_id",
            "platform_id",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    work_binding_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("publisher_work_bindings.id"),
        nullable=False,
    )
    project_id: Mapped[str] = mapped_column(String, default="")
    platform_id: Mapped[str] = mapped_column(String, nullable=False)
    chapter_number: Mapped[int] = mapped_column(Integer, default=0)
    chapter_title: Mapped[str] = mapped_column(String, default="")
    remote_chapter_id: Mapped[str] = mapped_column(String, default="")
    remote_url: Mapped[str] = mapped_column(String, default="")
    publish_state: Mapped[str] = mapped_column(String, default="unknown")
    audit_state: Mapped[str] = mapped_column(String, default="unknown")
    audit_reason: Mapped[str] = mapped_column(Text, default="")
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw_payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


class PublisherCoverAsset(Base):
    __tablename__ = "publisher_cover_assets"
    __table_args__ = (
        Index("ix_publisher_cover_assets_project", "project_id"),
        Index("ix_publisher_cover_assets_work", "work_binding_id"),
        Index(
            "ux_publisher_cover_assets_selected_work",
            "work_binding_id",
            unique=True,
            postgresql_where=text(
                "work_binding_id <> '' AND selection_state IN ('selected', 'approved')"
            ),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, default="")
    work_binding_id: Mapped[str] = mapped_column(String, default="")
    source: Mapped[str] = mapped_column(String, default="minimax")
    prompt: Mapped[str] = mapped_column(Text, default="")
    source_meta_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String, default="generated")
    selection_state: Mapped[str] = mapped_column(String, default="candidate")
    score: Mapped[float] = mapped_column(Float, default=0.0)
    score_reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    file_path: Mapped[str] = mapped_column(Text, default="")
    mime_type: Mapped[str] = mapped_column(String, default="")
    platform_validation_json: Mapped[str] = mapped_column(Text, default="{}")
    minimax_request_id: Mapped[str] = mapped_column(String, default="")
    raw_payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


class PublisherMilestone(Base):
    __tablename__ = "publisher_milestones"
    __table_args__ = (
        Index("ix_publisher_milestones_work_state", "work_binding_id", "state"),
        Index("ix_publisher_milestones_type", "milestone_type"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    work_binding_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("publisher_work_bindings.id"),
        nullable=False,
    )
    milestone_type: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, default="open")
    message: Mapped[str] = mapped_column(Text, default="")
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


class PublisherCommentSyncJob(Base):
    __tablename__ = "publisher_comment_sync_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, default="")
    platform_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="pending")
    work_id: Mapped[str] = mapped_column(String, default="")
    work_name: Mapped[str] = mapped_column(String, default="")
    chapter_id: Mapped[str] = mapped_column(String, default="")
    chapter_title: Mapped[str] = mapped_column(String, default="")
    limit: Mapped[int] = mapped_column(Integer, default=100)
    extension_client_id: Mapped[str] = mapped_column(String, default="")
    result_summary_json: Mapped[str] = mapped_column(Text, default="{}")
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PublisherRawComment(Base):
    __tablename__ = "publisher_raw_comments"
    __table_args__ = (
        UniqueConstraint(
            "platform_id",
            "source_scope",
            "work_id",
            "remote_comment_id",
            name="uq_publisher_raw_comments_scoped_remote",
        ),
        Index("ix_publisher_raw_comments_work_name", "work_name"),
        Index("ix_publisher_raw_comments_project", "project_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, default="")
    platform_id: Mapped[str] = mapped_column(String, nullable=False)
    active_analysis_id: Mapped[str] = mapped_column(
        String, default="", server_default=""
    )
    source_scope: Mapped[str] = mapped_column(String, default=new_id, nullable=False)
    account_id: Mapped[str] = mapped_column(String, default="", server_default="")
    work_binding_id: Mapped[str] = mapped_column(String, default="", server_default="")
    source_status: Mapped[str] = mapped_column(
        String, default="unknown", server_default="unknown"
    )
    source_chapter_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_chapter_plan_id: Mapped[str] = mapped_column(
        String, default="", server_default=""
    )
    source_canon_commit_id: Mapped[str] = mapped_column(
        String, default="", server_default=""
    )
    source_publication_id: Mapped[str] = mapped_column(
        String, default="", server_default=""
    )
    content_sha256: Mapped[str] = mapped_column(
        String(64), default="", server_default=""
    )
    source_sha256: Mapped[str] = mapped_column(
        String(64), default="", server_default=""
    )
    observed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    remote_comment_id: Mapped[str] = mapped_column(String, nullable=False)
    work_id: Mapped[str] = mapped_column(String, default="")
    work_name: Mapped[str] = mapped_column(String, default="")
    chapter_id: Mapped[str] = mapped_column(String, default="")
    chapter_title: Mapped[str] = mapped_column(String, default="")
    author_id: Mapped[str] = mapped_column(String, default="")
    author_name: Mapped[str] = mapped_column(String, default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    parent_remote_comment_id: Mapped[str] = mapped_column(String, default="")
    remote_created_at: Mapped[str] = mapped_column(String, default="")
    like_count: Mapped[int] = mapped_column(Integer, default=0)
    reply_count: Mapped[int] = mapped_column(Integer, default=0)
    raw_payload_json: Mapped[str] = mapped_column(Text, default="{}")
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


class CommentAnalysisRecord(Base):
    """One completion/retry state for an exact comment body and analyzer version."""

    __tablename__ = "comment_analysis_records"
    __table_args__ = (
        UniqueConstraint(
            "source_comment_id",
            "content_sha256",
            "source_sha256",
            "analyzer_version",
            name="uq_comment_analysis_version",
        ),
        Index("ix_comment_analysis_pending", "status", "next_retry_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    source_comment_id: Mapped[str] = mapped_column(
        String, ForeignKey("publisher_raw_comments.id"), nullable=False
    )
    project_id: Mapped[str] = mapped_column(String, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    analyzer_version: Mapped[str] = mapped_column(String, nullable=False)
    input_body: Mapped[str] = mapped_column(Text, nullable=False)
    source_identity_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, default="pending", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    signal_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    generation_chapter_number: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class CommentSignalCandidate(Base):
    __tablename__ = "comment_signal_candidates"
    __table_args__ = (
        Index("ix_comment_signals_source", "source_comment_id"),
        Index("ix_comment_signals_project_type", "project_id", "signal_type"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    source_comment_id: Mapped[str] = mapped_column(
        String, ForeignKey("publisher_raw_comments.id"), nullable=False
    )
    analysis_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("comment_analysis_records.id"), nullable=True
    )
    direction: Mapped[str] = mapped_column(
        String, default="unknown", server_default="unknown"
    )
    signal_type: Mapped[str] = mapped_column(String, nullable=False)
    target_type: Mapped[str] = mapped_column(String, default="")
    target_name: Mapped[str] = mapped_column(String, default="")
    severity: Mapped[int] = mapped_column(Integer, default=1)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    evidence_span: Mapped[str] = mapped_column(Text, default="")
    signal_level: Mapped[str] = mapped_column(String, default="noise")
    chapter_number: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class SignalWindowAggregate(Base):
    __tablename__ = "signal_window_aggregates"
    __table_args__ = (
        Index(
            "uq_signal_aggregate_evidence",
            "project_id",
            "aggregation_version",
            "evidence_sha256",
            unique=True,
            postgresql_where=text("evidence_sha256 <> ''"),
        ),
        Index(
            "ix_signal_window_agg_project_key_window",
            "project_id",
            "signal_key",
            "window_type",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    aggregation_version: Mapped[str] = mapped_column(
        String, default="", server_default=""
    )
    evidence_sha256: Mapped[str] = mapped_column(
        String(64), default="", server_default=""
    )
    provenance_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default="{}"
    )
    analyzed_comment_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    known_author_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    unknown_author_comment_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    source_qualified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    signal_key: Mapped[str] = mapped_column(String, nullable=False)
    direction: Mapped[str] = mapped_column(
        String, default="unknown", server_default="unknown"
    )
    signal_type: Mapped[str] = mapped_column(String, default="")
    target_type: Mapped[str] = mapped_column(String, default="")
    target_name: Mapped[str] = mapped_column(String, default="")
    window_type: Mapped[str] = mapped_column(String, default="short")
    window_chapter_start: Mapped[int] = mapped_column(Integer, default=0)
    window_chapter_end: Mapped[int] = mapped_column(Integer, default=0)
    hit_comment_count: Mapped[int] = mapped_column(Integer, default=0)
    unique_user_count: Mapped[int] = mapped_column(Integer, default=0)
    total_comment_count: Mapped[int] = mapped_column(Integer, default=0)
    reader_estimate: Mapped[int] = mapped_column(Integer, default=0)
    reader_tier: Mapped[int] = mapped_column(Integer, default=0)
    estimation_method: Mapped[str] = mapped_column(String, default="comment_proxy")
    scale_confidence: Mapped[float] = mapped_column(Float, default=0.35)
    max_severity: Mapped[int] = mapped_column(Integer, default=0)
    avg_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    signal_level: Mapped[str] = mapped_column(String, default="noise")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ReaderScaleSnapshot(Base):
    __tablename__ = "reader_scale_snapshots"
    __table_args__ = (
        Index("ix_reader_scale_project_chapter", "project_id", "chapter_number"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    chapter_number: Mapped[int] = mapped_column(Integer, default=0)
    reader_estimate: Mapped[int] = mapped_column(Integer, default=0)
    estimation_method: Mapped[str] = mapped_column(String, default="comment_proxy")
    tier: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class FeedbackActionRecord(Base):
    __tablename__ = "feedback_action_records"
    __table_args__ = (
        Index("ix_feedback_actions_project_key", "project_id", "signal_key"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    signal_key: Mapped[str] = mapped_column(String, nullable=False)
    signal_type: Mapped[str] = mapped_column(String, default="")
    action_type: Mapped[str] = mapped_column(String, default="")
    direction: Mapped[str] = mapped_column(String, default="unknown")
    aggregate_id: Mapped[str] = mapped_column(String, default="")
    aggregate_evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    source_qualified: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String, default="proposed")
    selected_at_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    selected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    target_chapter_start: Mapped[int] = mapped_column(Integer, default=0)
    target_chapter_end: Mapped[int] = mapped_column(Integer, default=0)
    hint_valid_from_chapter: Mapped[int] = mapped_column(Integer, default=0)
    hint_expires_at_chapter: Mapped[int] = mapped_column(Integer, default=0)
    action_payload_json: Mapped[str] = mapped_column(Text, default="{}")
    prompt_inclusions_json: Mapped[str] = mapped_column(Text, default="[]")
    plan_application_json: Mapped[str] = mapped_column(Text, default="{}")
    body_observation_json: Mapped[str] = mapped_column(Text, default="{}")
    effect_observation_json: Mapped[str] = mapped_column(Text, default="{}")
    triggered_at_chapter: Mapped[int] = mapped_column(Integer, default=0)
    cooldown_until_chapter: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
