from __future__ import annotations

from forwin.api_schemas import PerformanceBreakdownItem, PerformanceReportResponse


def build_performance_recommendations(report: PerformanceReportResponse) -> list[str]:
    recommendations: list[str] = []
    total = max(1, int(report.total_duration_ms or 0))
    slowest = report.top_slow_spans[0] if report.top_slow_spans else None
    if slowest is not None and slowest.duration_ms / total >= 0.35:
        recommendations.append(
            f"{slowest.span_name} 占 task 总耗时 {int(slowest.duration_ms / total * 100)}%，优先检查该阶段。"
        )
    llm_hotspot = _first_high_share(report.llm_breakdown, total)
    if llm_hotspot is not None:
        recommendations.append(
            f"{llm_hotspot.key} LLM 耗时占比较高，优先检查模型路由、retry/fallback 和 prompt 稳定性。"
        )
    db_hotspot = _first_high_share(report.db_breakdown, total)
    if db_hotspot is not None:
        recommendations.append(
            f"{db_hotspot.key} DB 耗时占比较高，优先检查 query、flush 和 commit 批量化。"
        )
    return recommendations


def _first_high_share(items: list[PerformanceBreakdownItem], total: int) -> PerformanceBreakdownItem | None:
    for item in items:
        if int(item.total_duration_ms or 0) / total >= 0.25:
            return item
    return None
