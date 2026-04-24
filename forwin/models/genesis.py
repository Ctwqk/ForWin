from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class BookGenesisRevision(Base):
    __tablename__ = "book_genesis_revisions"
    __table_args__ = (
        Index("ix_book_genesis_revisions_project_created", "project_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, default=1)
    based_on_revision_id: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="draft")
    pack_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


class PromptTrace(Base):
    __tablename__ = "prompt_traces"
    __table_args__ = (
        Index("ix_prompt_traces_project_stage_created", "project_id", "stage_key", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    genesis_revision_id: Mapped[str] = mapped_column(String, default="")
    decision_event_id: Mapped[str] = mapped_column(String, default="")
    parent_trace_id: Mapped[str] = mapped_column(String, default="")
    trace_scope: Mapped[str] = mapped_column(String, default="genesis")
    stage_key: Mapped[str] = mapped_column(String, default="")
    template_id: Mapped[str] = mapped_column(String, default="")
    template_version: Mapped[str] = mapped_column(String, default="v1")
    effective_system_prompt: Mapped[str] = mapped_column(Text, default="")
    prompt_layers_json: Mapped[str] = mapped_column(Text, default="[]")
    input_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    model_profile_json: Mapped[str] = mapped_column(Text, default="{}")
    attempts_json: Mapped[str] = mapped_column(Text, default="[]")
    output_summary_json: Mapped[str] = mapped_column(Text, default="{}")
    backend: Mapped[str] = mapped_column(String, default="")
    codex_job_id: Mapped[str] = mapped_column(String, default="")
    permission_profile: Mapped[str] = mapped_column(String, default="")
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
