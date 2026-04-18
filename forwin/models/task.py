from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func


from .base import Base


class GenerationTask(Base):
    __tablename__ = "generation_tasks"
    __table_args__ = (
        Index("ix_generation_tasks_status_updated", "status", "updated_at"),
        Index("ix_generation_tasks_project_updated", "project_id", "updated_at"),
        Index("ix_generation_tasks_deleted_updated", "deleted_at", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    task_kind: Mapped[str] = mapped_column(String, default="generation")
    status: Mapped[str] = mapped_column(String, default="starting")
    title: Mapped[str] = mapped_column(String, default="")
    subtitle: Mapped[str] = mapped_column(String, default="")
    project_id: Mapped[str] = mapped_column(String, default="")
    extension_client_id: Mapped[str] = mapped_column(String, default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    message: Mapped[str] = mapped_column(Text, default="")
    current_stage: Mapped[str] = mapped_column(String, default="queued")
    stage_history_json: Mapped[str] = mapped_column(Text, default="[]")
    requested_chapters: Mapped[int] = mapped_column(Integer, default=0)
    current_chapter: Mapped[int] = mapped_column(Integer, default=0)
    completed_chapters_json: Mapped[str] = mapped_column(Text, default="[]")
    failed_chapters_json: Mapped[str] = mapped_column(Text, default="[]")
    paused_chapters_json: Mapped[str] = mapped_column(Text, default="[]")
    frozen_artifacts_json: Mapped[str] = mapped_column(Text, default="[]")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )
