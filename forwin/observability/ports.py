from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol

from .context import OperationContext


class SpanHandle(Protocol):
    span_id: str
    parent_span_id: str
    trace_id: str

    def tag(self, key: str, value: Any) -> None: ...

    def metric(self, key: str, value: int | float) -> None: ...

    def set_status(self, status: str) -> None: ...


class ObservabilityPort(Protocol):
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
    ) -> Any | None: ...

    def error(
        self,
        context: OperationContext,
        *,
        event_type: str,
        summary: str,
        exc: BaseException,
        payload: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any | None: ...

    def span(
        self,
        context: OperationContext,
        span_name: str,
        *,
        span_kind: str = "stage",
        component: str = "",
        tags: dict[str, Any] | None = None,
        metrics: dict[str, int | float] | None = None,
    ) -> AbstractContextManager[SpanHandle]: ...

    def prompt_trace(
        self,
        context: OperationContext,
        prompt_trace: dict[str, Any],
        *,
        artifact_store: Any | None = None,
        **kwargs: Any,
    ) -> Any | None: ...

    def flush(self) -> None: ...


class _NullSpan:
    span_id = ""
    parent_span_id = ""
    trace_id = ""

    def __enter__(self) -> "_NullSpan":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
        return False

    def tag(self, key: str, value: Any) -> None:
        return None

    def metric(self, key: str, value: int | float) -> None:
        return None

    def set_status(self, status: str) -> None:
        return None


class NullObservability:
    def event(self, context: OperationContext, **kwargs: Any) -> None:
        return None

    def error(self, context: OperationContext, **kwargs: Any) -> None:
        return None

    def span(self, context: OperationContext, span_name: str, **kwargs: Any) -> _NullSpan:
        return _NullSpan()

    def prompt_trace(self, context: OperationContext, prompt_trace: dict[str, Any], **kwargs: Any) -> None:
        return None

    def flush(self) -> None:
        return None
