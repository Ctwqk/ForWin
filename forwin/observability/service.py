from __future__ import annotations

import logging
from typing import Any

from .context import OperationContext
from .redaction import stack_hash
from .sampling import SpanSampler
from .sinks import DecisionEventSink, PerformanceSpanSink, PromptTraceSink, StdlibLogSink
from .spans import SpanRecord, SpanTimer

logger = logging.getLogger(__name__)


class ObservabilityService:
    def __init__(
        self,
        *,
        session_factory: Any | None = None,
        artifact_store: Any | None = None,
        config: Any | None = None,
        decision_event_sink: DecisionEventSink | None = None,
        performance_span_sink: PerformanceSpanSink | None = None,
        prompt_trace_sink: PromptTraceSink | None = None,
        stdlib_log_sink: StdlibLogSink | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.artifact_store = artifact_store
        self.config = config
        self.enabled = bool(getattr(config, "observability_enabled", True))
        self.sampler = SpanSampler.from_config(config)
        self.decision_events = decision_event_sink or DecisionEventSink(session_factory=session_factory)
        self.performance_spans = performance_span_sink or PerformanceSpanSink(session_factory=session_factory)
        self.prompt_traces = prompt_trace_sink or PromptTraceSink(
            session_factory=session_factory,
            artifact_store=artifact_store,
        )
        self.stdlib_logs = stdlib_log_sink or StdlibLogSink()

    def event(
        self,
        context: OperationContext,
        *,
        event_family: str,
        event_type: str,
        summary: str,
        scope: str = "project",
        payload: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any | None:
        if not self.enabled:
            return None
        try:
            row = self.decision_events.record_event(
                context,
                event_family=event_family,
                event_type=event_type,
                summary=summary,
                scope=scope,
                payload=payload or {},
                **kwargs,
            )
            self.stdlib_logs.record(
                "observability_event",
                event_type=event_type,
                project_id=context.project_id,
                task_id=context.task_id,
                chapter=context.chapter_number,
            )
            return row
        except Exception:
            logger.debug("Ignoring observability event failure.", exc_info=True)
            return None

    def error(
        self,
        context: OperationContext,
        *,
        event_type: str,
        summary: str,
        exc: BaseException,
        payload: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any | None:
        error_payload = {
            **(payload or {}),
            "error_class": exc.__class__.__name__,
            "error_message": str(exc),
            "stack_hash": stack_hash(exc),
        }
        return self.event(
            context,
            event_family="runtime_observation",
            event_type=event_type,
            summary=summary,
            payload=error_payload,
            **kwargs,
        )

    def span(
        self,
        context: OperationContext,
        span_name: str,
        *,
        span_kind: str = "stage",
        component: str = "",
        tags: dict[str, Any] | None = None,
        metrics: dict[str, int | float] | None = None,
    ) -> SpanTimer:
        return SpanTimer(
            service=self,
            context=context,
            span_name=span_name,
            span_kind=span_kind,
            component=component,
            tags=tags,
            metrics=metrics,
            sampled=self.sampler.should_start(),
        )

    def prompt_trace(
        self,
        context: OperationContext,
        prompt_trace: dict[str, Any],
        *,
        artifact_store: Any | None = None,
        **kwargs: Any,
    ) -> Any | None:
        if not self.enabled:
            return None
        try:
            return self.prompt_traces.record_prompt_trace(
                context,
                prompt_trace,
                artifact_store=artifact_store,
                **kwargs,
            )
        except Exception:
            logger.debug("Ignoring observability prompt trace failure.", exc_info=True)
            return None

    def flush(self) -> None:
        return None

    def _record_span(self, record: SpanRecord) -> Any | None:
        try:
            return self.performance_spans.record_span(record)
        except Exception:
            logger.debug("Ignoring performance span failure.", exc_info=True)
            return None
