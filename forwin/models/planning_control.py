from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class BandCheckpoint(Base):
    __tablename__ = "band_checkpoints"
    __table_args__ = (
        Index(
            "ix_band_checkpoints_project_band_created",
            "project_id",
            "band_id",
            "created_at",
        ),
        Index(
            "ix_band_checkpoints_project_status_created",
            "project_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    arc_id: Mapped[str] = mapped_column(
        String, ForeignKey("arc_plan_versions.id"), nullable=False
    )
    band_id: Mapped[str] = mapped_column(String, default="")
    chapter_start: Mapped[int] = mapped_column(Integer, default=0)
    chapter_end: Mapped[int] = mapped_column(Integer, default=0)
    trigger_source: Mapped[str] = mapped_column(String, default="auto_band_end")
    boundary_kind: Mapped[str] = mapped_column(String, default="band_end")
    boundary_chapter: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String, default="pending")
    summary: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    issues_json: Mapped[str] = mapped_column(Text, default="[]")
    related_task_id: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class NarrativeConstraint(Base):
    __tablename__ = "narrative_constraints"
    __table_args__ = (
        Index("ix_narrative_constraints_project_status", "project_id", "status"),
        Index("ix_narrative_constraints_project_band", "project_id", "band_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    arc_id: Mapped[str] = mapped_column(String, default="")
    band_id: Mapped[str] = mapped_column(String, default="")
    constraint_type: Mapped[str] = mapped_column(
        String, default="character_availability"
    )
    level: Mapped[str] = mapped_column(String, default="hard")
    subject_name: Mapped[str] = mapped_column(String, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    effective_from_chapter: Mapped[int] = mapped_column(Integer, default=1)
    protect_until_chapter: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


__all__ = ["BandCheckpoint", "NarrativeConstraint"]
