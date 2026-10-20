from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class StoryCalendarRow(Base):
    """One story calendar per book: the epoch label, the opening time, and units."""

    __tablename__ = "story_calendars"
    __table_args__ = (
        Index("ux_story_calendars_project", "project_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    epoch_label: Mapped[str] = mapped_column(Text, default="")
    opening_minute: Mapped[int] = mapped_column(BigInteger, default=0)
    units_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


class BookLexiconRow(Base):
    """One lexicon per book: the vocabulary the runtime may use for this book."""

    __tablename__ = "book_lexicons"
    __table_args__ = (
        Index("ux_book_lexicons_project", "project_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=False
    )
    lexicon_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )
