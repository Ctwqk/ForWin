from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from forwin.http.tasks import (
    GenerationTaskPersistenceError,
    _run_generation_task_db_write,
)


def _retryable_operational_error() -> OperationalError:
    return OperationalError("UPDATE generation_tasks", {}, RuntimeError("deadlock detected"))


def test_generation_task_db_write_raises_after_retry_failure() -> None:
    def operation() -> None:
        raise _retryable_operational_error()

    with pytest.raises(GenerationTaskPersistenceError):
        _run_generation_task_db_write(
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
        _run_generation_task_db_write(
            operation,
            context="unit-test-prune",
            attempts=2,
            delay=0,
            raise_on_failure=False,
        )
        is False
    )
