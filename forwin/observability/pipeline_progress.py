"""Generation progress events and stage spans bound to the caller's audit transaction."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from forwin.audit.events import DecisionEventType
from forwin.observability.context import OperationContext
from forwin.observability.pipeline_trace import PipelineTraceRecorder
from forwin.observability.ports import SpanHandle
from forwin.observability.service import ObservabilityService
from forwin.state.updater import StateUpdater

logger = logging.getLogger(__name__)


class PipelineProgressRecorder:
    def __init__(
        self,
        *,
        trace_recorder: PipelineTraceRecorder,
        observability: ObservabilityService,
        progress_callback: Callable[[str, dict[str, object]], None] | None = None,
    ):
        self.trace_recorder = trace_recorder
        self.observability = observability
        self.progress_callback = progress_callback
        self._audit_project_id = ""
        self._audit_updater: StateUpdater | None = None
        self._audit_stage_name = ""
        self._audit_stage_started_at = 0.0
        self._audit_stage_chapter_number = 0
        self._audit_stage_span: SpanHandle | None = None

    def emit(self, event: str, **payload: Any) -> None:
        if event == "stage_changed":
            try:
                self._record_stage_transition(payload)
            except Exception:
                logger.debug("Ignoring stage transition tracking error.", exc_info=True)
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(event, payload)
        except Exception:
            logger.debug("Ignoring progress callback error.", exc_info=True)

    def bind(
        self,
        *,
        project_id: str,
        updater: StateUpdater,
    ) -> None:
        self._audit_project_id = str(project_id or "").strip()
        self._audit_updater = updater
        self._audit_stage_name = ""
        self._audit_stage_started_at = 0.0
        self._audit_stage_chapter_number = 0
        self._audit_stage_span = None

    def clear(self) -> None:
        self._finish_audit_stage_span(next_stage="", chapter_number=0)
        self._audit_project_id = ""
        self._audit_updater = None
        self._audit_stage_name = ""
        self._audit_stage_started_at = 0.0
        self._audit_stage_chapter_number = 0
        self._audit_stage_span = None

    def _start_audit_stage_span(
        self, *, project_id: str, stage: str, chapter_number: int
    ) -> None:
        if self._audit_stage_span is not None:
            return
        context = OperationContext(
            project_id=project_id,
            task_id=self.trace_recorder.audit.task_id,
            chapter_number=int(chapter_number or 0),
            stage=stage,
            operation_id=self.trace_recorder.audit.operation_id,
        )
        span = self.observability.span(
            context,
            f"stage.{stage}",
            span_kind="stage",
            component="pipeline",
            tags={"stage": stage},
        )
        span.__enter__()
        self._audit_stage_span = span

    def _finish_audit_stage_span(self, *, next_stage: str, chapter_number: int) -> None:
        span = self._audit_stage_span
        if span is None:
            return
        try:
            span.tag("next_stage", str(next_stage or ""))
            stage_chapter_number = int(
                getattr(self, "_audit_stage_chapter_number", 0) or chapter_number or 0
            )
            if stage_chapter_number:
                span.metric("chapter_number", stage_chapter_number)
            span.__exit__(None, None, None)
        except Exception:
            logger.debug(
                "Ignoring audit control stage span close failure.", exc_info=True
            )
        finally:
            self._audit_stage_span = None

    def _record_stage_transition(self, payload: dict[str, Any]) -> None:
        updater = self._audit_updater
        project_id = str(
            payload.get("project_id") or self._audit_project_id or ""
        ).strip()
        stage = str(payload.get("stage") or "").strip()
        if updater is None or not project_id or not stage:
            return
        now = time.perf_counter()
        chapter_number = int(payload.get("current_chapter") or 0)
        if self._audit_stage_name and self._audit_stage_name != stage:
            stage_chapter_number = int(
                getattr(self, "_audit_stage_chapter_number", 0) or chapter_number or 0
            )
            duration_ms = max(0, int((now - self._audit_stage_started_at) * 1000))
            stage_payload = {
                "stage": self._audit_stage_name,
                "next_stage": stage,
                "duration_ms": duration_ms,
            }
            self.trace_recorder.record_event(
                updater=updater,
                project_id=project_id,
                chapter_number=stage_chapter_number,
                event_family="runtime_observation",
                event_type=DecisionEventType.STAGE_EXITED,
                scope="task",
                summary=f"阶段 {self._audit_stage_name} 已结束。",
                payload=stage_payload,
            )
            self.trace_recorder.record_event(
                updater=updater,
                project_id=project_id,
                chapter_number=stage_chapter_number,
                event_family="runtime_observation",
                event_type=DecisionEventType.STAGE_DURATION_SUMMARY,
                scope="task",
                summary=f"阶段 {self._audit_stage_name} 用时 {duration_ms}ms。",
                payload=stage_payload,
            )
            self._finish_audit_stage_span(
                next_stage=stage, chapter_number=stage_chapter_number
            )
        if self._audit_stage_name != stage:
            self.trace_recorder.record_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="runtime_observation",
                event_type=DecisionEventType.STAGE_ENTERED,
                scope="task",
                summary=f"阶段 {stage} 已开始。",
                payload={"stage": stage},
            )
            self._audit_stage_name = stage
            self._audit_stage_started_at = now
            self._audit_stage_chapter_number = chapter_number
            self._start_audit_stage_span(
                project_id=project_id,
                stage=stage,
                chapter_number=chapter_number,
            )
