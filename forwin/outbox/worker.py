from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from forwin.outbox import store as outbox_store


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutboxClaim:
    row_id: str
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: Mapping[str, object]
    worker_id: str
    lease_epoch: int
    attempts: int
    payload_error: str = ""


OutboxHandler = Callable[[OutboxClaim], None]


@dataclass(frozen=True)
class OutboxWorkerResult:
    claimed: bool = False
    processed: bool = False
    row_id: str = ""
    event_id: str = ""
    event_type: str = ""
    message: str = ""


class _HeartbeatHelper:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        claim: OutboxClaim,
        lease_seconds: float,
        interval_seconds: float,
    ) -> None:
        self._session_factory = session_factory
        self._claim = claim
        self._lease_seconds = lease_seconds
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._ownership_lost = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"outbox-heartbeat-{claim.row_id}",
            daemon=True,
        )

    @property
    def ownership_lost(self) -> bool:
        return self._ownership_lost.is_set()

    def start(self) -> None:
        self._thread.start()

    def stop_and_join(self) -> None:
        self._stop.set()
        join_timeout = max(0.1, min(self._lease_seconds, self._interval_seconds * 2))
        self._thread.join(timeout=join_timeout)
        if self._thread.is_alive():
            logger.error(
                "Outbox heartbeat did not stop before T2 row=%s epoch=%s",
                self._claim.row_id,
                self._claim.lease_epoch,
            )
            self._ownership_lost.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                with self._session_factory.begin() as session:
                    owned = outbox_store.heartbeat_outbox_event(
                        session,
                        self._claim,
                        lease_seconds=self._lease_seconds,
                    )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Outbox heartbeat failed row=%s epoch=%s",
                    self._claim.row_id,
                    self._claim.lease_epoch,
                )
                self._ownership_lost.set()
                return
            if not owned:
                logger.warning(
                    "Outbox heartbeat lost ownership row=%s epoch=%s",
                    self._claim.row_id,
                    self._claim.lease_epoch,
                )
                self._ownership_lost.set()
                return


def run_one_outbox_event(
    *,
    session_factory: Callable[[], Any],
    worker_id: str,
    handlers: dict[str, OutboxHandler],
    lease_seconds: float = 60.0,
    heartbeat_interval_seconds: float = 15.0,
    base_delay_seconds: float = 30.0,
    max_delay_seconds: float = 900.0,
) -> OutboxWorkerResult:
    owner = str(worker_id or "").strip()
    if not owner:
        raise ValueError("worker_id must be non-empty")
    lease_duration = _positive_finite_seconds(lease_seconds, "lease_seconds")
    heartbeat_interval = _positive_finite_seconds(
        heartbeat_interval_seconds,
        "heartbeat_interval_seconds",
    )
    if heartbeat_interval >= lease_duration:
        raise ValueError(
            "heartbeat_interval_seconds must be less than lease_seconds"
        )

    with session_factory.begin() as session:
        claim = outbox_store.claim_next_outbox_event(
            session,
            worker_id=owner,
            lease_seconds=lease_duration,
        )
        if claim is None:
            return OutboxWorkerResult(message="no_claimable_outbox_event")

    handler_error: Exception | None = None
    heartbeat: _HeartbeatHelper | None = None
    if claim.payload_error:
        handler_error = ValueError(claim.payload_error)
        logger.warning(
            "Outbox event has invalid payload event=%s error=%s",
            claim.event_id,
            claim.payload_error,
        )
    else:
        heartbeat = _HeartbeatHelper(
            session_factory=session_factory,
            claim=claim,
            lease_seconds=lease_duration,
            interval_seconds=heartbeat_interval,
        )
        heartbeat.start()
        try:
            handler = handlers.get(claim.event_type)
            if handler is None:
                raise RuntimeError(
                    f"No outbox handler registered for event type: {claim.event_type}"
                )
            handler(claim)
        except Exception as exc:  # noqa: BLE001
            handler_error = exc
            logger.exception(
                "Outbox worker failed event %s type=%s",
                claim.event_id,
                claim.event_type,
            )
        finally:
            heartbeat.stop_and_join()

    if heartbeat is not None and heartbeat.ownership_lost:
        return _result_for(claim, processed=False, message="lease_lost")

    with session_factory.begin() as session:
        if handler_error is not None:
            updated = outbox_store.release_outbox_event_for_retry(
                session,
                claim,
                error=handler_error,
                base_delay_seconds=base_delay_seconds,
                max_delay_seconds=max_delay_seconds,
            )
            if not updated:
                return _result_for(claim, processed=False, message="stale_claim")
            return _result_for(claim, processed=False, message="handler_failed")

        updated = outbox_store.mark_outbox_event_processed(session, claim)
        if not updated:
            return _result_for(claim, processed=False, message="stale_claim")
    return _result_for(claim, processed=True, message="processed")


def run_outbox_worker_loop(
    *,
    session_factory: Callable[[], Any],
    worker_id: str,
    handlers: dict[str, OutboxHandler] | None = None,
    poll_interval: float = 2.0,
    once: bool = False,
    max_loops: int = 0,
    lease_seconds: float = 60.0,
    heartbeat_interval_seconds: float = 15.0,
    base_delay_seconds: float = 30.0,
    max_delay_seconds: float = 900.0,
) -> int:
    normalized_handlers = handlers or {}
    loops = 0
    while True:
        loops += 1
        result = run_one_outbox_event(
            session_factory=session_factory,
            worker_id=worker_id,
            handlers=normalized_handlers,
            lease_seconds=lease_seconds,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            base_delay_seconds=base_delay_seconds,
            max_delay_seconds=max_delay_seconds,
        )
        if once:
            return 0
        if max_loops > 0 and loops >= max_loops:
            return 0
        if not result.claimed:
            time.sleep(max(0.0, float(poll_interval or 0.0)))


def _positive_finite_seconds(value: object, name: str) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be positive") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{name} must be positive")
    return seconds


def _result_for(
    claim: OutboxClaim,
    *,
    processed: bool,
    message: str,
) -> OutboxWorkerResult:
    return OutboxWorkerResult(
        claimed=True,
        processed=processed,
        row_id=claim.row_id,
        event_id=claim.event_id,
        event_type=claim.event_type,
        message=message,
    )
