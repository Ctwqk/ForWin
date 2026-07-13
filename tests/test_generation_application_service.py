from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from forwin.application.errors import ActiveGenerationTaskError, ProjectNotFound
from forwin.application.generation import (
    EnqueueGenerationCommand,
    GenerationApplicationService,
)
from forwin.config import InfrastructureConfig
from forwin.generation.task_payload import payload_from_json
from forwin.models.audit import DecisionEvent
from forwin.models.project import Project
from forwin.models.task import GenerationTask
from forwin.runtime.policy import RuntimePolicy
from forwin.runtime.policy_store import ProjectPolicyStore


@pytest.fixture
def session_factory() -> Iterator[sessionmaker]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Project.__table__.create(engine)
    GenerationTask.__table__.create(engine)
    DecisionEvent.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


def _create_project(
    session_factory: sessionmaker,
    *,
    project_id: str = "project-1",
    policy: RuntimePolicy | None = None,
) -> Project:
    with session_factory.begin() as session:
        project = Project(
            id=project_id,
            title="测试书",
            premise="测试前提",
            genre="玄幻",
            creation_status="writing",
        )
        session.add(project)
        session.flush()
        ProjectPolicyStore(session).initialize(
            project,
            policy or RuntimePolicy.for_profile("standard"),
        )
        return project


def _command(project_id: str = "project-1") -> EnqueueGenerationCommand:
    return EnqueueGenerationCommand(
        project_id=project_id,
        requested_chapters=3,
        max_chapters=3,
        run_until_chapter=8,
        auto_continue=True,
        title="继续生成",
        subtitle="3 chapters",
        message="准备继续生成。",
        root_event_type="continue_requested",
    )


def _service(
    session_factory: sessionmaker,
    *,
    runner=None,
) -> GenerationApplicationService:
    return GenerationApplicationService(
        session_factory=session_factory,
        infrastructure=InfrastructureConfig(
            database_url="sqlite+pysqlite:///:memory:",
        ),
        runner=runner,
    )


def test_enqueue_snapshots_project_policy_and_creates_one_task(
    session_factory: sessionmaker,
) -> None:
    policy = RuntimePolicy.for_profile("pulp").with_user_settings(gate_delegate="spark")
    project = _create_project(session_factory, policy=policy)

    handle = _service(session_factory).enqueue(_command(project.id))

    with session_factory() as session:
        task = session.get(GenerationTask, handle.task_id)
        events = session.execute(select(DecisionEvent)).scalars().all()
    assert task is not None
    payload = payload_from_json(task.execution_payload_json)
    assert handle.project_id == project.id
    assert payload.mode == "continue"
    assert payload.policy_version == 1
    assert payload.policy_snapshot == policy
    assert payload.root_event_id == events[0].id
    assert events[0].task_id == task.id
    assert events[0].causal_root_id == events[0].id
    assert task.status == "queued"
    assert task.message == "准备继续生成。"


def test_enqueue_rejects_second_active_task(session_factory: sessionmaker) -> None:
    _create_project(session_factory)
    service = _service(session_factory)
    service.enqueue(_command())

    with pytest.raises(ActiveGenerationTaskError):
        service.enqueue(_command())

    with session_factory() as session:
        assert session.scalar(select(func.count(GenerationTask.id))) == 1


def test_enqueue_rejects_missing_project(session_factory: sessionmaker) -> None:
    with pytest.raises(ProjectNotFound):
        _service(session_factory).enqueue(_command("missing"))


def test_execute_claimed_builds_context_from_immutable_payload(
    session_factory: sessionmaker,
) -> None:
    policy = RuntimePolicy.for_profile("standard")
    project = _create_project(session_factory, policy=policy)
    calls: list[tuple[GenerationTask, object, int, str]] = []
    service = _service(
        session_factory,
        runner=lambda task, context, resume, worker, _lease_epoch: calls.append(
            (task, context, resume, worker)
        ),
    )
    handle = service.enqueue(_command(project.id))
    with session_factory() as session:
        task = session.get(GenerationTask, handle.task_id)
        assert task is not None
        task.status = "running"
        task.lease_owner = "worker-1"
        task.lease_epoch = 1
        session.commit()
        session.expunge(task)

    service.execute_claimed(task, resume_from_chapter=4, worker_id="worker-1")

    called_task, context, resume, worker = calls[0]
    assert called_task.id == task.id
    assert context.policy == policy
    assert context.task_id == task.id
    assert context.root_event_id
    assert resume == 4
    assert worker == "worker-1"


def test_default_runner_executes_project_task_and_persists_updates(
    session_factory: sessionmaker,
    monkeypatch,
) -> None:
    project = _create_project(session_factory)
    service = _service(session_factory)
    command = replace(_command(project.id), auto_continue=False)
    handle = service.enqueue(command)
    with session_factory() as session:
        task = session.get(GenerationTask, handle.task_id)
        assert task is not None
        task.status = "running"
        task.lease_owner = "worker-1"
        task.lease_epoch = 1
        session.commit()
        session.expunge(task)
    calls: list[dict[str, object]] = []

    def fake_run(context, project_id, update_task, _logger, **kwargs):
        calls.append(
            {
                "context": context,
                "project_id": project_id,
                "resume_from_chapter": kwargs["resume_from_chapter"],
                "abort": kwargs["should_abort"](),
                "pause": kwargs["should_pause"](),
            }
        )
        update_task(task.id, status="completed", current_stage="completed")
        kwargs["completion_handler"](
            SimpleNamespace(
                project_id=project_id,
                status="completed",
                completed_chapters=[1],
                failed_chapters=[],
                paused_chapters=[],
            )
        )

    monkeypatch.setattr(
        "forwin.application.generation_execution.execute_continuation",
        fake_run,
    )

    service.execute_claimed(
        task,
        resume_from_chapter=2,
        worker_id="worker-1",
        lease_epoch=1,
    )

    with session_factory() as session:
        saved = session.get(GenerationTask, task.id)
        assert saved is not None
        assert saved.status == "completed"
        assert saved.current_stage == "completed"
    assert calls[0]["project_id"] == project.id
    assert calls[0]["resume_from_chapter"] == 2
    assert calls[0]["abort"] is False
    assert calls[0]["pause"] is False
