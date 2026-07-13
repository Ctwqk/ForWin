from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from forwin.models.observability import PerformanceSpan


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    performance_span_days: int = 30

    @classmethod
    def from_config(cls, config) -> "RetentionPolicy":  # noqa: ANN001
        return cls(
            performance_span_days=max(0, int(getattr(config, "performance_span_retention_days", 30) or 0)),
        )


@dataclass(frozen=True, slots=True)
class RetentionCleanupResult:
    performance_spans_deleted: int = 0


def run_retention_cleanup(
    session: Session,
    policy: RetentionPolicy,
    *,
    now: datetime | None = None,
) -> RetentionCleanupResult:
    current_time = now or datetime.now(UTC).replace(tzinfo=None)
    performance_spans_deleted = _delete_older_than(
        session,
        PerformanceSpan,
        current_time=current_time,
        retention_days=policy.performance_span_days,
    )
    return RetentionCleanupResult(
        performance_spans_deleted=performance_spans_deleted,
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
