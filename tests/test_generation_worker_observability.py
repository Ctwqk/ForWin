from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from forwin.config import Config
from forwin.generation.task_lease import GenerationTaskClaimResult
from forwin.generation.worker_observability import (
    generation_worker_span,
    record_worker_claim,
)
from forwin.generation.worker import run_one_generation_task
from forwin.governance import DecisionEventType, ensure_decision_event_type
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.governance import DecisionEvent
from forwin.models.observability import PerformanceSpan
from forwin.models.project import Project
from forwin.models.task import GenerationTask
from tests.postgres import postgres_test_url


def test_generation_worker_decision_event_types_are_registered() -> None:
    assert ensure_decision_event_type("generation_worker_claimed") == (
        DecisionEventType.GENERATION_WORKER_CLAIMED
    )
    assert ensure_decision_event_type("generation_worker_reclaimed") == (
        DecisionEventType.GENERATION_WORKER_RECLAIMED
    )
    assert ensure_decision_event_type("generation_worker_heartbeat_failed") == (
        DecisionEventType.GENERATION_WORKER_HEARTBEAT_FAILED
    )
    assert ensure_decision_event_type("generation_worker_execution_failed") == (
        DecisionEventType.GENERATION_WORKER_EXECUTION_FAILED
    )


def _seed_project_task(Session, *, task_id: str, project_id: str = "project-worker-obs") -> None:
    with Session.begin() as session:
        session.add(
            Project(
                id=project_id,
                title="Worker Observability",
                premise="premise",
                genre="玄幻",
            )
        )
        session.add(
            GenerationTask(
                id=task_id,
                task_kind="generation",
                status="running",
                project_id=project_id,
                lease_owner="worker-1",
            )
        )


def _task_events(Session, task_id: str) -> list[DecisionEvent]:
    with Session() as session:
        return list(
            session.execute(
                select(DecisionEvent)
                .where(DecisionEvent.task_id == task_id)
                .order_by(DecisionEvent.created_at.asc(), DecisionEvent.id.asc())
            ).scalars()
        )


def test_record_worker_claim_writes_project_scoped_decision_event() -> None:
    database_url = postgres_test_url("generation-worker-claim-event")
    engine = get_engine(database_url)
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        _seed_project_task(Session, task_id="task-worker-claim-event")
        with Session() as session:
            task = session.get(GenerationTask, "task-worker-claim-event")
            assert task is not None
            claim = GenerationTaskClaimResult(task=task, claim_kind="queued")
            record_worker_claim(
                session_factory=Session,
                config=Config(database_url=database_url, minimax_api_key=""),
                worker_id="worker-1",
                claim=claim,
                resume_from_chapter=4,
                lease_seconds=300,
            )

        events = _task_events(Session, "task-worker-claim-event")
        assert [event.event_type for event in events] == ["generation_worker_claimed"]
        assert events[0].scope == "task"
        assert events[0].actor_type == "worker"
        assert events[0].actor_id == "worker-1"
        payload = json.loads(events[0].payload_json)
        assert payload["worker_id"] == "worker-1"
        assert payload["claim_kind"] == "queued"
        assert payload["resume_from_chapter"] == 4
        assert payload["lease_seconds"] == 300
        assert "previous_lease_owner" not in payload
    finally:
        engine.dispose()


def test_record_worker_reclaim_includes_previous_lease_metadata() -> None:
    database_url = postgres_test_url("generation-worker-reclaim-event")
    engine = get_engine(database_url)
    init_db(engine)
    Session = get_session_factory(engine)
    expired = (datetime.now(timezone.utc) - timedelta(minutes=5)).replace(tzinfo=None)
    try:
        _seed_project_task(Session, task_id="task-worker-reclaim-event")
        with Session() as session:
            task = session.get(GenerationTask, "task-worker-reclaim-event")
            assert task is not None
            task.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
            claim = GenerationTaskClaimResult(
                task=task,
                claim_kind="expired_running",
                previous_lease_owner="old-worker",
                previous_lease_expires_at=expired,
            )
            record_worker_claim(
                session_factory=Session,
                config=Config(database_url=database_url, minimax_api_key=""),
                worker_id="worker-2",
                claim=claim,
                resume_from_chapter=9,
                lease_seconds=300,
            )

        event = _task_events(Session, "task-worker-reclaim-event")[0]
        assert event.event_type == "generation_worker_reclaimed"
        payload = json.loads(event.payload_json)
        assert payload["claim_kind"] == "expired_running"
        assert payload["previous_lease_owner"] == "old-worker"
        assert payload["previous_lease_expires_at"] == expired.isoformat()
    finally:
        engine.dispose()


def test_generation_worker_span_records_performance_span() -> None:
    database_url = postgres_test_url("generation-worker-span")
    engine = get_engine(database_url)
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        _seed_project_task(Session, task_id="task-worker-span")
        with generation_worker_span(
            session_factory=Session,
            config=Config(database_url=database_url, minimax_api_key=""),
            span_name="generation_worker.claim",
            task_id="task-worker-span",
            project_id="project-worker-obs",
            worker_id="worker-1",
            tags={"claim_kind": "queued"},
            metrics={"claimed": 1, "lease_seconds": 300},
        ) as span:
            span.metric("resume_from_chapter", 3)

        with Session() as session:
            row = session.execute(
                select(PerformanceSpan).where(PerformanceSpan.task_id == "task-worker-span")
            ).scalar_one()
        assert row.span_name == "generation_worker.claim"
        assert row.span_kind == "worker"
        assert row.component == "worker"
        assert row.stage == "generation_worker.claim"
        assert json.loads(row.tags_json)["worker_id"] == "worker-1"
        assert json.loads(row.metrics_json)["resume_from_chapter"] == 3
    finally:
        engine.dispose()


def test_worker_observability_event_failure_is_non_fatal() -> None:
    class BrokenObservability:
        def event(self, *args, **kwargs):
            raise RuntimeError("event sink unavailable")

    database_url = postgres_test_url("generation-worker-event-nonfatal")
    engine = get_engine(database_url)
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        _seed_project_task(Session, task_id="task-worker-event-nonfatal")
        with Session() as session:
            task = session.get(GenerationTask, "task-worker-event-nonfatal")
            assert task is not None
            claim = GenerationTaskClaimResult(task=task, claim_kind="queued")

        record_worker_claim(
            session_factory=Session,
            config=Config(database_url=database_url, minimax_api_key=""),
            worker_id="worker-1",
            claim=claim,
            resume_from_chapter=1,
            lease_seconds=300,
            observability_factory=lambda **_kwargs: BrokenObservability(),
        )

        assert _task_events(Session, "task-worker-event-nonfatal") == []
    finally:
        engine.dispose()


def test_run_one_generation_task_records_claim_event_and_execute_span() -> None:
    database_url = postgres_test_url("generation-worker-integrated-claim")
    engine = get_engine(database_url)
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        _seed_project_task(Session, task_id="task-worker-integrated-claim")
        with Session.begin() as session:
            task = session.get(GenerationTask, "task-worker-integrated-claim")
            assert task is not None
            task.status = "queued"
            task.completed_chapters_json = "[1, 2]"
            session.add(task)

        result = run_one_generation_task(
            session_factory=Session,
            worker_id="worker-integrated",
            config=Config(database_url=database_url, minimax_api_key=""),
            execute_continue=lambda _task, _resume: None,
        )

        assert result.claimed is True
        events = _task_events(Session, "task-worker-integrated-claim")
        assert "generation_worker_claimed" in [event.event_type for event in events]
        with Session() as session:
            spans = session.execute(
                select(PerformanceSpan)
                .where(PerformanceSpan.task_id == "task-worker-integrated-claim")
                .order_by(PerformanceSpan.created_at.asc(), PerformanceSpan.id.asc())
            ).scalars().all()
        assert "generation_worker.claim" in [span.span_name for span in spans]
        assert "generation_worker.execute" in [span.span_name for span in spans]
        assert all(span.component == "worker" for span in spans)
    finally:
        engine.dispose()


def test_run_one_generation_task_records_reclaim_event() -> None:
    database_url = postgres_test_url("generation-worker-integrated-reclaim")
    engine = get_engine(database_url)
    init_db(engine)
    Session = get_session_factory(engine)
    expired = (datetime.now(timezone.utc) - timedelta(minutes=5)).replace(tzinfo=None)
    try:
        _seed_project_task(Session, task_id="task-worker-integrated-reclaim")
        with Session.begin() as session:
            task = session.get(GenerationTask, "task-worker-integrated-reclaim")
            assert task is not None
            task.status = "running"
            task.lease_owner = "old-worker"
            task.lease_expires_at = expired
            task.heartbeat_at = expired
            session.add(task)

        run_one_generation_task(
            session_factory=Session,
            worker_id="worker-reclaim",
            config=Config(database_url=database_url, minimax_api_key=""),
            execute_continue=lambda _task, _resume: None,
        )

        event = _task_events(Session, "task-worker-integrated-reclaim")[0]
        assert event.event_type == "generation_worker_reclaimed"
        payload = json.loads(event.payload_json)
        assert payload["previous_lease_owner"] == "old-worker"
        assert payload["previous_lease_expires_at"] == expired.isoformat()
    finally:
        engine.dispose()


def test_run_one_generation_task_records_heartbeat_failure_event() -> None:
    database_url = postgres_test_url("generation-worker-heartbeat-failed")
    engine = get_engine(database_url)
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        _seed_project_task(Session, task_id="task-worker-heartbeat-failed")
        with Session.begin() as session:
            task = session.get(GenerationTask, "task-worker-heartbeat-failed")
            assert task is not None
            task.status = "queued"
            session.add(task)

        def steal_lease(task: GenerationTask, _resume: int) -> None:
            with Session.begin() as session:
                row = session.get(GenerationTask, task.id)
                assert row is not None
                row.lease_owner = "other-worker"
                session.add(row)

        run_one_generation_task(
            session_factory=Session,
            worker_id="worker-heartbeat",
            config=Config(database_url=database_url, minimax_api_key=""),
            execute_continue=steal_lease,
        )

        assert "generation_worker_heartbeat_failed" in [
            event.event_type for event in _task_events(Session, "task-worker-heartbeat-failed")
        ]
    finally:
        engine.dispose()


def test_run_one_generation_task_records_execution_failed_event() -> None:
    database_url = postgres_test_url("generation-worker-execution-failed")
    engine = get_engine(database_url)
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        _seed_project_task(Session, task_id="task-worker-execution-failed")
        with Session.begin() as session:
            task = session.get(GenerationTask, "task-worker-execution-failed")
            assert task is not None
            task.status = "queued"
            session.add(task)

        def fail_execution(_task: GenerationTask, _resume: int) -> None:
            raise RuntimeError("worker failed")

        try:
            run_one_generation_task(
                session_factory=Session,
                worker_id="worker-failed",
                config=Config(database_url=database_url, minimax_api_key=""),
                execute_continue=fail_execution,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("run_one_generation_task should propagate executor failure")

        event_types = [event.event_type for event in _task_events(Session, "task-worker-execution-failed")]
        assert "generation_worker_execution_failed" in event_types
        with Session() as session:
            row = session.get(GenerationTask, "task-worker-execution-failed")
            assert row is not None
            assert row.status == "failed"
            assert row.error_message == "generation_worker_execution_failed"
    finally:
        engine.dispose()
