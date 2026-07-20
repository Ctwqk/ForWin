from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class PostCanonMaintenanceRun(Base):
    __tablename__ = "post_canon_maintenance_runs"
    __table_args__ = (
        UniqueConstraint(
            "canon_commit_id",
            "step_name",
            name="uq_post_canon_maintenance_canon_step",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_post_canon_maintenance_idempotency_key",
        ),
        Index(
            "ix_post_canon_maintenance_project_chapter_status",
            "project_id",
            "chapter_number",
            "status",
        ),
        Index(
            "ix_post_canon_maintenance_status_available",
            "status",
            "available_at",
            "created_at",
        ),
        Index(
            "ix_post_canon_maintenance_status_lease_expires",
            "status",
            "lease_expires_at",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    canon_commit_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("canon_commit_records.id"),
        nullable=False,
    )
    project_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("projects.id"),
        nullable=False,
    )
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("candidate_draft_records.id"),
        nullable=False,
    )
    step_name: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="pending",
        server_default="pending",
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    available_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    result_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="{}",
        server_default="{}",
    )
    last_error: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
        server_default="",
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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


__all__ = ["PostCanonMaintenanceRun"]
