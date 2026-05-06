from __future__ import annotations

from collections import defaultdict
from typing import Callable, Iterable

from forwin.api_schemas import PerformanceBreakdownItem, PerformanceSpanInfo


def percentile(values: list[int], ratio: float) -> int:
    if not values:
        return 0
    ordered = sorted(max(0, int(value or 0)) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    index = int(round((len(ordered) - 1) * ratio))
    return ordered[max(0, min(len(ordered) - 1, index))]


class PerformanceAnalyzer:
    def breakdown(
        self,
        spans: Iterable[PerformanceSpanInfo],
        *,
        key_fn: Callable[[PerformanceSpanInfo], str],
    ) -> list[PerformanceBreakdownItem]:
        grouped: dict[str, list[PerformanceSpanInfo]] = defaultdict(list)
        for span in spans:
            key = str(key_fn(span) or "").strip() or "unknown"
            grouped[key].append(span)
        result: list[PerformanceBreakdownItem] = []
        for key, items in grouped.items():
            durations = [int(item.duration_ms or 0) for item in items]
            total = sum(durations)
            error_count = sum(1 for item in items if item.status == "failed")
            result.append(
                PerformanceBreakdownItem(
                    key=key,
                    count=len(items),
                    total_duration_ms=total,
                    avg_duration_ms=(float(total) / len(items)) if items else 0.0,
                    p50_ms=percentile(durations, 0.50),
                    p95_ms=percentile(durations, 0.95),
                    p99_ms=percentile(durations, 0.99),
                    max_ms=max(durations) if durations else 0,
                    error_count=error_count,
                    error_rate=(float(error_count) / len(items)) if items else 0.0,
                )
            )
        return sorted(result, key=lambda item: (-item.total_duration_ms, item.key))


class CriticalPathAnalyzer:
    def critical_path(self, spans: list[PerformanceSpanInfo]) -> list[PerformanceSpanInfo]:
        if not spans:
            return []
        by_parent: dict[str, list[PerformanceSpanInfo]] = defaultdict(list)
        by_id: dict[str, PerformanceSpanInfo] = {}
        for span in spans:
            by_id[span.span_id] = span
            by_parent[span.parent_span_id].append(span)
        roots = by_parent.get("", [])
        if not roots:
            roots = [max(spans, key=lambda item: item.duration_ms)]
        current = max(roots, key=lambda item: item.duration_ms)
        path = [current]
        while by_parent.get(current.span_id):
            current = max(by_parent[current.span_id], key=lambda item: item.duration_ms)
            path.append(current)
        return path
