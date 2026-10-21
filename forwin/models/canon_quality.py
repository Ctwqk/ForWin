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
