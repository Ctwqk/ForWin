from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class SubWorld(Base):
    __tablename__ = "sub_worlds"
    __table_args__ = (
        Index("ix_sub_worlds_project_status", "project_id", "status"),
        Index("ix_sub_worlds_project_scope", "project_id", "scope"),
        Index("ix_sub_worlds_project_origin_arc", "project_id", "origin_arc_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    origin_arc_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    parent_subworld_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, default="")
    scope: Mapped[str] = mapped_column(String, default="arc_local")
    status: Mapped[str] = mapped_column(String, default="active")
    introduced_at_chapter: Mapped[int] = mapped_column(Integer, default=0)
    retired_at_chapter: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
    )


class SubWorldRosterItem(Base):
    __tablename__ = "sub_world_roster_items"
    __table_args__ = (
        Index("ix_sub_world_roster_project_subworld", "project_id", "subworld_id"),
        Index("ix_sub_world_roster_project_status", "project_id", "status"),
        Index("ix_sub_world_roster_project_display", "project_id", "display_name"),
        Index("ix_sub_world_roster_project_slot", "project_id", "slot_key"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    subworld_id: Mapped[str] = mapped_column(
        String, ForeignKey("sub_worlds.id"), nullable=False
    )
    entity_id: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("entities.id"), nullable=True
    )
    entity_kind: Mapped[str] = mapped_column(String, default="character")
    display_name: Mapped[str] = mapped_column(String, default="")
    slot_key: Mapped[str] = mapped_column(String, default="")
    role_hint: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    is_core: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String, default="planned_slot")
    activation_chapter: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
    )
