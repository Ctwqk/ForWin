from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from forwin.models.outbox import OutboxEvent
from forwin.outbox.store import (
    claim_next_outbox_event,
    mark_outbox_event_failed,
    mark_outbox_event_processed,
)


logger = logging.getLogger(__name__)

OutboxHandler = Callable[[OutboxEvent], None]


@dataclass(frozen=True)
class OutboxWorkerResult:
    claimed: bool = False
    processed: bool = False
    row_id: str = ""
    event_id: str = ""
    event_type: str = ""
    message: str = ""


def run_one_outbox_event(
    *,
    session_factory: Callable[[], Any],
    worker_id: str,
    handlers: dict[str, OutboxHandler],
    max_attempts: int = 3,
    retry_delay_seconds: int = 0,
) -> OutboxWorkerResult:
    with session_factory.begin() as session:
        event = claim_next_outbox_event(session, worker_id=worker_id)
        if event is None:
            return OutboxWorkerResult(message="no_claimable_outbox_event")
        row_id = event.id
        event_id = event.event_id
        event_type = event.event_type

    try:
        handler = handlers.get(event_type)
        if handler is None:
            raise RuntimeError(f"No outbox handler registered for event type: {event_type}")
        handler(event)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Outbox worker failed event %s type=%s", event_id, event_type)
        with session_factory.begin() as session:
            mark_outbox_event_failed(
                session,
                row_id=row_id,
                error_message=str(exc),
                max_attempts=max_attempts,
                retry_delay_seconds=retry_delay_seconds,
            )
        return OutboxWorkerResult(
            claimed=True,
            processed=False,
            row_id=row_id,
            event_id=event_id,
            event_type=event_type,
            message="handler_failed",
        )

    with session_factory.begin() as session:
        mark_outbox_event_processed(session, row_id=row_id)
    return OutboxWorkerResult(
        claimed=True,
        processed=True,
        row_id=row_id,
        event_id=event_id,
        event_type=event_type,
        message="processed",
    )


def run_outbox_worker_loop(
    *,
    session_factory: Callable[[], Any],
    worker_id: str,
    handlers: dict[str, OutboxHandler] | None = None,
    poll_interval: float = 2.0,
    once: bool = False,
    max_loops: int = 0,
    max_attempts: int = 3,
    retry_delay_seconds: int = 30,
) -> int:
    normalized_handlers = handlers or {}
    loops = 0
    while True:
        loops += 1
        result = run_one_outbox_event(
            session_factory=session_factory,
            worker_id=worker_id,
            handlers=normalized_handlers,
            max_attempts=max_attempts,
            retry_delay_seconds=retry_delay_seconds,
        )
        if once:
            return 0
        if max_loops > 0 and loops >= max_loops:
            return 0
        if not result.claimed:
            time.sleep(max(0.0, float(poll_interval or 0.0)))
