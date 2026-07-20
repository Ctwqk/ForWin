from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from forwin.models.base import new_id
from forwin.models.outbox import OutboxEvent


if TYPE_CHECKING:
    from forwin.outbox.worker import OutboxClaim


MAX_ERROR_LENGTH = 4000


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
    lease_seconds: float,
    now: datetime | None = None,
) -> OutboxClaim | None:
    from forwin.outbox.worker import OutboxClaim

    owner = str(worker_id or "").strip()
    if not owner:
        raise ValueError("worker_id must be non-empty")
    lease_duration = _positive_seconds(lease_seconds, name="lease_seconds")
    timestamp = now or utcnow()
    row = (
        session.execute(
            select(OutboxEvent)
            .where(
                or_(
                    and_(
                        OutboxEvent.status == "pending",
                        or_(
                            OutboxEvent.available_at.is_(None),
                            OutboxEvent.available_at <= timestamp,
                        ),
                    ),
                    and_(
                        OutboxEvent.status == "running",
                        OutboxEvent.lease_expires_at.is_not(None),
                        OutboxEvent.lease_expires_at <= timestamp,
                    ),
                )
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
    row.worker_id = owner
    row.lease_epoch = int(row.lease_epoch or 0) + 1
    row.heartbeat_at = timestamp
    row.lease_expires_at = timestamp + timedelta(seconds=lease_duration)
    row.available_at = None
    row.error_message = ""
    payload, payload_error = _decode_payload(row.payload_json)
    return OutboxClaim(
        row_id=str(row.id),
        event_id=str(row.event_id),
        event_type=str(row.event_type),
        aggregate_type=str(row.aggregate_type),
        aggregate_id=str(row.aggregate_id),
        payload=payload,
        worker_id=owner,
        lease_epoch=row.lease_epoch,
        attempts=row.attempts,
        payload_error=payload_error,
    )


def heartbeat_outbox_event(
    session: Session,
    claim: OutboxClaim,
    *,
    lease_seconds: float,
    now: datetime | None = None,
) -> bool:
    lease_duration = _positive_seconds(lease_seconds, name="lease_seconds")
    timestamp = now or utcnow()
    result = session.execute(
        _fenced_update(claim).values(
            heartbeat_at=timestamp,
            lease_expires_at=timestamp + timedelta(seconds=lease_duration),
        )
    )
    return result.rowcount == 1


def mark_outbox_event_processed(
    session: Session,
    claim: OutboxClaim,
    *,
    now: datetime | None = None,
) -> bool:
    timestamp = now or utcnow()
    result = session.execute(
        _fenced_update(claim).values(
            status="processed",
            processed_at=timestamp,
            worker_id="",
            lease_expires_at=None,
            heartbeat_at=None,
            available_at=None,
            error_message="",
        )
    )
    return result.rowcount == 1


def release_outbox_event_for_retry(
    session: Session,
    claim: OutboxClaim,
    *,
    error: object,
    base_delay_seconds: float,
    max_delay_seconds: float,
    now: datetime | None = None,
) -> bool:
    timestamp = now or utcnow()
    delay = _retry_delay_seconds(
        attempts=claim.attempts,
        base_delay_seconds=base_delay_seconds,
        max_delay_seconds=max_delay_seconds,
    )
    try:
        available_at = timestamp + timedelta(seconds=delay)
    except OverflowError:
        available_at = datetime.max.replace(tzinfo=timestamp.tzinfo)
    result = session.execute(
        _fenced_update(claim).values(
            status="pending",
            error_message=_sanitize_error(error),
            worker_id="",
            lease_expires_at=None,
            heartbeat_at=None,
            available_at=available_at,
        )
    )
    return result.rowcount == 1


def _fenced_update(claim: OutboxClaim):
    return (
        update(OutboxEvent)
        .where(
            OutboxEvent.id == claim.row_id,
            OutboxEvent.status == "running",
            OutboxEvent.worker_id == claim.worker_id,
            OutboxEvent.lease_epoch == claim.lease_epoch,
        )
        .execution_options(synchronize_session=False)
    )


def _decode_payload(payload_json: str) -> tuple[MappingProxyType[str, object], str]:
    try:
        payload = json.loads(payload_json)
    except RecursionError as exc:
        return (
            MappingProxyType({}),
            f"outbox payload_json parsing exceeded recursion limit: {exc}",
        )
    except (TypeError, json.JSONDecodeError) as exc:
        return MappingProxyType({}), f"outbox payload_json contains invalid JSON: {exc}"

    if not isinstance(payload, dict):
        return MappingProxyType({}), "outbox payload_json must decode to a JSON object"

    try:
        frozen_payload = _freeze_mapping(payload)
    except RecursionError as exc:
        return (
            MappingProxyType({}),
            f"outbox payload_json freezing exceeded recursion limit: {exc}",
        )
    return frozen_payload, ""


def _freeze_mapping(payload: dict[str, object]) -> MappingProxyType[str, object]:
    return MappingProxyType(
        {str(key): _freeze_json_value(value) for key, value in payload.items()}
    )


def _freeze_json_value(value: object) -> object:
    if isinstance(value, dict):
        return _freeze_mapping(value)
    if isinstance(value, list):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _positive_seconds(value: object, *, name: str) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be positive") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{name} must be positive")
    return seconds


def _nonnegative_seconds(value: object) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(seconds) or seconds <= 0:
        return 0.0
    return seconds


def _retry_delay_seconds(
    *,
    attempts: int,
    base_delay_seconds: object,
    max_delay_seconds: object,
) -> float:
    base_delay = _nonnegative_seconds(base_delay_seconds)
    max_delay = _nonnegative_seconds(max_delay_seconds)
    if base_delay == 0 or max_delay == 0:
        return 0.0
    if base_delay >= max_delay:
        return max_delay
    exponent = max(int(attempts or 0) - 1, 0)
    try:
        delay = math.ldexp(base_delay, exponent)
    except (OverflowError, TypeError):
        return max_delay
    if not math.isfinite(delay):
        return max_delay
    return min(max_delay, delay)


def _sanitize_error(error: object) -> str:
    text = str(error or "").replace("\x00", "")
    return " ".join(text.split())[:MAX_ERROR_LENGTH]
