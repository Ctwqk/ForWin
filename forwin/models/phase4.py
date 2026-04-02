from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class NPCIntentSnapshot(Base):
    __tablename__ = "npc_intent_snapshots"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    entity_id: Mapped[str] = mapped_column(
        String, ForeignKey("entities.id"), nullable=False
    )
    entity_name: Mapped[str] = mapped_column(String, nullable=False)
    intent_kind: Mapped[str] = mapped_column(String, default="pursue")
    objective: Mapped[str] = mapped_column(Text, default="")
    tactic: Mapped[str] = mapped_column(Text, default="")
    urgency: Mapped[int] = mapped_column(Integer, default=1)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class WorldSimulationTurn(Base):
    __tablename__ = "world_simulation_turns"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    pressure_level: Mapped[str] = mapped_column(String, default="steady")
    pressure_summary: Mapped[str] = mapped_column(Text, default="")
    notable_shifts_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
