from __future__ import annotations

import logging
import time
from typing import Any

from forwin.governance import DecisionEventInfo, ensure_decision_event_type
from .context import OperationContext
from .redaction import redact_payload, stack_hash

logger = logging.getLogger(__name__)


class LogRecorder:
    def __init__(self, *, updater) -> None:
        self.updater = updater

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
        normalized_payload: dict[str, Any] = {
            **context.payload_fields(),
            **(payload or {}),
        }
        row = self.updater.save_decision_event(
            DecisionEventInfo(
                project_id=context.project_id,
                task_id=context.task_id,
                band_id=band_id or context.band_id,
                chapter_number=(
                    int(chapter_number)
                    if chapter_number is not None
                    else int(context.chapter_number or 0)
                ),
                scope=scope,
                event_family=event_family,
                event_type=ensure_decision_event_type(event_type),
                actor_type=actor_type or context.actor_type,
                actor_id=actor_id or context.actor_id,
                summary=summary,
                reason=reason,
                payload=redact_payload(normalized_payload),
                related_object_type=related_object_type,
                related_object_id=related_object_id,
                parent_event_id=parent_event_id or context.parent_event_id,
                causal_root_id=causal_root_id or context.causal_root_id,
            )
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
