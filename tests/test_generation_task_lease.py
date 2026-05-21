from __future__ import annotations

from datetime import datetime, timedelta, timezone

from forwin.generation.task_lease import (
    claim_generation_task,
    generation_task_resume_from_chapter,
    heartbeat_generation_task,
)
from forwin.generation.worker import run_one_generation_task
from forwin.config import Config
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.task import GenerationTask
from tests.postgres import postgres_test_url


def test_claim_generation_task_sets_lease_fields() -> None:
    engine = get_engine(postgres_test_url("generation-task-lease"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            session.add(
                GenerationTask(
                    id="task-lease",
                    task_kind="generation",
                    status="queued",
                    project_id="project-1",
                )
            )

        with Session.begin() as session:
            task = claim_generation_task(session, worker_id="worker-1", lease_seconds=300)

        assert task is not None
        assert task.id == "task-lease"
        assert task.status == "running"
        assert task.lease_owner == "worker-1"
        assert task.lease_expires_at is not None
        assert task.heartbeat_at is not None
    finally:
        engine.dispose()


def test_expired_running_task_can_be_reclaimed() -> None:
    engine = get_engine(postgres_test_url("generation-task-reclaim"))
    init_db(engine)
    Session = get_session_factory(engine)
    expired = datetime.now(timezone.utc) - timedelta(minutes=10)
    try:
        with Session.begin() as session:
            session.add(
                GenerationTask(
                    id="task-expired",
                    task_kind="generation",
                    status="running",
                    project_id="project-1",
                    lease_owner="old-worker",
                    lease_expires_at=expired,
                    heartbeat_at=expired,
                )
            )

        with Session.begin() as session:
            task = claim_generation_task(session, worker_id="worker-2", lease_seconds=300)

        assert task is not None
        assert task.lease_owner == "worker-2"
    finally:
        engine.dispose()


def test_heartbeat_extends_matching_running_lease() -> None:
    engine = get_engine(postgres_test_url("generation-task-heartbeat"))
    init_db(engine)
    Session = get_session_factory(engine)
    now = datetime.now(timezone.utc)
    try:
        with Session.begin() as session:
            session.add(
                GenerationTask(
                    id="task-heartbeat",
                    task_kind="generation",
                    status="running",
                    project_id="project-1",
                    lease_owner="worker-1",
                    lease_expires_at=now + timedelta(seconds=60),
                    heartbeat_at=now,
                )
            )

        with Session.begin() as session:
            assert heartbeat_generation_task(
                session,
                task_id="task-heartbeat",
                worker_id="worker-1",
                lease_seconds=300,
            )
            task = session.get(GenerationTask, "task-heartbeat")
            assert task is not None
            assert task.lease_expires_at is not None
            assert task.lease_expires_at > now + timedelta(seconds=60)
    finally:
        engine.dispose()


def test_generation_task_resume_from_completed_chapters() -> None:
    task = GenerationTask(
        id="task-resume",
        completed_chapters_json="[1, 2, 3]",
        run_until_chapter=10,
    )

    assert generation_task_resume_from_chapter(task) == 4


def test_generation_task_resume_prefers_explicit_resume_point() -> None:
    task = GenerationTask(
        id="task-resume-explicit",
        completed_chapters_json="[1, 2, 3]",
        resume_from_chapter=7,
        run_until_chapter=10,
    )

    assert generation_task_resume_from_chapter(task) == 7


def test_worker_claims_and_executes_queued_project_task() -> None:
    engine = get_engine(postgres_test_url("generation-task-worker"))
    init_db(engine)
    Session = get_session_factory(engine)
    calls: list[tuple[str, int]] = []
    try:
        with Session.begin() as session:
            session.add(
                GenerationTask(
                    id="task-worker",
                    task_kind="generation",
                    status="queued",
                    project_id="project-1",
                    completed_chapters_json="[1, 2]",
                    max_chapters=3,
                )
            )

        result = run_one_generation_task(
            session_factory=Session,
            worker_id="worker-1",
            config=Config(),
            execute_continue=lambda task, resume: calls.append((task.id, resume)),
        )

        assert result.claimed is True
        assert result.task_id == "task-worker"
        assert result.resume_from_chapter == 3
        assert calls == [("task-worker", 3)]
        with Session.begin() as session:
            task = session.get(GenerationTask, "task-worker")
            assert task is not None
            assert task.lease_owner == "worker-1"
            assert task.heartbeat_at is not None
    finally:
        engine.dispose()
