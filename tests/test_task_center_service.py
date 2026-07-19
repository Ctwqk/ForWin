from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timedelta, timezone

import pytest

from forwin.application.task_center import TaskCenterService
from forwin.config import InfrastructureConfig
from forwin.http.runtime import HttpRuntime
from forwin.http.tasks import _get_task_center_service
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.task import GenerationTask
from tests.postgres import postgres_test_url


@pytest.fixture
def task_center_service():
    database_url = postgres_test_url("task-center-service")
    engine = get_engine(database_url)
    init_db(engine)
    session_factory = get_session_factory(engine)
    runtime = HttpRuntime(
        config=InfrastructureConfig(database_url=database_url),
        engine=engine,
        session_factory=session_factory,
    )
    yield _get_task_center_service(runtime), session_factory
    engine.dispose()


def test_task_center_service_has_no_process_memory_task_dependencies() -> None:
    dependency_names = {field.name for field in fields(TaskCenterService)}

    assert dependency_names.isdisjoint(
        {
            "has_db_session",
            "prune_task_cache",
            "cached_generation_task",
            "iter_cached_generation_tasks",
            "prefer_cached_generation_task",
        }
    )


def test_list_generation_tasks_keeps_old_terminal_history(
    task_center_service,
) -> None:
    service, session_factory = task_center_service
    old = datetime.now(timezone.utc) - timedelta(days=2)
    with session_factory.begin() as session:
        session.add(
            GenerationTask(
                id="task-center-old-terminal",
                task_kind="generation",
                status="completed",
                current_stage="completed",
                title="old durable history",
                created_at=old,
                updated_at=old,
            )
        )

    for _ in range(3):
        assert [
            task_id for task_id, _ in service.list_generation_tasks(limit=10)
        ] == ["task-center-old-terminal"]

    assert service.load_generation_task("task-center-old-terminal") is not None
    with session_factory() as session:
        assert session.get(GenerationTask, "task-center-old-terminal") is not None
