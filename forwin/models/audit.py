from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class DecisionEvent(Base):
    __tablename__ = "decision_events"
    __table_args__ = (
        Index("ix_decision_events_project_created", "project_id", "created_at"),
        Index(
            "ix_decision_events_project_scope_created",
            "project_id",
            "scope",
            "created_at",
        ),
        Index(
            "ix_decision_events_project_band_created",
            "project_id",
            "band_id",
            "created_at",
        ),
        Index(
            "ix_decision_events_project_chapter_created",
            "project_id",
            "chapter_number",
            "created_at",
        ),
        Index("ix_decision_events_task_created", "task_id", "created_at"),
        Index("ix_decision_events_causal_root_created", "causal_root_id", "created_at"),
        Index(
            "ix_decision_events_related_object",
            "related_object_type",
            "related_object_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    task_id: Mapped[str] = mapped_column(String, default="")
    band_id: Mapped[str] = mapped_column(String, default="")
    chapter_number: Mapped[int] = mapped_column(Integer, default=0)
    scope: Mapped[str] = mapped_column(String, default="project")
    event_family: Mapped[str] = mapped_column(String, default="business_event")
    event_type: Mapped[str] = mapped_column(String, default="")
    actor_type: Mapped[str] = mapped_column(String, default="system")
    actor_id: Mapped[str] = mapped_column(String, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    related_object_type: Mapped[str] = mapped_column(String, default="")
    related_object_id: Mapped[str] = mapped_column(String, default="")
    parent_event_id: Mapped[str] = mapped_column(String, default="")
    causal_root_id: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


__all__ = ["DecisionEvent"]
