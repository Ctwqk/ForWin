from __future__ import annotations

from collections import defaultdict
from typing import Sequence

from forwin.models import SignalWindowAggregate
from forwin.protocol import AudienceTrendView

_SCORE_LEVEL_WEIGHT = {
    "noise": 0.1,
    "candidate": 0.4,
    "watchlist": 0.7,
    "confirmed": 1.0,
}


def score_signal_aggregate_v1(aggregate: SignalWindowAggregate) -> float:
    """Compute the Phase C score_v1 on a normalized 0-1 scale."""
    total_comments = max(1, int(aggregate.total_comment_count or 0))
    reader_estimate = max(1, int(aggregate.reader_estimate or 0))
    prevalence = min(1.0, float(aggregate.hit_comment_count or 0) / float(total_comments))
    penetration = min(
        1.0,
        float(aggregate.unique_user_count or 0) / max(1.0, float(reader_estimate) / 50.0),
    )
    severity = min(1.0, max(0.0, float(aggregate.max_severity or 0) / 3.0))
    confidence = min(1.0, max(0.0, float(aggregate.avg_confidence or 0.0)))
    level_weight = _SCORE_LEVEL_WEIGHT.get(str(aggregate.signal_level or "noise"), 0.1)
    score = (
        0.32 * prevalence
        + 0.26 * penetration
        + 0.18 * severity
        + 0.14 * level_weight
        + 0.10 * confidence
    )
    return round(min(1.0, max(0.0, score)), 4)


def derive_audience_trends(
    aggregates: Sequence[SignalWindowAggregate],
    *,
    window_type: str = "long",
    limit: int = 6,
    min_delta: float = 0.05,
) -> list[AudienceTrendView]:
    """Build an explicit trend layer from aggregate snapshots."""
    filtered = [
        row
        for row in aggregates
        if (not window_type or row.window_type == window_type)
        and str(row.signal_level or "noise") != "noise"
    ]
    by_key: dict[str, list[SignalWindowAggregate]] = defaultdict(list)
    for row in filtered:
        by_key[str(row.signal_key or "")].append(row)

    trends: list[AudienceTrendView] = []
    for signal_key, rows in by_key.items():
        ordered = sorted(
            rows,
            key=lambda row: (
                int(row.window_chapter_end or 0),
                int(row.window_chapter_start or 0),
                int(row.unique_user_count or 0),
            ),
            reverse=True,
        )
        current = ordered[0]
        previous = ordered[1] if len(ordered) > 1 else None
        current_score = score_signal_aggregate_v1(current)
        previous_score = score_signal_aggregate_v1(previous) if previous is not None else 0.0
        delta = round(current_score - previous_score, 4)
        if delta >= min_delta:
            trend_type = "rising"
        elif delta <= -min_delta:
            trend_type = "falling"
        else:
            trend_type = "stable"
        trends.append(
            AudienceTrendView(
                signal_key=signal_key,
                signal_type=str(current.signal_type or ""),
                target_name=str(current.target_name or ""),
                window_type=str(current.window_type or window_type or "long"),
                current_level=str(current.signal_level or "noise"),
                previous_score=previous_score,
                current_score=current_score,
                delta=delta,
                scale_confidence=float(getattr(current, "scale_confidence", 0.0) or 0.0),
                estimation_method=str(getattr(current, "estimation_method", "") or ""),
                trend_type=trend_type,
            )
        )

    return sorted(
        trends,
        key=lambda item: (abs(item.delta), item.current_score, item.signal_key),
        reverse=True,
    )[: max(1, limit)]
