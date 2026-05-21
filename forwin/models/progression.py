from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class ProjectProgressionRule(Base):
    __tablename__ = "project_progression_rules"
    __table_args__ = (
        Index(
            "ix_project_progression_rules_project_range",
            "project_id",
            "chapter_start",
            "chapter_end",
        ),
        Index("ix_project_progression_rules_project_type", "project_id", "rule_type"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    rule_type: Mapped[str] = mapped_column(String, nullable=False)
    chapter_start: Mapped[int] = mapped_column(Integer, default=1)
    chapter_end: Mapped[int] = mapped_column(Integer, default=0)
    severity: Mapped[str] = mapped_column(String, default="warning")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
