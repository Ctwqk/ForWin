from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base, new_id


class PerformanceSpan(Base):
    __tablename__ = "performance_spans"
    __table_args__ = (
        Index("ix_performance_spans_project_created", "project_id", "created_at"),
        Index("ix_performance_spans_task_created", "task_id", "created_at"),
        Index("ix_performance_spans_operation", "operation_id", "created_at"),
        Index("ix_performance_spans_parent", "parent_span_id", "created_at"),
        Index("ix_performance_spans_name_duration", "span_name", "duration_ms"),
        Index("ix_performance_spans_chapter", "project_id", "chapter_number", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, default="")
    task_id: Mapped[str] = mapped_column(String, default="")
    operation_id: Mapped[str] = mapped_column(String, default="")
    trace_id: Mapped[str] = mapped_column(String, default="")
    span_id: Mapped[str] = mapped_column(String, default="")
    parent_span_id: Mapped[str] = mapped_column(String, default="")

    span_name: Mapped[str] = mapped_column(String, default="")
    span_kind: Mapped[str] = mapped_column(String, default="")
    component: Mapped[str] = mapped_column(String, default="")
    stage: Mapped[str] = mapped_column(String, default="")
    chapter_number: Mapped[int] = mapped_column(Integer, default=0)
    arc_id: Mapped[str] = mapped_column(String, default="")
    band_id: Mapped[str] = mapped_column(String, default="")

    status: Mapped[str] = mapped_column(String, default="ok")
    start_time_unix_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    self_duration_ms: Mapped[int] = mapped_column(Integer, default=0)

    tags_json: Mapped[str] = mapped_column(Text, default="{}")
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    error_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
