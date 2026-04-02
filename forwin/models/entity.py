from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class Entity(Base):
    __tablename__ = "entities"
    __table_args__ = (
        Index("ix_entities_project_active", "project_id", "is_active"),
        Index("ix_entities_project_name", "project_id", "name"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    aliases_json: Mapped[str] = mapped_column(Text, default="[]")
    description: Mapped[str] = mapped_column(Text, default="")
    importance: Mapped[int] = mapped_column(Integer, default=5)
    created_at_chapter: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class EntityAlias(Base):
    __tablename__ = "entity_aliases"
    __table_args__ = (
        UniqueConstraint("project_id", "alias", name="uq_entity_alias_project_alias"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    entity_id: Mapped[str] = mapped_column(
        String, ForeignKey("entities.id"), nullable=False
    )
    project_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    alias: Mapped[str] = mapped_column(String, nullable=False, index=True)


class EntityState(Base):
    __tablename__ = "entity_states"
    __table_args__ = (
        Index("ix_entity_states_entity_chapter", "entity_id", "as_of_chapter"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    entity_id: Mapped[str] = mapped_column(
        String, ForeignKey("entities.id"), nullable=False
    )
    as_of_chapter: Mapped[int] = mapped_column(Integer, nullable=False)
    state_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class RelationEdge(Base):
    __tablename__ = "relation_edges"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    source_entity_id: Mapped[str] = mapped_column(
        String, ForeignKey("entities.id"), nullable=False
    )
    target_entity_id: Mapped[str] = mapped_column(
        String, ForeignKey("entities.id"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    established_at_chapter: Mapped[int] = mapped_column(Integer, default=0)
    ended_at_chapter: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
