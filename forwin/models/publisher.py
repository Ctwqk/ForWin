from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
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


class PublisherUploadJob(Base):
    __tablename__ = "publisher_upload_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    platform_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="pending")
    book_name: Mapped[str] = mapped_column(String, default="")
    chapter_title: Mapped[str] = mapped_column(String, default="")
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    upload_url: Mapped[str] = mapped_column(String, default="")
    publish: Mapped[bool] = mapped_column(Boolean, default=True)
    extension_client_id: Mapped[str] = mapped_column(String, default="")
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    current_url: Mapped[str] = mapped_column(String, default="")
    result_message: Mapped[str] = mapped_column(Text, default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    result_payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


class PublisherCommentSyncJob(Base):
    __tablename__ = "publisher_comment_sync_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
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
            "remote_comment_id",
            name="uq_publisher_raw_comments_platform_remote",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    platform_id: Mapped[str] = mapped_column(String, nullable=False)
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
    raw_payload_json: Mapped[str] = mapped_column(Text, default="{}")
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )
