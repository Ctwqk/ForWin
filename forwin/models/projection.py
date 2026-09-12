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


class ProjectionCheckpoint(Base):
    __tablename__ = "projection_checkpoints"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "projection_kind",
            name="uq_projection_checkpoints_project_kind",
        ),
        Index(
            "ix_projection_checkpoints_project_status",
            "project_id",
            "status",
            "updated_at",
        ),
        Index(
            "ix_projection_checkpoints_status_updated",
            "status",
            "updated_at",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("projects.id"),
        nullable=False,
    )
    projection_kind: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="never",
        server_default="never",
    )
    target_canon_commit_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("canon_commit_records.id"),
        nullable=True,
    )
    target_chapter_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    projected_canon_commit_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("canon_commit_records.id"),
        nullable=True,
    )
    projected_chapter_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    # NULL denotes a migrated, unknown baseline; the first run rebuilds explicitly.
    target_book_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    projected_book_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)

    last_event_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
        server_default="",
    )
    source_digest: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
        server_default="",
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


__all__ = ["ProjectionCheckpoint"]
