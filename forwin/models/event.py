from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class CanonEvent(Base):
    __tablename__ = "canon_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    significance: Mapped[str] = mapped_column(String, default="minor")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class EventEntityLink(Base):
    __tablename__ = "event_entity_links"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    event_id: Mapped[str] = mapped_column(
        String, ForeignKey("canon_events.id"), nullable=False
    )
    entity_id: Mapped[str] = mapped_column(
        String, ForeignKey("entities.id"), nullable=False
    )
    role: Mapped[str] = mapped_column(String, nullable=False)
