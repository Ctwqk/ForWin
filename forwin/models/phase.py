from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class BandExperiencePlan(Base):
    __tablename__ = "band_experience_plans"
    __table_args__ = (
        Index("ix_band_experience_plans_project_arc_band", "project_id", "arc_id", "band_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    arc_id: Mapped[str] = mapped_column(
        String, ForeignKey("arc_plan_versions.id"), nullable=False
    )
    band_id: Mapped[str] = mapped_column(String, nullable=False)
    chapter_start: Mapped[int] = mapped_column(Integer, default=1)
    chapter_end: Mapped[int] = mapped_column(Integer, default=1)
    stall_guard_max_gap: Mapped[int] = mapped_column(Integer, default=0)
    task_contract_json: Mapped[str] = mapped_column(Text, default="[]")
    schedule_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class TropeUsageRecord(Base):
    __tablename__ = "trope_usage_records"
    __table_args__ = (
        Index("ix_trope_usage_project_band", "project_id", "band_id", "created_at"),
        Index(
            "ix_trope_usage_project_stage_created",
            "project_id",
            "usage_stage",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    arc_id: Mapped[str] = mapped_column(String, default="")
    band_id: Mapped[str] = mapped_column(String, default="")
    chapter_number: Mapped[int] = mapped_column(Integer, default=0)
    template_id: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False, default="")
    usage_stage: Mapped[str] = mapped_column(
        String,
        default="accepted",
        server_default="accepted",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ChapterRewriteAttempt(Base):
    __tablename__ = "chapter_rewrite_attempts"
    __table_args__ = (
        Index(
            "ix_chapter_rewrite_attempts_project_chapter_attempt",
            "project_id",
            "chapter_number",
            "attempt_no",
        ),
        Index(
            "ix_chapter_rewrite_attempts_project_chapter_phase",
            "project_id",
            "chapter_number",
            "repair_phase",
            "phase_attempt_no",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    repair_phase: Mapped[str] = mapped_column(
        String,
        default="review_repair",
        server_default="review_repair",
    )
    phase_attempt_no: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    trigger_review_id: Mapped[str] = mapped_column(
        String, ForeignKey("chapter_reviews.id"), nullable=False
    )
    repair_scope: Mapped[str] = mapped_column(String, default="")
    design_patch_json: Mapped[str] = mapped_column(Text, default="{}")
    source_draft_id: Mapped[str] = mapped_column(
        String, ForeignKey("chapter_drafts.id"), nullable=False
    )
    result_draft_id: Mapped[str] = mapped_column(
        String, ForeignKey("chapter_drafts.id"), nullable=False
    )
    result_verdict: Mapped[str] = mapped_column(String, default="")
    result_review_id: Mapped[str] = mapped_column(String, default="")
    failure_reason: Mapped[str] = mapped_column(Text, default="")
    verification_json: Mapped[str] = mapped_column(Text, default="{}")
    source_chapter_plan_json: Mapped[str] = mapped_column(Text, default="{}")
    result_chapter_plan_json: Mapped[str] = mapped_column(Text, default="{}")
    source_band_plan_json: Mapped[str] = mapped_column(Text, default="{}")
    result_band_plan_json: Mapped[str] = mapped_column(Text, default="{}")
    forced_accept_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
