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


class CanonCommitRecord(Base):
    __tablename__ = "canon_commit_records"
    __table_args__ = (
        UniqueConstraint(
            "chapter_plan_id", "id", name="uq_canon_commit_chapter_identity"
        ),
        Index("ux_canon_commits_idempotency_key", "idempotency_key", unique=True),
        Index("ix_canon_commits_candidate", "candidate_id"),
        Index("ix_canon_commits_project_base_revision", "project_id", "base_book_revision"),
        Index(
            "ux_canon_commits_chapter_revision",
            "chapter_plan_id",
            "acceptance_revision",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    candidate_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("candidate_draft_records.id"),
        nullable=False,
    )
    project_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("projects.id"),
        nullable=False,
    )
    chapter_plan_id: Mapped[str] = mapped_column(
        String, ForeignKey("chapter_plans.id"), nullable=False
    )
    chapter_title: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    acceptance_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    base_book_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    production_mode: Mapped[str] = mapped_column(
        String, nullable=False, default="daily_serial", server_default="daily_serial"
    )
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_previous_accepted_chapter: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    expected_book_state_chapter: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    graph_delta_ids_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="[]",
    )
    world_snapshot_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    map_snapshot_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    status: Mapped[str] = mapped_column(String, nullable=False, default="committed")
    result_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        onupdate=func.now(),
    )


__all__ = ["CanonCommitRecord"]


class CanonPublicationProtection(Base):
    """Publication facts outlive upload jobs, attempts and platform bindings."""

    __tablename__ = "canon_publication_protections"
    __table_args__ = (
        Index("ux_canon_publication_job", "upload_job_id", unique=True),
        Index("ix_canon_publication_range", "project_id", "chapter_number", "state"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    chapter_plan_id: Mapped[str] = mapped_column(
        String, ForeignKey("chapter_plans.id"), nullable=False
    )
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    canon_commit_id: Mapped[str] = mapped_column(
        String, ForeignKey("canon_commit_records.id"), nullable=False
    )
    upload_job_id: Mapped[str] = mapped_column(String, nullable=False)
    platform_id: Mapped[str] = mapped_column(String, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False, default="reserved")
    remote_book_id: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    remote_chapter_id: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    evidence_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


class CanonRevisionValidationRecord(Base):
    """Immutable full-suffix evidence; only Canon admission records its consumption."""

    __tablename__ = "canon_revision_validations"
    __table_args__ = (Index("ix_canon_revision_candidate", "project_id", "candidate_id", "created_at"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    candidate_id: Mapped[str] = mapped_column(String, ForeignKey("candidate_draft_records.id"), nullable=False)
    base_book_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)
    accepted_book_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
