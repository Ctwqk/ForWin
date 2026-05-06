from __future__ import annotations

import logging
import time
from typing import Any

from .context import OperationContext
from .redaction import stack_hash
from .sinks import DecisionEventSink

logger = logging.getLogger(__name__)


class LogRecorder:
    def __init__(self, *, updater) -> None:
        self.updater = updater
        self.sink = DecisionEventSink(updater=updater)

    def record_event(
        self,
        context: OperationContext,
        *,
        event_family: str,
        event_type: str,
        summary: str,
        reason: str = "",
        scope: str = "project",
        actor_type: str = "",
        actor_id: str = "",
        payload: dict[str, Any] | None = None,
        related_object_type: str = "",
        related_object_id: str = "",
        parent_event_id: str = "",
        causal_root_id: str = "",
        band_id: str = "",
        chapter_number: int | None = None,
    ):
        row = self.sink.record_event(
            context,
            event_family=event_family,
            event_type=event_type,
            summary=summary,
            reason=reason,
            scope=scope,
            actor_type=actor_type,
            actor_id=actor_id,
            payload=payload,
            related_object_type=related_object_type,
            related_object_id=related_object_id,
            parent_event_id=parent_event_id,
            causal_root_id=causal_root_id,
            band_id=band_id,
            chapter_number=chapter_number,
            commit=False,
        )
        logger.info(
            "observability_event event_type=%s project_id=%s task_id=%s chapter=%s",
            event_type,
            context.project_id,
            context.task_id,
            context.chapter_number,
        )
        return row

    def record_error(
        self,
        context: OperationContext,
        *,
        event_type: str,
        summary: str,
        exc: BaseException,
        payload: dict[str, Any] | None = None,
        **kwargs: Any,
    ):
        error_payload = {
            **(payload or {}),
            "error_class": exc.__class__.__name__,
            "error_message": str(exc),
            "stack_hash": stack_hash(exc),
        }
        return self.record_event(
            context,
            event_family="runtime_observation",
            event_type=event_type,
            summary=summary,
            payload=error_payload,
            **kwargs,
        )

    def record_stage(
        self,
        context: OperationContext,
        *,
        event_type: str,
        summary: str,
        started_at: float | None = None,
        payload: dict[str, Any] | None = None,
        **kwargs: Any,
    ):
        stage_payload = dict(payload or {})
        if started_at is not None:
            stage_payload["duration_ms"] = max(0, int((time.perf_counter() - started_at) * 1000))
        return self.record_event(
            context,
            event_family="runtime_observation",
            event_type=event_type,
            summary=summary,
            payload=stage_payload,
            **kwargs,
        )
