from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class ScenarioRehearsalRunRow(Base):
    __tablename__ = "scenario_rehearsal_runs"
    __table_args__ = (
        Index(
            "ix_scenario_rehearsal_project_arc_band",
            "project_id",
            "arc_id",
            "band_id",
        ),
        Index("ix_scenario_rehearsal_project_created", "project_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    arc_id: Mapped[str] = mapped_column(String, default="")
    band_id: Mapped[str] = mapped_column(String, default="")
    rehearsal_scope: Mapped[str] = mapped_column(String, default="band")
    chapter_numbers_json: Mapped[str] = mapped_column(Text, default="[]")
    trigger_reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    recommendation: Mapped[str] = mapped_column(String, default="pass")
    risk_count: Mapped[int] = mapped_column(Integer, default=0)
    blocker_count: Mapped[int] = mapped_column(Integer, default=0)
    required_patch_count: Mapped[int] = mapped_column(Integer, default=0)
    report_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ScenarioPlanPatchRow(Base):
    __tablename__ = "scenario_plan_patches"
    __table_args__ = (
        Index("ix_scenario_plan_patches_project_run", "project_id", "run_id"),
        Index("ix_scenario_plan_patches_project_status", "project_id", "status"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(
        String, ForeignKey("scenario_rehearsal_runs.id"), default=""
    )
    arc_id: Mapped[str] = mapped_column(String, default="")
    band_id: Mapped[str] = mapped_column(String, default="")
    patch_type: Mapped[str] = mapped_column(String, default="")
    target: Mapped[str] = mapped_column(Text, default="")
    message: Mapped[str] = mapped_column(Text, default="")
    evidence_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    patch_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String, default="proposed")
    approval_reason: Mapped[str] = mapped_column(Text, default="")
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


__all__ = ["ScenarioPlanPatchRow", "ScenarioRehearsalRunRow"]
