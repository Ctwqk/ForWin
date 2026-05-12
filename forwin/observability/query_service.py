from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from forwin.api_schemas import PerformanceReportResponse, PerformanceSpanInfo
from forwin.models.observability import PerformanceSpan

from .performance import CriticalPathAnalyzer, PerformanceAnalyzer
from .recommendations import build_performance_recommendations


class ObservabilityQueryService:
    def __init__(self, *, session_factory: Any, display_datetime=None) -> None:
        self.session_factory = session_factory
        self.display_datetime = display_datetime or (lambda value: value.isoformat() if value else "")
        self.performance = PerformanceAnalyzer()
        self.critical_path = CriticalPathAnalyzer()

    def task_performance_report(self, task_id: str) -> PerformanceReportResponse:
        spans = self._span_infos(task_id=str(task_id or "").strip())
        return self._report(spans, task_id=str(task_id or "").strip())

    def project_performance_report(self, project_id: str, *, limit: int = 1000) -> PerformanceReportResponse:
        spans = self._span_infos(project_id=str(project_id or "").strip(), limit=limit)
        return self._report(spans, project_id=str(project_id or "").strip())

    def chapter_performance_report(
        self,
        project_id: str,
        chapter_number: int,
        *,
        limit: int = 1000,
    ) -> PerformanceReportResponse:
        spans = self._span_infos(
            project_id=str(project_id or "").strip(),
            chapter_number=max(0, int(chapter_number or 0)),
            limit=limit,
        )
        return self._report(
            spans,
            project_id=str(project_id or "").strip(),
            chapter_number=max(0, int(chapter_number or 0)),
        )

    def slow_spans(
        self,
        *,
        project_id: str = "",
        task_id: str = "",
        limit: int = 50,
    ) -> list[PerformanceSpanInfo]:
        return self._slow_span_infos(project_id=project_id, task_id=task_id, limit=max(1, int(limit or 50)))

    def llm_performance_report(self, *, project_id: str = "", days: int = 7) -> PerformanceReportResponse:
        spans = [span for span in self._span_infos(project_id=project_id, limit=2000) if span.span_kind == "llm"]
        return self._report(spans, project_id=project_id)

    def db_performance_report(self, *, project_id: str = "", days: int = 7) -> PerformanceReportResponse:
        spans = [
            span
            for span in self._span_infos(project_id=project_id, limit=2000)
            if span.span_kind == "db" or any(str(key).startswith("db.") for key in span.metrics)
        ]
        return self._report(spans, project_id=project_id)

    def _span_infos(
        self,
        *,
        project_id: str = "",
        task_id: str = "",
        chapter_number: int = 0,
        limit: int = 1000,
    ) -> list[PerformanceSpanInfo]:
        with self.session_factory() as session:
            stmt = select(PerformanceSpan)
            if project_id:
                stmt = stmt.where(PerformanceSpan.project_id == project_id)
            if task_id:
                stmt = stmt.where(PerformanceSpan.task_id == task_id)
            if chapter_number:
                stmt = stmt.where(PerformanceSpan.chapter_number == int(chapter_number))
            stmt = stmt.order_by(PerformanceSpan.created_at.asc(), PerformanceSpan.id.asc()).limit(max(1, int(limit or 1000)))
            rows = session.execute(stmt).scalars().all()
        return [self._span_info(row) for row in rows]

    def _slow_span_infos(
        self,
        *,
        project_id: str = "",
        task_id: str = "",
        limit: int = 50,
    ) -> list[PerformanceSpanInfo]:
        with self.session_factory() as session:
            stmt = select(PerformanceSpan)
            if project_id:
                stmt = stmt.where(PerformanceSpan.project_id == project_id)
            if task_id:
                stmt = stmt.where(PerformanceSpan.task_id == task_id)
            stmt = stmt.order_by(
                PerformanceSpan.duration_ms.desc(),
                PerformanceSpan.created_at.asc(),
                PerformanceSpan.id.asc(),
            ).limit(max(1, int(limit or 50)))
            rows = session.execute(stmt).scalars().all()
        return [self._span_info(row) for row in rows]

    def _report(
        self,
        spans: list[PerformanceSpanInfo],
        *,
        project_id: str = "",
        task_id: str = "",
        chapter_number: int = 0,
    ) -> PerformanceReportResponse:
        project_id = project_id or (spans[0].project_id if spans else "")
        task_id = task_id or (spans[0].task_id if spans else "")
        total_duration = max([span.duration_ms for span in spans], default=0)
        report = PerformanceReportResponse(
            project_id=project_id,
            task_id=task_id,
            chapter_number=chapter_number,
            total_duration_ms=total_duration,
            top_slow_spans=sorted(spans, key=lambda span: (-span.duration_ms, span.created_at))[:20],
            critical_path=self.critical_path.critical_path(spans),
            component_breakdown=self.performance.breakdown(spans, key_fn=lambda span: span.component or span.span_kind),
            stage_breakdown=self.performance.breakdown(spans, key_fn=lambda span: span.span_name),
            llm_breakdown=self.performance.breakdown(
                [span for span in spans if span.span_kind == "llm" or span.span_name.startswith("llm.")],
                key_fn=lambda span: (
                    f"{span.tags.get('stage_key') or span.stage or span.span_name}:"
                    f"{span.tags.get('model') or span.tags.get('profile_id') or 'unknown'}"
                ),
            ),
            db_breakdown=self.performance.breakdown(
                [
                    span
                    for span in spans
                    if span.span_kind == "db" or any(str(key).startswith("db.") for key in span.metrics)
                ],
                key_fn=lambda span: span.span_name,
            ),
        )
        report.recommendations = build_performance_recommendations(report)
        return report

    def _span_info(self, row: PerformanceSpan) -> PerformanceSpanInfo:
        return PerformanceSpanInfo(
            span_id=str(row.span_id or ""),
            parent_span_id=str(row.parent_span_id or ""),
            trace_id=str(row.trace_id or ""),
            span_name=str(row.span_name or ""),
            span_kind=str(row.span_kind or ""),
            component=str(row.component or ""),
            stage=str(row.stage or ""),
            status=str(row.status or "ok"),
            project_id=str(row.project_id or ""),
            task_id=str(row.task_id or ""),
            operation_id=str(row.operation_id or ""),
            chapter_number=int(row.chapter_number or 0),
            duration_ms=int(row.duration_ms or 0),
            self_duration_ms=int(row.self_duration_ms or 0),
            tags=_json_object(row.tags_json),
            metrics=_json_object(row.metrics_json),
            error=_json_object(row.error_json),
            created_at=self.display_datetime(row.created_at),
        )


def _json_object(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
