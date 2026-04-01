from __future__ import annotations

from typing import Optional

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, new_id


class StoryTimePoint(Base):
    __tablename__ = "story_time_points"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    label: Mapped[str] = mapped_column(String, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")


class ChapterTimeline(Base):
    __tablename__ = "chapter_timelines"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    start_time_id: Mapped[str] = mapped_column(
        String, ForeignKey("story_time_points.id"), nullable=False
    )
    end_time_id: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("story_time_points.id"), nullable=True
    )
    duration_description: Mapped[str] = mapped_column(String, default="")
