from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class ArcWorldContractRow(Base):
    __tablename__ = "arc_world_contracts"
    __table_args__ = (
        Index("ix_arc_world_contracts_project_arc", "project_id", "arc_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    arc_id: Mapped[str] = mapped_column(String, default="")
    arc_number: Mapped[int] = mapped_column(Integer, default=0)
    contract_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


class BandWorldContractRow(Base):
    __tablename__ = "band_world_contracts"
    __table_args__ = (
        Index(
            "ix_band_world_contracts_project_arc_band",
            "project_id",
            "arc_id",
            "band_id",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    arc_id: Mapped[str] = mapped_column(String, default="")
    band_id: Mapped[str] = mapped_column(String, default="")
    chapter_start: Mapped[int] = mapped_column(Integer, default=0)
    chapter_end: Mapped[int] = mapped_column(Integer, default=0)
    contract_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


class ChapterWorldDeltaIntentRow(Base):
    __tablename__ = "chapter_world_delta_intents"
    __table_args__ = (
        Index(
            "ix_chapter_world_intents_project_chapter",
            "project_id",
            "chapter_number",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    chapter_plan_id: Mapped[str] = mapped_column(String, default="")
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    intent_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String, default="planned")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


__all__ = [
    "ArcWorldContractRow",
    "BandWorldContractRow",
    "ChapterWorldDeltaIntentRow",
]
