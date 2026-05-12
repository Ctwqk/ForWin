from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from forwin.models.draft import CandidateDraftRecord
from forwin.models.genesis import PromptTrace
from forwin.models.observability import PerformanceSpan


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    performance_span_days: int = 30
    prompt_trace_days: int = 30
    candidate_drafts_keep_per_chapter: int = 5

    @classmethod
    def from_config(cls, config) -> "RetentionPolicy":  # noqa: ANN001
        return cls(
            performance_span_days=max(0, int(getattr(config, "performance_span_retention_days", 30) or 0)),
            prompt_trace_days=max(0, int(getattr(config, "prompt_trace_retention_days", 30) or 0)),
            candidate_drafts_keep_per_chapter=max(
                0,
                int(getattr(config, "candidate_draft_keep_per_chapter", 5) or 0),
            ),
        )


@dataclass(frozen=True, slots=True)
class RetentionCleanupResult:
    performance_spans_deleted: int = 0
    prompt_traces_deleted: int = 0
    candidate_drafts_deleted: int = 0


def run_retention_cleanup(
    session: Session,
    policy: RetentionPolicy,
    *,
    now: datetime | None = None,
) -> RetentionCleanupResult:
    current_time = now or datetime.utcnow()
    performance_spans_deleted = _delete_older_than(
        session,
        PerformanceSpan,
        current_time=current_time,
        retention_days=policy.performance_span_days,
    )
    prompt_traces_deleted = _delete_older_than(
        session,
        PromptTrace,
        current_time=current_time,
        retention_days=policy.prompt_trace_days,
    )
    candidate_drafts_deleted = _delete_stale_candidate_drafts(
        session,
        keep_per_chapter=policy.candidate_drafts_keep_per_chapter,
    )
    return RetentionCleanupResult(
        performance_spans_deleted=performance_spans_deleted,
        prompt_traces_deleted=prompt_traces_deleted,
        candidate_drafts_deleted=candidate_drafts_deleted,
    )


def _delete_older_than(
    session: Session,
    model,
    *,
    current_time: datetime,
    retention_days: int,
) -> int:
    if retention_days <= 0:
        return 0
    cutoff = current_time - timedelta(days=retention_days)
    result = session.execute(delete(model).where(model.created_at < cutoff))
    return int(result.rowcount or 0)


def _delete_stale_candidate_drafts(session: Session, *, keep_per_chapter: int) -> int:
    if keep_per_chapter <= 0:
        return 0
    ranked = (
        select(
            CandidateDraftRecord.id.label("row_id"),
            func.row_number()
            .over(
                partition_by=(CandidateDraftRecord.project_id, CandidateDraftRecord.chapter_number),
                order_by=(
                    CandidateDraftRecord.updated_at.desc(),
                    CandidateDraftRecord.created_at.desc(),
                    CandidateDraftRecord.id.desc(),
                ),
            )
            .label("draft_rank"),
        )
        .subquery()
    )
    stale_ids = select(ranked.c.row_id).where(ranked.c.draft_rank > keep_per_chapter)
    result = session.execute(delete(CandidateDraftRecord).where(CandidateDraftRecord.id.in_(stale_ids)))
    return int(result.rowcount or 0)
