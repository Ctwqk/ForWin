from __future__ import annotations

from typing import Optional

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, new_id


class PlotThread(Base):
    __tablename__ = "plot_threads"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="active")
    priority: Mapped[int] = mapped_column(Integer, default=2)
    opened_at_chapter: Mapped[int] = mapped_column(Integer, default=0)
    closed_at_chapter: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class PlotThreadBeat(Base):
    __tablename__ = "plot_thread_beats"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    thread_id: Mapped[str] = mapped_column(
        String, ForeignKey("plot_threads.id"), nullable=False
    )
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    beat_type: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
