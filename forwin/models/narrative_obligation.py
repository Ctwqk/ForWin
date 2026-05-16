from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class NarrativeObligationRow(Base):
    __tablename__ = "narrative_obligations"
    __table_args__ = (
        Index("ix_narrative_obligations_project_status", "project_id", "status", "priority"),
        Index("ix_narrative_obligations_project_deadline", "project_id", "deadline_chapter"),
        Index("ix_narrative_obligations_origin_chapter", "project_id", "origin_chapter_number"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    origin_chapter_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    origin_draft_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    origin_review_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    origin_signal_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    origin_plan_snapshot_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    obligation_type: Mapped[str] = mapped_column(String, nullable=False, default="")
    priority: Mapped[str] = mapped_column(String, nullable=False, default="P1")
    status: Mapped[str] = mapped_column(String, nullable=False, default="proposed")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    deferral_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    hardness: Mapped[str] = mapped_column(String, nullable=False, default="soft_gap")
    subject_refs_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    evidence_refs_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    deadline_chapter: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deadline_policy: Mapped[str] = mapped_column(String, nullable=False, default="block_at_deadline")
    payoff_test: Mapped[str] = mapped_column(Text, nullable=False, default="")
    resolution_conditions_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    linked_plan_patch_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    linked_future_chapters_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    blocking_policy: Mapped[str] = mapped_column(String, nullable=False, default="block_at_deadline")
    created_by: Mapped[str] = mapped_column(String, nullable=False, default="system")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolution_chapter: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resolution_evidence_refs_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    waive_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class NarrativePlanPatchRow(Base):
    __tablename__ = "narrative_plan_patches"
    __table_args__ = (
        Index("ix_narrative_plan_patches_project_scope", "project_id", "target_scope"),
        Index("ix_narrative_plan_patches_project_applied", "project_id", "applied"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    patch_type: Mapped[str] = mapped_column(String, nullable=False, default="defer_acceptance")
    target_scope: Mapped[str] = mapped_column(String, nullable=False, default="chapter")
    target_plan_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    target_arc_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    target_band_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    affected_chapters_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    source_obligation_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    source_signal_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    old_plan_digest: Mapped[str] = mapped_column(String, nullable=False, default="")
    new_plan_digest: Mapped[str] = mapped_column(String, nullable=False, default="")
    old_contract_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    new_contract_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    diff_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    must_preserve_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    must_not_change_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    new_constraints_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    writer_context_injections_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    reviewer_context_injections_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    expected_resolution_tests_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    validation_status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    validation_errors_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class FuturePlanAuditRunRow(Base):
    __tablename__ = "future_plan_audit_runs"
    __table_args__ = (
        Index("ix_future_plan_audit_project_created", "project_id", "created_at"),
        Index("ix_future_plan_audit_project_chapter", "project_id", "current_chapter_number"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    current_chapter_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    trigger_stage: Mapped[str] = mapped_column(String, nullable=False, default="")
    inspected_chapters_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(String, nullable=False, default="pass")
    issues_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    applied_plan_patch_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    blocking_reasons_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
