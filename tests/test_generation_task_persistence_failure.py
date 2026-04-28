from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy.exc import OperationalError

import forwin.api as api_module


def _retryable_operational_error() -> OperationalError:
    return OperationalError("UPDATE generation_tasks", {}, RuntimeError("deadlock detected"))


def test_generation_task_db_write_raises_after_retry_failure() -> None:
    def operation() -> None:
        raise _retryable_operational_error()

    with pytest.raises(api_module.GenerationTaskPersistenceError):
        api_module._run_generation_task_db_write(
            operation,
            context="unit-test",
            attempts=2,
            delay=0,
            raise_on_failure=True,
        )


def test_generation_task_db_write_can_return_false_for_noncritical_cleanup() -> None:
    def operation() -> None:
        raise _retryable_operational_error()

    assert (
        api_module._run_generation_task_db_write(
            operation,
            context="unit-test-prune",
            attempts=2,
            delay=0,
            raise_on_failure=False,
        )
        is False
    )


def test_persist_generation_task_marks_nonterminal_task_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    old_session_factory = api_module._SessionFactory
    api_module._SessionFactory = None
    with api_module._tasks_lock:
        old_tasks = dict(api_module._tasks)
        api_module._tasks.clear()
    task = api_module._create_task_record(title="degraded", requested_chapters=1)
    api_module._SessionFactory = object()
    try:
        with patch.object(
            api_module,
            "_run_generation_task_db_write",
            side_effect=api_module.GenerationTaskPersistenceError("write failed"),
        ):
            api_module._persist_generation_task("task-degraded", task)

        with api_module._tasks_lock:
            cached = dict(api_module._tasks["task-degraded"])
        assert cached["persistence_degraded"] is True
        assert "write failed" in cached["persistence_error"]

        serialized = api_module._serialize_task("task-degraded", cached)
        assert serialized.persistence_degraded is True
        assert "write failed" in str(serialized.persistence_error)
    finally:
        api_module._SessionFactory = old_session_factory
        with api_module._tasks_lock:
            api_module._tasks.clear()
            api_module._tasks.update(old_tasks)


def test_persist_generation_task_raises_for_terminal_task(monkeypatch: pytest.MonkeyPatch) -> None:
    old_session_factory = api_module._SessionFactory
    api_module._SessionFactory = None
    task = api_module._create_task_record(title="terminal", requested_chapters=1)
    task["status"] = "failed"
    task["current_stage"] = "failed"
    api_module._SessionFactory = object()
    try:
        with patch.object(
            api_module,
            "_run_generation_task_db_write",
            side_effect=api_module.GenerationTaskPersistenceError("terminal write failed"),
        ):
            with pytest.raises(api_module.GenerationTaskPersistenceError):
                api_module._persist_generation_task("task-terminal", task)
    finally:
        api_module._SessionFactory = old_session_factory
