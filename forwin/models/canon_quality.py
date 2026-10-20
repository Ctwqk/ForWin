from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class QualityAnalysisRunRow(Base):
    __tablename__ = "quality_analysis_runs"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "chapter_number",
            "content_hash",
            "plan_fingerprint",
            "analysis_mode",
            "analyzer_fingerprint",
            name="uq_quality_analysis_run_cache_key",
        ),
        Index(
            "ix_quality_analysis_runs_project_chapter",
            "project_id",
            "chapter_number",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    plan_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    analysis_mode: Mapped[str] = mapped_column(String, nullable=False)
    analyzer_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class CanonQualitySignalRow(Base):
    __tablename__ = "canon_quality_signals"
    __table_args__ = (
        Index("ix_canon_quality_signals_project_status", "project_id", "status", "created_at"),
        Index("ix_canon_quality_signals_project_chapter", "project_id", "chapter_number"),
        Index("ix_canon_quality_signals_project_type", "project_id", "signal_type"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    chapter_number: Mapped[int] = mapped_column(Integer, default=0)
    draft_id: Mapped[str] = mapped_column(String, default="")
    signal_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    signal_type: Mapped[str] = mapped_column(String, nullable=False, default="")
    severity: Mapped[str] = mapped_column(String, nullable=False, default="warning")
    target_scope: Mapped[str] = mapped_column(String, nullable=False, default="chapter")
    subject_key: Mapped[str] = mapped_column(String, nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence_refs_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    span_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    span_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String, nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CharacterStateTransitionRow(Base):
    __tablename__ = "character_state_transitions"
    __table_args__ = (
        Index("ix_character_state_transitions_project_character", "project_id", "character_name", "chapter_number"),
        Index("ix_character_state_transitions_project_terminal", "project_id", "terminality", "chapter_number"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    character_id: Mapped[str] = mapped_column(String, default="")
    character_name: Mapped[str] = mapped_column(String, default="")
    chapter_number: Mapped[int] = mapped_column(Integer, default=0)
    transition_type: Mapped[str] = mapped_column(String, default="")
    from_state: Mapped[str] = mapped_column(String, default="")
    to_state: Mapped[str] = mapped_column(String, default="")
    terminality: Mapped[str] = mapped_column(String, default="none")
    can_participate: Mapped[str] = mapped_column(String, default="true")
    requires_bridge_from_transition_id: Mapped[str] = mapped_column(String, default="")
    bridge_event_id: Mapped[str] = mapped_column(String, default="")
    evidence_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class CountdownLedgerRow(Base):
    __tablename__ = "countdown_ledgers"
    __table_args__ = (
        Index("ix_countdown_ledgers_project_key", "project_id", "countdown_key", "chapter_number"),
        Index("ix_countdown_ledgers_project_status", "project_id", "status", "chapter_number"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    countdown_key: Mapped[str] = mapped_column(String, default="main")
    label: Mapped[str] = mapped_column(String, default="main")
    chapter_number: Mapped[int] = mapped_column(Integer, default=0)
    normalized_remaining_minutes: Mapped[int] = mapped_column(Integer, default=0)
    raw_mention: Mapped[str] = mapped_column(String, default="")
    is_reset_event: Mapped[str] = mapped_column(String, default="false")
    is_branch_clock: Mapped[str] = mapped_column(String, default="false")
    is_resolution_event: Mapped[str] = mapped_column(String, default="false")
    previous_remaining_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, default="consistent")
    evidence_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ChapterBodyMetricRow(Base):
    __tablename__ = "chapter_body_metrics"
    __table_args__ = (
        Index("ix_chapter_body_metrics_project_chapter", "project_id", "chapter_number"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    chapter_number: Mapped[int] = mapped_column(Integer, default=0)
    draft_id: Mapped[str] = mapped_column(String, default="")
    paragraph_hashes_json: Mapped[str] = mapped_column(Text, default="[]")
    dialogue_fingerprints_json: Mapped[str] = mapped_column(Text, default="[]")
    scene_fingerprints_json: Mapped[str] = mapped_column(Text, default="[]")
    duplicate_spans_json: Mapped[str] = mapped_column(Text, default="[]")
    style_motifs_json: Mapped[str] = mapped_column(Text, default="[]")
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class CanonAdmissionRunRow(Base):
    __tablename__ = "canon_admission_runs"
    __table_args__ = (
        Index("ix_canon_admission_runs_project_chapter", "project_id", "chapter_number", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    chapter_number: Mapped[int] = mapped_column(Integer, default=0)
    draft_id: Mapped[str] = mapped_column(String, default="")
    review_id: Mapped[str] = mapped_column(String, default="")
    commit_allowed: Mapped[str] = mapped_column(String, default="false")
    verdict: Mapped[str] = mapped_column(String, default="warn")
    admission_mode: Mapped[str] = mapped_column(String, default="clean")
    obligation_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    required_plan_patch_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    blocking_reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    expired_obligation_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    over_budget: Mapped[str] = mapped_column(String, default="false")
    blocking_issue_count: Mapped[int] = mapped_column(Integer, default=0)
    warning_issue_count: Mapped[int] = mapped_column(Integer, default=0)
    gate_summary: Mapped[str] = mapped_column(Text, default="")
    signals_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
