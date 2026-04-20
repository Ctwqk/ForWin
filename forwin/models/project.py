from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String, nullable=False)
    premise: Mapped[str] = mapped_column(Text, nullable=False)
    genre: Mapped[str] = mapped_column(String, default="玄幻")
    setting_summary: Mapped[str] = mapped_column(Text, default="")
    target_total_chapters: Mapped[int] = mapped_column(Integer, default=3)
    creation_status: Mapped[str] = mapped_column(String, default="legacy")
    active_genesis_revision_id: Mapped[str] = mapped_column(String, default="")
    automation_json: Mapped[str] = mapped_column(Text, default="{}")
    governance_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


class ArcPlanVersion(Base):
    __tablename__ = "arc_plan_versions"
    __table_args__ = (
        Index("ix_arc_plan_versions_project_status", "project_id", "status"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    arc_number: Mapped[int] = mapped_column(Integer, default=1)
    chapter_start: Mapped[int] = mapped_column(Integer, default=1)
    chapter_end: Mapped[int] = mapped_column(Integer, default=0)
    arc_synopsis: Mapped[str] = mapped_column(Text, nullable=False)
    planned_target_size: Mapped[int] = mapped_column(Integer, default=0)
    planned_soft_min: Mapped[int] = mapped_column(Integer, default=0)
    planned_soft_max: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ChapterPlan(Base):
    __tablename__ = "chapter_plans"
    __table_args__ = (
        Index("ix_chapter_plans_project_chapter", "project_id", "chapter_number"),
        Index("ix_chapter_plans_project_status", "project_id", "status"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    arc_plan_id: Mapped[str] = mapped_column(
        String, ForeignKey("arc_plan_versions.id"), nullable=False
    )
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String, default="")
    one_line: Mapped[str] = mapped_column(Text, default="")
    goals_json: Mapped[str] = mapped_column(Text, default="[]")
    task_contract_json: Mapped[str] = mapped_column(Text, default="[]")
    experience_plan_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String, default="planned")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
