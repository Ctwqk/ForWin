from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from forwin.config import Config
from forwin.models.base import get_engine, get_session_factory, init_db
from tests.postgres import postgres_test_url


def _session_factory(name: str):
    engine = get_engine(postgres_test_url(name))
    init_db(engine)
    return engine, get_session_factory(engine)


def test_enqueue_outbox_event_serializes_payload() -> None:
    from forwin.models.outbox import OutboxEvent
    from forwin.outbox.store import enqueue_outbox_event

    engine, Session = _session_factory("outbox-enqueue")
    try:
        with Session.begin() as session:
            event = enqueue_outbox_event(
                session,
                aggregate_type="project",
                aggregate_id="project-1",
                event_type="knowledge.rebuild.requested",
                payload={"chapter": 3},
            )

        with Session() as session:
            row = session.get(OutboxEvent, event.id)
            assert row is not None
            assert row.status == "pending"
            assert row.event_id
            assert json.loads(row.payload_json) == {"chapter": 3}
    finally:
        engine.dispose()


def test_claim_outbox_event_skips_unavailable_events() -> None:
    from forwin.outbox.store import claim_next_outbox_event, enqueue_outbox_event

    engine, Session = _session_factory("outbox-claim-availability")
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    try:
        with Session.begin() as session:
            enqueue_outbox_event(
                session,
                aggregate_type="project",
                aggregate_id="project-1",
                event_type="future.event",
                payload={},
                available_at=future,
            )

        with Session.begin() as session:
            assert claim_next_outbox_event(session, worker_id="worker-1") is None
    finally:
        engine.dispose()


def test_outbox_worker_processes_handled_event() -> None:
    from forwin.models.outbox import OutboxEvent
    from forwin.outbox.store import enqueue_outbox_event
    from forwin.outbox.worker import run_one_outbox_event

    engine, Session = _session_factory("outbox-worker-success")
    handled = []
    try:
        with Session.begin() as session:
            enqueue_outbox_event(
                session,
                aggregate_type="project",
                aggregate_id="project-1",
                event_type="test.event",
                payload={"ok": True},
            )

        result = run_one_outbox_event(
            session_factory=Session,
            worker_id="worker-1",
            handlers={"test.event": lambda event: handled.append(event.event_id)},
        )

        assert result.claimed is True
        assert result.processed is True
        assert handled == [result.event_id]
        with Session() as session:
            row = session.get(OutboxEvent, result.row_id)
            assert row is not None
            assert row.status == "processed"
            assert row.processed_at is not None
    finally:
        engine.dispose()


def test_outbox_worker_retries_then_fails_event() -> None:
    from forwin.models.outbox import OutboxEvent
    from forwin.outbox.store import enqueue_outbox_event
    from forwin.outbox.worker import run_one_outbox_event

    engine, Session = _session_factory("outbox-worker-failure")

    def fail(_event):
        raise RuntimeError("handler failed")

    try:
        with Session.begin() as session:
            event = enqueue_outbox_event(
                session,
                aggregate_type="project",
                aggregate_id="project-1",
                event_type="test.fail",
                payload={},
            )

        first = run_one_outbox_event(
            session_factory=Session,
            worker_id="worker-1",
            handlers={"test.fail": fail},
            max_attempts=2,
        )
        assert first.claimed is True
        assert first.processed is False
        with Session() as session:
            row = session.get(OutboxEvent, event.id)
            assert row is not None
            assert row.status == "pending"
            assert row.attempts == 1
            assert "handler failed" in row.error_message

        second = run_one_outbox_event(
            session_factory=Session,
            worker_id="worker-2",
            handlers={"test.fail": fail},
            max_attempts=2,
        )
        assert second.claimed is True
        assert second.processed is False
        with Session() as session:
            row = session.get(OutboxEvent, event.id)
            assert row is not None
            assert row.status == "failed"
            assert row.attempts == 2
    finally:
        engine.dispose()


def test_outbox_worker_cli_help_exposes_once_mode() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "forwin.cli", "outbox-worker", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "--once" in result.stdout
    assert "--worker-id" in result.stdout
