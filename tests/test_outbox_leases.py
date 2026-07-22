from __future__ import annotations

import dataclasses
import json
import sys
import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql import Select

from forwin.models.outbox import OutboxEvent
from forwin.outbox import store as outbox_store
from forwin.outbox import worker as outbox_worker


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
SQLITE_NOW = NOW.replace(tzinfo=None)


def _row(
    *,
    status: str = "pending",
    attempts: int = 0,
    lease_epoch: int = 0,
    worker_id: str = "",
    lease_expires_at: datetime | None = None,
    payload_json: str = '{"project_id": "project-1", "nested": {"value": 1}}',
) -> OutboxEvent:
    return OutboxEvent(
        id="row-1",
        event_id="event-1",
        aggregate_type="project",
        aggregate_id="project-1",
        event_type="test.event",
        payload_json=payload_json,
        status=status,
        attempts=attempts,
        available_at=None,
        worker_id=worker_id,
        lease_epoch=lease_epoch,
        lease_expires_at=lease_expires_at,
        heartbeat_at=None,
        processed_at=None,
        error_message="old error",
        created_at=NOW - timedelta(hours=1),
        updated_at=NOW - timedelta(hours=1),
    )


class _ScalarResult:
    def __init__(self, row: OutboxEvent | None) -> None:
        self._row = row

    def scalars(self) -> _ScalarResult:
        return self

    def first(self) -> OutboxEvent | None:
        return self._row


class _RecordingSession:
    def __init__(
        self,
        *,
        selected_row: OutboxEvent | None = None,
        rowcount: int = 1,
    ) -> None:
        self.selected_row = selected_row
        self.rowcount = rowcount
        self.statements: list[Any] = []

    def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        if isinstance(statement, Select):
            return _ScalarResult(self.selected_row)
        return SimpleNamespace(rowcount=self.rowcount)

    def add(self, _row: object) -> None:
        return None


class _Transaction(AbstractContextManager[_RecordingSession]):
    def __init__(self, factory: _SessionFactory, session: _RecordingSession) -> None:
        self.factory = factory
        self.session = session

    def __enter__(self) -> _RecordingSession:
        self.factory.started += 1
        return self.session

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if exc_type is None:
            self.factory.committed += 1
        else:
            self.factory.rolled_back += 1
        return False


class _SessionFactory:
    def __init__(self, sessions: list[_RecordingSession] | None = None) -> None:
        self.sessions = list(sessions or [])
        self.started = 0
        self.committed = 0
        self.rolled_back = 0
        self.created: list[_RecordingSession] = []
        self._lock = threading.Lock()

    def begin(self) -> _Transaction:
        with self._lock:
            session = self.sessions.pop(0) if self.sessions else _RecordingSession()
            self.created.append(session)
        return _Transaction(self, session)


@pytest.fixture
def sqlite_outbox_sessions() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    OutboxEvent.__table__.create(engine)
    assert inspect(engine).get_table_names() == ["outbox_events"]
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield sessions
    finally:
        engine.dispose()


def _relational_row(
    row_id: str,
    *,
    status: str = "pending",
    attempts: int = 0,
    available_at: datetime | None = None,
    worker_id: str = "",
    lease_epoch: int = 0,
    lease_expires_at: datetime | None = None,
) -> OutboxEvent:
    return OutboxEvent(
        id=row_id,
        event_id=f"event-{row_id}",
        aggregate_type="project",
        aggregate_id="project-1",
        event_type="test.event",
        payload_json='{"project_id": "project-1"}',
        status=status,
        attempts=attempts,
        available_at=available_at,
        worker_id=worker_id,
        lease_epoch=lease_epoch,
        lease_expires_at=lease_expires_at,
        heartbeat_at=None,
        processed_at=None,
        error_message="old error",
        created_at=SQLITE_NOW - timedelta(hours=1),
        updated_at=SQLITE_NOW - timedelta(hours=1),
    )


def _claim(
    *,
    worker_id: str = "worker-1",
    lease_epoch: int = 1,
    attempts: int = 1,
    payload: dict[str, object] | None = None,
    payload_error: str = "",
):
    return outbox_worker.OutboxClaim(
        row_id="row-1",
        event_id="event-1",
        event_type="test.event",
        aggregate_type="project",
        aggregate_id="project-1",
        payload=MappingProxyType(payload or {"project_id": "project-1"}),
        payload_error=payload_error,
        worker_id=worker_id,
        lease_epoch=lease_epoch,
        attempts=attempts,
    )


def _compiled(statement: Any) -> tuple[str, dict[str, object]]:
    compiled = statement.compile(dialect=postgresql.dialect())
    return str(compiled), compiled.params


def test_sqlite_claim_excludes_future_pending_and_unexpired_running(
    sqlite_outbox_sessions: sessionmaker[Session],
) -> None:
    with sqlite_outbox_sessions.begin() as session:
        session.add_all(
            [
                _relational_row(
                    "future-pending",
                    available_at=SQLITE_NOW + timedelta(seconds=30),
                ),
                _relational_row(
                    "live-running",
                    status="running",
                    attempts=2,
                    worker_id="live-worker",
                    lease_epoch=2,
                    lease_expires_at=SQLITE_NOW + timedelta(seconds=30),
                ),
            ]
        )

    with sqlite_outbox_sessions.begin() as session:
        claim = outbox_store.claim_next_outbox_event(
            session,
            worker_id="other-worker",
            lease_seconds=60,
            now=SQLITE_NOW,
        )

    assert claim is None
    with sqlite_outbox_sessions() as session:
        future = session.get(OutboxEvent, "future-pending")
        live = session.get(OutboxEvent, "live-running")
        assert future is not None
        assert future.status == "pending"
        assert future.worker_id == ""
        assert future.lease_epoch == 0
        assert live is not None
        assert live.status == "running"
        assert live.worker_id == "live-worker"
        assert live.lease_epoch == 2


def test_sqlite_claim_reclaims_expired_running_row(
    sqlite_outbox_sessions: sessionmaker[Session],
) -> None:
    with sqlite_outbox_sessions.begin() as session:
        session.add(
            _relational_row(
                "expired-running",
                status="running",
                attempts=4,
                worker_id="dead-worker",
                lease_epoch=7,
                lease_expires_at=SQLITE_NOW - timedelta(seconds=1),
            )
        )

    with sqlite_outbox_sessions.begin() as session:
        claim = outbox_store.claim_next_outbox_event(
            session,
            worker_id="recovery-worker",
            lease_seconds=30,
            now=SQLITE_NOW,
        )

    assert claim is not None
    assert claim.worker_id == "recovery-worker"
    assert claim.lease_epoch == 8
    assert claim.attempts == 5
    with sqlite_outbox_sessions() as session:
        row = session.get(OutboxEvent, "expired-running")
        assert row is not None
        assert row.status == "running"
        assert row.worker_id == "recovery-worker"
        assert row.lease_epoch == 8
        assert row.attempts == 5
        assert row.heartbeat_at == SQLITE_NOW
        assert row.lease_expires_at == SQLITE_NOW + timedelta(seconds=30)
        assert row.error_message == ""


def test_sqlite_heartbeat_blocks_reclaim_until_extended_lease_expires(
    sqlite_outbox_sessions: sessionmaker[Session],
) -> None:
    with sqlite_outbox_sessions.begin() as session:
        session.add(_relational_row("heartbeat-row"))
    with sqlite_outbox_sessions.begin() as session:
        first_claim = outbox_store.claim_next_outbox_event(
            session,
            worker_id="worker-1",
            lease_seconds=10,
            now=SQLITE_NOW,
        )

    assert first_claim is not None
    with sqlite_outbox_sessions.begin() as session:
        assert outbox_store.heartbeat_outbox_event(
            session,
            first_claim,
            lease_seconds=10,
            now=SQLITE_NOW + timedelta(seconds=8),
        )
    with sqlite_outbox_sessions() as session:
        row = session.get(OutboxEvent, "heartbeat-row")
        assert row is not None
        assert row.heartbeat_at == SQLITE_NOW + timedelta(seconds=8)
        assert row.lease_expires_at == SQLITE_NOW + timedelta(seconds=18)

    with sqlite_outbox_sessions.begin() as session:
        blocked_claim = outbox_store.claim_next_outbox_event(
            session,
            worker_id="worker-2",
            lease_seconds=10,
            now=SQLITE_NOW + timedelta(seconds=11),
        )
    assert blocked_claim is None

    with sqlite_outbox_sessions.begin() as session:
        recovered_claim = outbox_store.claim_next_outbox_event(
            session,
            worker_id="worker-2",
            lease_seconds=10,
            now=SQLITE_NOW + timedelta(seconds=19),
        )

    assert recovered_claim is not None
    assert recovered_claim.worker_id == "worker-2"
    assert recovered_claim.lease_epoch == first_claim.lease_epoch + 1
    assert recovered_claim.attempts == first_claim.attempts + 1


def test_sqlite_reclaim_rejects_all_stale_fenced_updates(
    sqlite_outbox_sessions: sessionmaker[Session],
) -> None:
    old_claim = _claim(worker_id="old-worker", lease_epoch=4, attempts=7)
    with sqlite_outbox_sessions.begin() as session:
        session.add(
            _relational_row(
                "row-1",
                status="running",
                attempts=7,
                worker_id="old-worker",
                lease_epoch=4,
                lease_expires_at=SQLITE_NOW - timedelta(seconds=1),
            )
        )
    with sqlite_outbox_sessions.begin() as session:
        current_claim = outbox_store.claim_next_outbox_event(
            session,
            worker_id="new-worker",
            lease_seconds=30,
            now=SQLITE_NOW,
        )

    assert current_claim is not None
    assert current_claim.worker_id == "new-worker"
    assert current_claim.lease_epoch == 5
    assert current_claim.attempts == 8
    with sqlite_outbox_sessions.begin() as session:
        assert not outbox_store.heartbeat_outbox_event(
            session,
            old_claim,
            lease_seconds=60,
            now=SQLITE_NOW + timedelta(seconds=1),
        )
        assert session.execute(text("SELECT changes()")).scalar_one() == 0
    with sqlite_outbox_sessions.begin() as session:
        assert not outbox_store.mark_outbox_event_processed(
            session,
            old_claim,
            now=SQLITE_NOW + timedelta(seconds=1),
        )
        assert session.execute(text("SELECT changes()")).scalar_one() == 0
    with sqlite_outbox_sessions.begin() as session:
        assert not outbox_store.release_outbox_event_for_retry(
            session,
            old_claim,
            error="stale failure",
            base_delay_seconds=10,
            max_delay_seconds=100,
            now=SQLITE_NOW + timedelta(seconds=1),
        )
        assert session.execute(text("SELECT changes()")).scalar_one() == 0

    with sqlite_outbox_sessions() as session:
        row = session.get(OutboxEvent, "row-1")
        assert row is not None
        assert row.status == "running"
        assert row.worker_id == "new-worker"
        assert row.lease_epoch == 5
        assert row.attempts == 8
        assert row.heartbeat_at == SQLITE_NOW
        assert row.lease_expires_at == SQLITE_NOW + timedelta(seconds=30)
        assert row.available_at is None
        assert row.error_message == ""

    with sqlite_outbox_sessions.begin() as session:
        assert outbox_store.mark_outbox_event_processed(
            session,
            current_claim,
            now=SQLITE_NOW + timedelta(seconds=2),
        )
        assert session.execute(text("SELECT changes()")).scalar_one() == 1
    with sqlite_outbox_sessions() as session:
        row = session.get(OutboxEvent, "row-1")
        assert row is not None
        assert row.status == "processed"
        assert row.worker_id == ""
        assert row.lease_epoch == 5
        assert row.lease_expires_at is None
        assert row.heartbeat_at is None
        assert row.available_at is None
        assert row.processed_at == SQLITE_NOW + timedelta(seconds=2)


def test_claim_due_pending_event_returns_immutable_detached_snapshot() -> None:
    row = _row()
    session = _RecordingSession(selected_row=row)

    claim = outbox_store.claim_next_outbox_event(
        session,
        worker_id="worker-1",
        lease_seconds=60,
        now=NOW,
    )

    assert isinstance(claim, outbox_worker.OutboxClaim)
    assert claim.row_id == row.id
    assert claim.attempts == 1
    assert claim.lease_epoch == 1
    assert claim.worker_id == "worker-1"
    assert claim.payload_error == ""
    assert claim.payload["project_id"] == "project-1"
    assert row.status == "running"
    assert row.worker_id == "worker-1"
    assert row.heartbeat_at == NOW
    assert row.lease_expires_at == NOW + timedelta(seconds=60)
    assert row.error_message == ""
    with pytest.raises(dataclasses.FrozenInstanceError):
        claim.worker_id = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        claim.payload["project_id"] = "other"  # type: ignore[index]
    with pytest.raises(TypeError):
        claim.payload["nested"]["value"] = 2  # type: ignore[index]


def test_claim_query_accepts_due_pending_or_expired_running_only() -> None:
    session = _RecordingSession(selected_row=None)

    assert (
        outbox_store.claim_next_outbox_event(
            session,
            worker_id="worker-1",
            lease_seconds=60,
            now=NOW,
        )
        is None
    )

    sql, params = _compiled(session.statements[0])
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "outbox_events.status =" in sql
    assert "outbox_events.available_at IS NULL" in sql
    assert "outbox_events.available_at <=" in sql
    assert "outbox_events.lease_expires_at IS NOT NULL" in sql
    assert "outbox_events.lease_expires_at <=" in sql
    assert "pending" in params.values()
    assert "running" in params.values()
    assert NOW in params.values()


def test_claim_requires_owner_and_positive_lease() -> None:
    with pytest.raises(ValueError, match="worker_id"):
        outbox_store.claim_next_outbox_event(
            _RecordingSession(), worker_id=" ", lease_seconds=60, now=NOW
        )
    with pytest.raises(ValueError, match="lease_seconds"):
        outbox_store.claim_next_outbox_event(
            _RecordingSession(), worker_id="worker-1", lease_seconds=0, now=NOW
        )


@pytest.mark.parametrize(
    ("payload_json", "error_fragment"),
    [
        ("", "invalid JSON"),
        ("{not-json", "invalid JSON"),
        ("[]", "JSON object"),
    ],
)
def test_payload_error_is_explicit_on_committable_claim(
    payload_json: str,
    error_fragment: str,
) -> None:
    row = _row(payload_json=payload_json)

    claim = outbox_store.claim_next_outbox_event(
        _RecordingSession(selected_row=row),
        worker_id="worker-1",
        lease_seconds=60,
        now=NOW,
    )

    assert claim is not None
    assert error_fragment in claim.payload_error
    assert dict(claim.payload) == {}
    assert row.status == "running"
    assert row.lease_epoch == 1


def test_parse_recursion_error_is_an_explicit_committable_payload_error() -> None:
    depth = sys.getrecursionlimit() * 10
    payload_json = '{"nested":' * depth + "null" + "}" * depth
    row = _row(payload_json=payload_json)

    with pytest.raises(RecursionError):
        json.loads(payload_json)

    claim = outbox_store.claim_next_outbox_event(
        _RecordingSession(selected_row=row),
        worker_id="worker-1",
        lease_seconds=60,
        now=NOW,
    )

    assert claim is not None
    assert "parsing exceeded recursion limit" in claim.payload_error
    assert dict(claim.payload) == {}
    assert row.status == "running"
    assert row.lease_epoch == 1


def test_freeze_recursion_error_commits_t1_and_worker_fenced_retries(
    monkeypatch,
) -> None:
    row = _row()
    factory = _SessionFactory(
        [
            _RecordingSession(selected_row=row),
            _RecordingSession(rowcount=1),
        ]
    )
    handled: list[object] = []
    retried_claims: list[outbox_worker.OutboxClaim] = []
    original_retry = outbox_store.release_outbox_event_for_retry

    def fail_freeze(_payload: dict[str, object]) -> MappingProxyType[str, object]:
        raise RecursionError("freeze nesting too deep")

    def capture_retry(session, claim, **kwargs) -> bool:
        retried_claims.append(claim)
        return original_retry(session, claim, **kwargs)

    monkeypatch.setattr(outbox_store, "_freeze_mapping", fail_freeze)
    monkeypatch.setattr(outbox_store, "release_outbox_event_for_retry", capture_retry)

    result = outbox_worker.run_one_outbox_event(
        session_factory=factory,
        worker_id="worker-1",
        handlers={"test.event": handled.append},
        lease_seconds=60,
        heartbeat_interval_seconds=15,
        base_delay_seconds=10,
        max_delay_seconds=50,
    )

    assert result.message == "handler_failed"
    assert handled == []
    assert factory.committed == 2
    assert factory.rolled_back == 0
    assert row.status == "running"
    assert len(retried_claims) == 1
    assert "freezing exceeded recursion limit" in retried_claims[0].payload_error
    retry_sql, retry_params = _compiled(factory.created[1].statements[0])
    assert "UPDATE outbox_events SET" in retry_sql
    assert "pending" in retry_params.values()


def test_heartbeat_is_fenced_and_advances_expiry() -> None:
    session = _RecordingSession(rowcount=1)
    claim = _claim(lease_epoch=3)

    assert outbox_store.heartbeat_outbox_event(
        session,
        claim,
        lease_seconds=45,
        now=NOW,
    )

    sql, params = _compiled(session.statements[0])
    for predicate in ("id =", "status =", "worker_id =", "lease_epoch ="):
        assert predicate in sql
    assert "running" in params.values()
    assert "worker-1" in params.values()
    assert 3 in params.values()
    assert NOW in params.values()
    assert NOW + timedelta(seconds=45) in params.values()


@pytest.mark.parametrize(
    ("attempts", "expected_seconds"),
    [
        (1, 10),
        (2, 20),
        (3, 40),
        (4, 50),
        (10**9, 50),
    ],
)
def test_retry_backoff_is_exponential_capped_and_never_terminal(
    attempts: int,
    expected_seconds: int,
) -> None:
    session = _RecordingSession(rowcount=1)

    assert outbox_store.release_outbox_event_for_retry(
        session,
        _claim(attempts=attempts),
        error="bad\x00message\n" + ("x" * 5000),
        base_delay_seconds=10,
        max_delay_seconds=50,
        now=NOW,
    )

    sql, params = _compiled(session.statements[0])
    assert "UPDATE outbox_events SET" in sql
    assert "pending" in params.values()
    assert "failed" not in params.values()
    assert NOW + timedelta(seconds=expected_seconds) in params.values()
    error = next(
        value
        for value in params.values()
        if isinstance(value, str) and value.startswith("badmessage")
    )
    assert "\x00" not in error
    assert len(error) <= 4000


def test_retry_delay_inputs_are_normalized_and_max_is_authoritative() -> None:
    session = _RecordingSession(rowcount=1)

    assert outbox_store.release_outbox_event_for_retry(
        session,
        _claim(attempts=5),
        error="failure",
        base_delay_seconds=-10,
        max_delay_seconds=-1,
        now=NOW,
    )

    _sql, params = _compiled(session.statements[0])
    assert NOW in params.values()


def test_worker_commits_claim_before_handler_and_acks_exactly_once(monkeypatch) -> None:
    factory = _SessionFactory()
    claim = _claim(payload={"nested": MappingProxyType({"value": 1})})
    acknowledgements: list[object] = []

    monkeypatch.setattr(
        outbox_worker.outbox_store,
        "claim_next_outbox_event",
        lambda *_args, **_kwargs: claim,
    )
    monkeypatch.setattr(
        outbox_worker.outbox_store,
        "heartbeat_outbox_event",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        outbox_worker.outbox_store,
        "mark_outbox_event_processed",
        lambda _session, received, **_kwargs: (
            acknowledgements.append(received) or True
        ),
    )

    def handler(received) -> None:
        assert factory.committed == 1
        assert received is claim
        assert not isinstance(received, OutboxEvent)
        with pytest.raises(TypeError):
            received.payload["nested"]["value"] = 2

    result = outbox_worker.run_one_outbox_event(
        session_factory=factory,
        worker_id="worker-1",
        handlers={"test.event": handler},
        lease_seconds=1,
        heartbeat_interval_seconds=0.1,
        base_delay_seconds=10,
        max_delay_seconds=50,
    )

    assert result.processed is True
    assert result.message == "processed"
    assert acknowledgements == [claim]
    assert factory.committed == 2
    assert factory.rolled_back == 0


def test_worker_failure_and_missing_handler_are_always_retryable(monkeypatch) -> None:
    claims = [_claim(attempts=4), _claim(attempts=9)]
    retried: list[tuple[int, str]] = []

    monkeypatch.setattr(
        outbox_worker.outbox_store,
        "claim_next_outbox_event",
        lambda *_args, **_kwargs: claims.pop(0),
    )
    monkeypatch.setattr(
        outbox_worker.outbox_store,
        "heartbeat_outbox_event",
        lambda *_args, **_kwargs: True,
    )

    def retry(_session, claim, *, error, **_kwargs) -> bool:
        retried.append((claim.attempts, str(error)))
        return True

    monkeypatch.setattr(
        outbox_worker.outbox_store,
        "release_outbox_event_for_retry",
        retry,
    )

    failed = outbox_worker.run_one_outbox_event(
        session_factory=_SessionFactory(),
        worker_id="worker-1",
        handlers={"test.event": lambda _claim: (_ for _ in ()).throw(RuntimeError("boom"))},
        lease_seconds=1,
        heartbeat_interval_seconds=0.1,
        base_delay_seconds=10,
        max_delay_seconds=50,
    )
    missing = outbox_worker.run_one_outbox_event(
        session_factory=_SessionFactory(),
        worker_id="worker-1",
        handlers={},
        lease_seconds=1,
        heartbeat_interval_seconds=0.1,
        base_delay_seconds=10,
        max_delay_seconds=50,
    )

    assert failed.message == "handler_failed"
    assert missing.message == "handler_failed"
    assert retried[0] == (4, "boom")
    assert "No outbox handler registered" in retried[1][1]


def test_heartbeat_ownership_loss_prevents_ack_or_retry(monkeypatch) -> None:
    heartbeat_called = threading.Event()
    acknowledgements: list[object] = []
    retries: list[object] = []

    monkeypatch.setattr(
        outbox_worker.outbox_store,
        "claim_next_outbox_event",
        lambda *_args, **_kwargs: _claim(),
    )

    def lose_heartbeat(*_args, **_kwargs) -> bool:
        heartbeat_called.set()
        return False

    monkeypatch.setattr(
        outbox_worker.outbox_store,
        "heartbeat_outbox_event",
        lose_heartbeat,
    )
    monkeypatch.setattr(
        outbox_worker.outbox_store,
        "mark_outbox_event_processed",
        lambda *_args, **_kwargs: acknowledgements.append(True) or True,
    )
    monkeypatch.setattr(
        outbox_worker.outbox_store,
        "release_outbox_event_for_retry",
        lambda *_args, **_kwargs: retries.append(True) or True,
    )

    result = outbox_worker.run_one_outbox_event(
        session_factory=_SessionFactory(),
        worker_id="worker-1",
        handlers={"test.event": lambda _claim: heartbeat_called.wait(timeout=1)},
        lease_seconds=0.2,
        heartbeat_interval_seconds=0.01,
        base_delay_seconds=10,
        max_delay_seconds=50,
    )

    assert heartbeat_called.is_set()
    assert result.message == "lease_lost"
    assert result.processed is False
    assert acknowledgements == []
    assert retries == []


def test_heartbeat_exception_is_ownership_loss(monkeypatch) -> None:
    heartbeat_called = threading.Event()
    acknowledgements: list[object] = []

    monkeypatch.setattr(
        outbox_worker.outbox_store,
        "claim_next_outbox_event",
        lambda *_args, **_kwargs: _claim(),
    )

    def broken_heartbeat(*_args, **_kwargs) -> bool:
        heartbeat_called.set()
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        outbox_worker.outbox_store,
        "heartbeat_outbox_event",
        broken_heartbeat,
    )
    monkeypatch.setattr(
        outbox_worker.outbox_store,
        "mark_outbox_event_processed",
        lambda *_args, **_kwargs: acknowledgements.append(True) or True,
    )

    result = outbox_worker.run_one_outbox_event(
        session_factory=_SessionFactory(),
        worker_id="worker-1",
        handlers={"test.event": lambda _claim: heartbeat_called.wait(timeout=1)},
        lease_seconds=0.2,
        heartbeat_interval_seconds=0.01,
        base_delay_seconds=10,
        max_delay_seconds=50,
    )

    assert result.message == "lease_lost"
    assert acknowledgements == []


def test_live_heartbeat_uses_fresh_transaction_and_allows_ack(monkeypatch) -> None:
    heartbeat_called = threading.Event()
    acknowledgements: list[object] = []
    factory = _SessionFactory()

    monkeypatch.setattr(
        outbox_worker.outbox_store,
        "claim_next_outbox_event",
        lambda *_args, **_kwargs: _claim(),
    )

    def heartbeat(*_args, **_kwargs) -> bool:
        heartbeat_called.set()
        return True

    monkeypatch.setattr(
        outbox_worker.outbox_store,
        "heartbeat_outbox_event",
        heartbeat,
    )
    monkeypatch.setattr(
        outbox_worker.outbox_store,
        "mark_outbox_event_processed",
        lambda _session, claim, **_kwargs: acknowledgements.append(claim) or True,
    )

    result = outbox_worker.run_one_outbox_event(
        session_factory=factory,
        worker_id="worker-1",
        handlers={"test.event": lambda _claim: heartbeat_called.wait(timeout=1)},
        lease_seconds=0.2,
        heartbeat_interval_seconds=0.01,
        base_delay_seconds=10,
        max_delay_seconds=50,
    )

    assert heartbeat_called.is_set()
    assert result.processed is True
    assert acknowledgements == [_claim()]
    assert factory.committed >= 3


@pytest.mark.parametrize("handler_fails", [False, True])
def test_worker_reports_stale_claim_when_t2_fence_is_lost(
    monkeypatch,
    handler_fails: bool,
) -> None:
    monkeypatch.setattr(
        outbox_worker.outbox_store,
        "claim_next_outbox_event",
        lambda *_args, **_kwargs: _claim(),
    )
    monkeypatch.setattr(
        outbox_worker.outbox_store,
        "heartbeat_outbox_event",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        outbox_worker.outbox_store,
        "mark_outbox_event_processed",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        outbox_worker.outbox_store,
        "release_outbox_event_for_retry",
        lambda *_args, **_kwargs: False,
    )

    def handler(_claim) -> None:
        if handler_fails:
            raise RuntimeError("failure")

    result = outbox_worker.run_one_outbox_event(
        session_factory=_SessionFactory(),
        worker_id="worker-1",
        handlers={"test.event": handler},
        lease_seconds=1,
        heartbeat_interval_seconds=0.1,
        base_delay_seconds=10,
        max_delay_seconds=50,
    )

    assert result.processed is False
    assert result.message == "stale_claim"


@pytest.mark.parametrize("payload_json", ["", "{not-json", "[]"])
def test_malformed_payload_commits_t1_then_retries_without_calling_handler(
    payload_json: str,
) -> None:
    row = _row(payload_json=payload_json)
    factory = _SessionFactory(
        [
            _RecordingSession(selected_row=row),
            _RecordingSession(rowcount=1),
        ]
    )
    handled: list[object] = []

    result = outbox_worker.run_one_outbox_event(
        session_factory=factory,
        worker_id="worker-1",
        handlers={"test.event": handled.append},
        lease_seconds=60,
        heartbeat_interval_seconds=15,
        base_delay_seconds=10,
        max_delay_seconds=50,
    )

    assert result.message == "handler_failed"
    assert handled == []
    assert factory.committed == 2
    assert factory.rolled_back == 0
    assert row.status == "running"
    retry_sql, retry_params = _compiled(factory.created[1].statements[0])
    assert "UPDATE outbox_events SET" in retry_sql
    assert "pending" in retry_params.values()


def test_worker_rejects_invalid_heartbeat_interval_before_claim(monkeypatch) -> None:
    claimed: list[object] = []
    monkeypatch.setattr(
        outbox_worker.outbox_store,
        "claim_next_outbox_event",
        lambda *_args, **_kwargs: claimed.append(True),
    )

    with pytest.raises(ValueError, match="heartbeat_interval_seconds"):
        outbox_worker.run_one_outbox_event(
            session_factory=_SessionFactory(),
            worker_id="worker-1",
            handlers={},
            lease_seconds=10,
            heartbeat_interval_seconds=10,
            base_delay_seconds=10,
            max_delay_seconds=50,
        )

    assert claimed == []


def test_default_projection_handler_consumes_detached_claim_payload(
    monkeypatch,
) -> None:
    from forwin.knowledge_system import projection_jobs
    from forwin.outbox.handlers import build_default_outbox_handlers

    captured: dict[str, object] = {}
    factory = _SessionFactory([_RecordingSession()])
    factory.sessions[0].get = lambda _model, _key: SimpleNamespace(id="project-1")

    def refresh(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(projection_jobs, "refresh_projection_now", refresh)
    handlers = build_default_outbox_handlers(
        session_factory=factory,
        memory_index=SimpleNamespace(),
    )
    claim = outbox_worker.OutboxClaim(
        row_id="row-1",
        event_id="event-1",
        event_type=projection_jobs.KNOWLEDGE_PROJECTION_REFRESH_EVENT,
        aggregate_type="project",
        aggregate_id="project-1",
        payload=MappingProxyType(
            {
                "project_id": "project-1",
                "projection_kind": "obsidian",
                "requested_as_of_chapter": 7,
                "trigger": "test",
            }
        ),
        payload_error="",
        worker_id="worker-1",
        lease_epoch=1,
        attempts=1,
    )

    handlers[projection_jobs.KNOWLEDGE_PROJECTION_REFRESH_EVENT](claim)

    assert not hasattr(claim, "payload_json")
    assert captured["project_id"] == "project-1"
    assert captured["projection_kind"] == "obsidian"
    assert captured["trigger"] == "test"
    assert captured["event_id"] == "event-1"
    assert captured["event_identity"] is None
