from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class ArcBeatSheetRow(Base):
    __tablename__ = "arc_beat_sheets"
    __table_args__ = (
        UniqueConstraint("arc_plan_id", "version", name="uq_arc_beat_sheets_arc_version"),
        Index("ix_arc_beat_sheets_project_status", "project_id", "status"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    arc_plan_id: Mapped[str] = mapped_column(String, ForeignKey("arc_plan_versions.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    source: Mapped[str] = mapped_column(String, nullable=False, default="llm")
    protagonist_ref: Mapped[str] = mapped_column(String, nullable=False)
    phases_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    milestones_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    protections_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    reveal_targets_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    exit_state_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    band_breakdown_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    reader_promise_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    arc_payoff_map_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    setting_brief: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    revision_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class BandPlanRow(Base):
    __tablename__ = "band_plans"
    __table_args__ = (
        UniqueConstraint("arc_plan_id", "band_index", "version", name="uq_band_plans_arc_band_version"),
        Index("ix_band_plans_project_arc_band_status", "project_id", "arc_plan_id", "band_index", "status"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    arc_plan_id: Mapped[str] = mapped_column(String, ForeignKey("arc_plan_versions.id"), nullable=False)
    beat_sheet_id: Mapped[str] = mapped_column(String, ForeignKey("arc_beat_sheets.id"), nullable=False)
    band_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chapter_start: Mapped[int] = mapped_column(Integer, nullable=False)
    chapter_end: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    replan_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    plan_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
