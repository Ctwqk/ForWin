from __future__ import annotations

from collections.abc import Callable
from typing import Any

from forwin.canon.outbox_events import (
    CANON_PHASE3_REQUESTED,
    CanonPhase3EventPayload,
    parse_canon_event_envelope,
)
from forwin.outbox.worker import OutboxClaim

POST_CANON_STEP_NAMES = ("planning", "arc", "world", "feedback")
ORDER_CONTROLS_KEY = "order_controls"


def build_post_canon_outbox_handlers(
    *,
    service_provider: Callable[[], Any],
) -> dict[str, Callable[[OutboxClaim], None]]:
    def handle(event: OutboxClaim) -> None:
        parsed = parse_canon_event_envelope(
            event_type=event.event_type,
            event_id=event.event_id,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            payload=event.payload,
        )
        if not isinstance(parsed, CanonPhase3EventPayload):
            raise TypeError("Canon phase 3 event payload has the wrong type")
        service = service_provider()
        commit_id = service.resolve_event_canon_commit(
            canon_commit_id=parsed.canon_commit_id,
            canon_idempotency_key=parsed.canon_idempotency_key,
            project_id=parsed.project_id,
            chapter_number=parsed.chapter_number,
            candidate_id=parsed.candidate_id,
        )
        service.run(
            canon_commit_id=commit_id,
            worker_id=f"outbox:{event.worker_id}:{event.row_id}:{event.lease_epoch}",
        )

    return {CANON_PHASE3_REQUESTED: handle}


__all__ = [
    "ORDER_CONTROLS_KEY",
    "POST_CANON_STEP_NAMES",
    "build_post_canon_outbox_handlers",
]
