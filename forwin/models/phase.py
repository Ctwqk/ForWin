from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class ProjectStageAnalysis(Base):
    __tablename__ = "project_stage_analyses"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    stage_label: Mapped[str] = mapped_column(String, nullable=False)
    progress_ratio: Mapped[float] = mapped_column(Float, default=0.0)
    timeline_label: Mapped[str] = mapped_column(String, default="")
    timeline_ordinal: Mapped[int] = mapped_column(Integer, default=0)
    pacing_verdict: Mapped[str] = mapped_column(String, default="steady")
    pacing_summary: Mapped[str] = mapped_column(Text, default="")
    stale_threads_json: Mapped[str] = mapped_column(Text, default="[]")
    active_thread_count: Mapped[int] = mapped_column(Integer, default=0)
    unresolved_thread_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ProjectReplanEvent(Base):
    __tablename__ = "project_replan_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    trigger_chapter: Mapped[int] = mapped_column(Integer, nullable=False)
    risk_level: Mapped[str] = mapped_column(String, default="low")
    reason: Mapped[str] = mapped_column(Text, default="")
    focus_threads_json: Mapped[str] = mapped_column(Text, default="[]")
    strategy: Mapped[str] = mapped_column(String, default="rearc")
    status: Mapped[str] = mapped_column(String, default="applied")
    cooldown_until_chapter: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ArcEnvelope(Base):
    __tablename__ = "arc_envelopes"
    __table_args__ = (
        Index("ix_arc_envelopes_project_arc", "project_id", "arc_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    arc_id: Mapped[str] = mapped_column(
        String, ForeignKey("arc_plan_versions.id"), nullable=False
    )
    base_target_size: Mapped[int] = mapped_column(Integer, default=0)
    base_soft_min: Mapped[int] = mapped_column(Integer, default=0)
    base_soft_max: Mapped[int] = mapped_column(Integer, default=0)
    resolved_target_size: Mapped[int] = mapped_column(Integer, default=0)
    resolved_soft_min: Mapped[int] = mapped_column(Integer, default=0)
    resolved_soft_max: Mapped[int] = mapped_column(Integer, default=0)
    detailed_band_size: Mapped[int] = mapped_column(Integer, default=0)
    frozen_zone_size: Mapped[int] = mapped_column(Integer, default=0)
    current_projected_size: Mapped[int] = mapped_column(Integer, default=0)
    current_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source_policy_tier: Mapped[str] = mapped_column(String, default="short")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ArcStructureDraft(Base):
    __tablename__ = "arc_structure_drafts"
    __table_args__ = (
        Index("ix_arc_structure_drafts_project_arc", "project_id", "arc_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    arc_id: Mapped[str] = mapped_column(
        String, ForeignKey("arc_plan_versions.id"), nullable=False
    )
    phase_layout_json: Mapped[str] = mapped_column(Text, default="[]")
    key_beats_json: Mapped[str] = mapped_column(Text, default="[]")
    thread_priorities_json: Mapped[str] = mapped_column(Text, default="[]")
    hotspot_candidates_json: Mapped[str] = mapped_column(Text, default="[]")
    compression_candidates_json: Mapped[str] = mapped_column(Text, default="[]")
    reader_promise_json: Mapped[str] = mapped_column(Text, default="{}")
    arc_payoff_map_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ArcEnvelopeAnalysis(Base):
    __tablename__ = "arc_envelope_analyses"
    __table_args__ = (
        Index("ix_arc_envelope_analyses_project_arc", "project_id", "arc_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    arc_id: Mapped[str] = mapped_column(
        String, ForeignKey("arc_plan_versions.id"), nullable=False
    )
    based_on_band_id: Mapped[str] = mapped_column(String, default="")
    recommendation: Mapped[str] = mapped_column(String, default="keep")
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    expansion_signals_json: Mapped[str] = mapped_column(Text, default="[]")
    compression_signals_json: Mapped[str] = mapped_column(Text, default="[]")
    suggested_target: Mapped[int] = mapped_column(Integer, default=0)
    suggested_soft_min: Mapped[int] = mapped_column(Integer, default=0)
    suggested_soft_max: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ProvisionalPromotionRecord(Base):
    __tablename__ = "provisional_promotion_records"
    __table_args__ = (
        Index("ix_provisional_promotions_project_arc_band", "project_id", "arc_id", "band_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    arc_id: Mapped[str] = mapped_column(
        String, ForeignKey("arc_plan_versions.id"), nullable=False
    )
    band_id: Mapped[str] = mapped_column(String, default="")
    promoted_chapter_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    promotion_reason: Mapped[str] = mapped_column(Text, default="")
    based_on_analysis_id: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ProvisionalBandExecution(Base):
    __tablename__ = "provisional_band_executions"
    __table_args__ = (
        Index("ix_provisional_band_exec_project_arc_band", "project_id", "arc_id", "band_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    arc_id: Mapped[str] = mapped_column(
        String, ForeignKey("arc_plan_versions.id"), nullable=False
    )
    band_id: Mapped[str] = mapped_column(String, default="")
    chapter_numbers_json: Mapped[str] = mapped_column(Text, default="[]")
    artifact_path: Mapped[str] = mapped_column(Text, default="")
    aggregate_verdict: Mapped[str] = mapped_column(String, default="pass")
    preview_char_count: Mapped[int] = mapped_column(Integer, default=0)
    issue_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ProvisionalChapterLedger(Base):
    __tablename__ = "provisional_chapter_ledgers"
    __table_args__ = (
        Index(
            "ix_provisional_chapter_ledgers_project_arc_band_chapter",
            "project_id",
            "arc_id",
            "band_id",
            "chapter_number",
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
    chapter_number: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    verdict: Mapped[str] = mapped_column(String, default="pass")
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    artifact_meta_path: Mapped[str] = mapped_column(Text, default="")
    draft_blob_path: Mapped[str] = mapped_column(Text, default="")
    current_time_label: Mapped[str] = mapped_column(Text, default="")
    projected_time_label: Mapped[str] = mapped_column(Text, default="")
    state_changes_json: Mapped[str] = mapped_column(Text, default="[]")
    events_json: Mapped[str] = mapped_column(Text, default="[]")
    thread_beats_json: Mapped[str] = mapped_column(Text, default="[]")
    time_advance_json: Mapped[str] = mapped_column(Text, default="{}")
    issues_json: Mapped[str] = mapped_column(Text, default="[]")
    error_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class WorldProjectionDeltaRow(Base):
    __tablename__ = "world_projection_deltas"
    __table_args__ = (
        Index(
            "ix_world_projection_deltas_project_projection",
            "project_id",
            "projection_id",
            "projection_layer",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    projection_id: Mapped[str] = mapped_column(String, nullable=False)
    projection_layer: Mapped[str] = mapped_column(String, default="provisional_projection")
    delta_id: Mapped[str] = mapped_column(String, default="")
    chapter_number: Mapped[int] = mapped_column(Integer, default=0)
    world_delta_json: Mapped[str] = mapped_column(Text, default="{}")
    promoted_compile_run_id: Mapped[str] = mapped_column(String, default="")
    promotion_review_verdict_id: Mapped[str] = mapped_column(String, default="")
    promotion_reason: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


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
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    arc_id: Mapped[str] = mapped_column(String, default="")
    band_id: Mapped[str] = mapped_column(String, default="")
    chapter_number: Mapped[int] = mapped_column(Integer, default=0)
    template_id: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False, default="")
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
    repair_phase: Mapped[str] = mapped_column(String, default="review_repair")
    phase_attempt_no: Mapped[int] = mapped_column(Integer, default=0)
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
