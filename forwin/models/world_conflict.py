from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class WorldPlanConflictRow(Base):
    __tablename__ = "world_plan_conflicts"
    __table_args__ = (
        Index("ix_world_plan_conflicts_project_status", "project_id", "status"),
        Index("ix_world_plan_conflicts_project_blocks", "project_id", "blocks_from_chapter"),
        Index("ix_world_plan_conflicts_project_origin", "project_id", "origin_key"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    origin_key: Mapped[str] = mapped_column(String, nullable=False, default="", server_default="")
    detected_after_chapter: Mapped[int] = mapped_column(Integer, nullable=False)
    story_time: Mapped[int] = mapped_column(BigInteger, nullable=False)
    involved_refs_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    violated: Mapped[str] = mapped_column(String, nullable=False)
    proposed_change_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    impact_level: Mapped[str] = mapped_column(String, nullable=False)
    options_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    required_decider: Mapped[str] = mapped_column(String, nullable=False)
    blocks_from_chapter: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="open")
    decision_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    result_refs_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    intent_ref: Mapped[str] = mapped_column(String, nullable=False, default="")
    actor_ref: Mapped[str] = mapped_column(String, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())
