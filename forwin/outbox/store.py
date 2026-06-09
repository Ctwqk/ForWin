from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from forwin.models.base import new_id
from forwin.models.outbox import OutboxEvent


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def enqueue_outbox_event(
    session: Session,
    *,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    payload: dict[str, Any],
    event_id: str = "",
    available_at: datetime | None = None,
) -> OutboxEvent:
    row = OutboxEvent(
        id=new_id(),
        event_id=str(event_id or "").strip() or new_id(),
        aggregate_type=str(aggregate_type or "").strip(),
        aggregate_id=str(aggregate_id or "").strip(),
        event_type=str(event_type or "").strip(),
        payload_json=json.dumps(payload or {}, ensure_ascii=False),
        status="pending",
        attempts=0,
        available_at=available_at,
    )
    session.add(row)
    return row


def claim_next_outbox_event(
    session: Session,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> OutboxEvent | None:
    timestamp = now or utcnow()
    row = (
        session.execute(
            select(OutboxEvent)
            .where(
                OutboxEvent.status == "pending",
                or_(OutboxEvent.available_at.is_(None), OutboxEvent.available_at <= timestamp),
            )
            .order_by(OutboxEvent.created_at.asc(), OutboxEvent.id.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        .scalars()
        .first()
    )
    if row is None:
        return None
    row.status = "running"
    row.attempts = int(row.attempts or 0) + 1
    row.locked_by = str(worker_id or "").strip()
    row.locked_at = timestamp
    row.error_message = ""
    session.add(row)
    return row


def mark_outbox_event_processed(
    session: Session,
    *,
    row_id: str,
    now: datetime | None = None,
) -> None:
    row = session.get(OutboxEvent, row_id)
    if row is None:
        return
    row.status = "processed"
    row.processed_at = now or utcnow()
    row.locked_by = ""
    row.locked_at = None
    session.add(row)


def mark_outbox_event_failed(
    session: Session,
    *,
    row_id: str,
    error_message: str,
    max_attempts: int = 3,
    retry_delay_seconds: int = 30,
    now: datetime | None = None,
) -> None:
    row = session.get(OutboxEvent, row_id)
    if row is None:
        return
    timestamp = now or utcnow()
    row.error_message = str(error_message or "").strip()
    row.locked_by = ""
    row.locked_at = None
    if int(row.attempts or 0) >= max(1, int(max_attempts or 1)):
        row.status = "failed"
        row.available_at = None
    else:
        row.status = "pending"
        row.available_at = timestamp + timedelta(seconds=max(0, int(retry_delay_seconds or 0)))
    session.add(row)
