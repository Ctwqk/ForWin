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
            claim = claim_generation_task(session, worker_id="worker-1", lease_seconds=300)
            task = claim.task if claim is not None else None

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
    expired = (datetime.now(timezone.utc) - timedelta(minutes=10)).replace(tzinfo=None)
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
            claim = claim_generation_task(session, worker_id="worker-2", lease_seconds=300)
            task = claim.task if claim is not None else None

        assert task is not None
        assert task.lease_owner == "worker-2"
    finally:
        engine.dispose()


def test_queued_claim_reports_claim_kind_and_no_previous_owner() -> None:
    engine = get_engine(postgres_test_url("generation-task-queued-claim-kind"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            session.add(
                GenerationTask(
                    id="task-queued-kind",
                    task_kind="generation",
                    status="queued",
                    project_id="project-1",
                )
            )

        with Session.begin() as session:
            claim = claim_generation_task(session, worker_id="worker-1", lease_seconds=300)

        assert claim is not None
        assert claim.task.id == "task-queued-kind"
        assert claim.claim_kind == "queued"
        assert claim.previous_lease_owner == ""
        assert claim.previous_lease_expires_at is None
    finally:
        engine.dispose()


def test_expired_claim_reports_previous_lease_metadata() -> None:
    engine = get_engine(postgres_test_url("generation-task-expired-claim-kind"))
    init_db(engine)
    Session = get_session_factory(engine)
    expired = (datetime.now(timezone.utc) - timedelta(minutes=10)).replace(tzinfo=None)
    try:
        with Session.begin() as session:
            session.add(
                GenerationTask(
                    id="task-expired-kind",
                    task_kind="generation",
                    status="running",
                    project_id="project-1",
                    lease_owner="old-worker",
                    lease_expires_at=expired,
                    heartbeat_at=expired,
                )
            )

        with Session.begin() as session:
            claim = claim_generation_task(session, worker_id="worker-2", lease_seconds=300)

        assert claim is not None
        assert claim.task.id == "task-expired-kind"
        assert claim.task.lease_owner == "worker-2"
        assert claim.claim_kind == "expired_running"
        assert claim.previous_lease_owner == "old-worker"
        assert claim.previous_lease_expires_at == expired
    finally:
        engine.dispose()


def test_claim_generation_task_does_not_claim_non_expired_running_task() -> None:
    engine = get_engine(postgres_test_url("generation-task-non-expired"))
    init_db(engine)
    Session = get_session_factory(engine)
    now = datetime.now(timezone.utc)
    try:
        with Session.begin() as session:
            session.add(
                GenerationTask(
                    id="task-running-owned",
                    task_kind="generation",
                    status="running",
                    project_id="project-1",
                    lease_owner="worker-1",
                    lease_expires_at=now + timedelta(minutes=5),
                    heartbeat_at=now,
                )
            )

        with Session.begin() as session:
            task = claim_generation_task(session, worker_id="worker-2", lease_seconds=300)

        assert task is None
    finally:
        engine.dispose()


def test_claim_generation_task_skips_paused_or_cancel_requested_queued_tasks() -> None:
    engine = get_engine(postgres_test_url("generation-task-claim-flags"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            session.add_all(
                [
                    GenerationTask(
                        id="task-paused-before-claim",
                        task_kind="generation",
                        status="queued",
                        project_id="project-1",
                        pause_requested=True,
                    ),
                    GenerationTask(
                        id="task-cancel-before-claim",
                        task_kind="generation",
                        status="queued",
                        project_id="project-2",
                        cancel_requested=True,
                    ),
                ]
            )

        with Session.begin() as session:
            task = claim_generation_task(session, worker_id="worker-1", lease_seconds=300)

        assert task is None
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


def test_default_continue_executor_passes_resume_to_runtime(monkeypatch) -> None:
    engine = get_engine(postgres_test_url("generation-worker-resume-runtime"))
    init_db(engine)
    Session = get_session_factory(engine)
    calls: list[dict[str, object]] = []
    try:
        with Session.begin() as session:
            session.add(
                GenerationTask(
                    id="task-worker-resume-runtime",
                    task_kind="generation",
                    status="queued",
                    project_id="project-1",
                    completed_chapters_json="[1, 2]",
                    max_chapters=3,
                    execution_payload_json='{"mode":"continue","runtime_overrides":{}}',
                )
            )

        def fake_run_continue_project_with_config(*args, **kwargs):
            calls.append(kwargs)

        monkeypatch.setattr(
            "forwin.api_runtime.run_continue_project_with_config",
            fake_run_continue_project_with_config,
        )

        result = run_one_generation_task(
            session_factory=Session,
            worker_id="worker-resume",
            config=Config(minimax_api_key="sk-test"),
        )

        assert result.resume_from_chapter == 3
        assert calls[0]["resume_from_chapter"] == 3
        assert calls[0]["component"] == "worker"
    finally:
        engine.dispose()


def test_worker_uses_initial_payload_for_new_generation(monkeypatch) -> None:
    engine = get_engine(postgres_test_url("generation-worker-initial-payload"))
    init_db(engine)
    Session = get_session_factory(engine)
    calls: list[dict[str, object]] = []
    try:
        with Session.begin() as session:
            session.add(
                GenerationTask(
                    id="task-initial-payload",
                    task_kind="generation",
                    status="queued",
                    project_id="",
                    requested_chapters=2,
                    execution_payload_json=(
                        '{"mode":"initial","premise":"县城开局","genre":"都市",'
                        '"num_chapters":2,"runtime_overrides":{"quality_profile":"pulp"}}'
                    ),
                )
            )

        def fake_run_generation_with_config(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})

        monkeypatch.setattr(
            "forwin.api_runtime.run_generation_with_config",
            fake_run_generation_with_config,
        )

        result = run_one_generation_task(
            session_factory=Session,
            worker_id="worker-initial",
            config=Config(minimax_api_key="sk-test"),
        )

        assert result.claimed is True
        assert calls
        assert calls[0]["args"][1] == "县城开局"
        assert calls[0]["args"][2] == "都市"
        assert calls[0]["args"][3] == 2
        assert calls[0]["kwargs"]["component"] == "worker"
    finally:
        engine.dispose()


def test_worker_continue_executor_passes_completion_handler(monkeypatch) -> None:
    engine = get_engine(postgres_test_url("generation-worker-completion-handler"))
    init_db(engine)
    Session = get_session_factory(engine)
    seen_completion_handlers: list[object] = []
    try:
        with Session.begin() as session:
            session.add(
                GenerationTask(
                    id="task-worker-completion",
                    task_kind="generation",
                    status="queued",
                    project_id="project-1",
                    max_chapters=2,
                    run_until_chapter=10,
                    execution_payload_json=(
                        '{"mode":"continue","auto_continue":true,'
                        '"run_until_chapter":10,"max_chapters":2,'
                        '"runtime_overrides":{}}'
                    ),
                )
            )

        def fake_run_continue_project_with_config(*args, **kwargs):
            seen_completion_handlers.append(kwargs.get("completion_handler"))

        monkeypatch.setattr(
            "forwin.api_runtime.run_continue_project_with_config",
            fake_run_continue_project_with_config,
        )

        run_one_generation_task(
            session_factory=Session,
            worker_id="worker-completion",
            config=Config(minimax_api_key="sk-test"),
        )

        assert callable(seen_completion_handlers[0])
        assert seen_completion_handlers[0] is not None
    finally:
        engine.dispose()
