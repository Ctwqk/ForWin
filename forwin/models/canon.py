from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class CanonCommitRecord(Base):
    __tablename__ = "canon_commit_records"
    __table_args__ = (
        Index("ux_canon_commits_idempotency_key", "idempotency_key", unique=True),
        Index("ux_canon_commits_candidate", "candidate_id", unique=True),
        Index(
            "ux_canon_commits_project_chapter",
            "project_id",
            "chapter_number",
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
