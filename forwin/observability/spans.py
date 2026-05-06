from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
import time
from types import TracebackType
from typing import Any
from uuid import uuid4

from .context import OperationContext
from .redaction import redact_payload, stack_hash


_active_stack: ContextVar[tuple["SpanTimer", ...]] = ContextVar(
    "forwin_observability_span_stack",
    default=(),
)


def current_span() -> "SpanTimer | None":
    stack = _active_stack.get()
    return stack[-1] if stack else None


@dataclass(slots=True)
class SpanRecord:
    context: OperationContext
    span_name: str
    span_kind: str = "stage"
    component: str = ""
    tags: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, int | float] = field(default_factory=dict)
    status: str = "ok"
    error: dict[str, Any] = field(default_factory=dict)
    trace_id: str = ""
    span_id: str = ""
    parent_span_id: str = ""
    start_time_unix_ms: int = 0
    duration_ms: int = 0
    self_duration_ms: int = 0


class SpanTimer:
    def __init__(
        self,
        *,
        service: Any,
        context: OperationContext,
        span_name: str,
        span_kind: str = "stage",
        component: str = "",
        tags: dict[str, Any] | None = None,
        metrics: dict[str, int | float] | None = None,
        sampled: bool = True,
    ) -> None:
        stack = _active_stack.get()
        parent = stack[-1] if stack else None
        operation_id = str(context.operation_id or context.task_id or "").strip()
        self.service = service
        self.context = context
        self.span_name = str(span_name or "").strip() or "unknown"
        self.span_kind = str(span_kind or "stage").strip() or "stage"
        self.component = str(component or "").strip()
        self.tags: dict[str, Any] = dict(tags or {})
        self.metrics: dict[str, int | float] = dict(metrics or {})
        self.status = "ok"
        self.error: dict[str, Any] = {}
        self.trace_id = parent.trace_id if parent is not None else (operation_id or uuid4().hex)
        self.span_id = uuid4().hex
        self.parent_span_id = parent.span_id if parent is not None else ""
        self.start_time_unix_ms = int(time.time() * 1000)
        self._started_at = time.perf_counter()
        self._child_duration_ms = 0
        self._sampled = sampled
        self._token = None

    def __enter__(self) -> "SpanTimer":
        if self._sampled:
            self._token = _active_stack.set((*_active_stack.get(), self))
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        if exc is not None:
            self.status = "failed"
            self.error = redact_payload(
                {
                    "error_class": exc.__class__.__name__,
                    "error_message": str(exc),
                    "stack_hash": stack_hash(exc),
                }
            )
        if self._sampled:
            duration_ms = max(0, int((time.perf_counter() - self._started_at) * 1000))
            self_duration_ms = max(0, duration_ms - self._child_duration_ms)
            record = SpanRecord(
                context=self.context,
                span_name=self.span_name,
                span_kind=self.span_kind,
                component=self.component,
                tags=redact_payload(self.tags),
                metrics=dict(self.metrics),
                status=self.status,
                error=self.error,
                trace_id=self.trace_id,
                span_id=self.span_id,
                parent_span_id=self.parent_span_id,
                start_time_unix_ms=self.start_time_unix_ms,
                duration_ms=duration_ms,
                self_duration_ms=self_duration_ms,
            )
            stack = _active_stack.get()
            parent = stack[-2] if len(stack) >= 2 else None
            if parent is not None:
                parent._child_duration_ms += duration_ms
            if self._token is not None:
                _active_stack.reset(self._token)
            self.service._record_span(record)
        return False

    def tag(self, key: str, value: Any) -> None:
        normalized = str(key or "").strip()
        if normalized:
            self.tags[normalized] = value

    def metric(self, key: str, value: int | float) -> None:
        normalized = str(key or "").strip()
        if not normalized:
            return
        try:
            if isinstance(value, float):
                self.metrics[normalized] = float(value)
            else:
                self.metrics[normalized] = int(value)
        except (TypeError, ValueError):
            return

    def set_status(self, status: str) -> None:
        normalized = str(status or "").strip()
        if normalized:
            self.status = normalized
