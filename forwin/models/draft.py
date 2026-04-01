from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class ChapterDraft(Base):
    __tablename__ = "chapter_drafts"

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
