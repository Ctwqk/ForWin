from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base


class SerialCapacityConfigRevision(Base):
    """Append-only observations of the authoritative Project capacity settings."""

    __tablename__ = "serial_capacity_config_revisions"
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    total_chapters: Mapped[int] = mapped_column(Integer, nullable=False)
    primary_platform: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ChapterCapacityReservation(Base):
    __tablename__ = "chapter_capacity_reservations"
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    chapter_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    chapter_plan_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("chapter_plans.id", ondelete="CASCADE"), nullable=True
    )
    task_id: Mapped[str] = mapped_column(
        String, ForeignKey("generation_tasks.id", ondelete="CASCADE"), nullable=False
    )
    lease_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
