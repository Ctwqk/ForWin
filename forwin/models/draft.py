from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class ChapterDraft(Base):
    __tablename__ = "chapter_drafts"
    __table_args__ = (
        Index("ix_chapter_drafts_plan_version", "chapter_plan_id", "version"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    chapter_plan_id: Mapped[str] = mapped_column(
        String, ForeignKey("chapter_plans.id"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    llm_model: Mapped[str] = mapped_column(String, default="")
    llm_raw_response: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ChapterReview(Base):
    __tablename__ = "chapter_reviews"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    draft_id: Mapped[str] = mapped_column(
        String, ForeignKey("chapter_drafts.id"), nullable=False
    )
    verdict: Mapped[str] = mapped_column(String, nullable=False)
    issues_json: Mapped[str] = mapped_column(Text, default="[]")
    review_meta_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class CandidateDraftRecord(Base):
    __tablename__ = "candidate_draft_records"
    __table_args__ = (
        Index("ix_candidate_drafts_project_chapter", "project_id", "chapter_number"),
        Index("ix_candidate_drafts_plan_created", "chapter_plan_id", "created_at"),
        Index("ix_candidate_drafts_draft", "candidate_draft_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    chapter_plan_id: Mapped[str] = mapped_column(
        String, ForeignKey("chapter_plans.id"), nullable=False
    )
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_draft_id: Mapped[str] = mapped_column(
        String, ForeignKey("chapter_drafts.id"), nullable=False
    )
    review_id: Mapped[str] = mapped_column(String, ForeignKey("chapter_reviews.id"), default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, default="drafted")
    canon_status: Mapped[str] = mapped_column(String, default="candidate")
    scene_outputs_json: Mapped[str] = mapped_column(Text, default="[]")
    state_change_candidates_json: Mapped[str] = mapped_column(Text, default="[]")
    event_candidates_json: Mapped[str] = mapped_column(Text, default="[]")
    thread_beat_candidates_json: Mapped[str] = mapped_column(Text, default="[]")
    repair_attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    canon_artifact_path: Mapped[str] = mapped_column(Text, default="")
    failure_reason: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )
