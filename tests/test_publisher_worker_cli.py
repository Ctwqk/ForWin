from __future__ import annotations

from contextlib import contextmanager

import pytest

from forwin.cli import run_publisher_worker_loop


class FakeBackendJobs:
    def __init__(
        self,
        batches: list[list[str]],
        *,
        recovered: list[str] | None = None,
    ) -> None:
        self.batches = list(batches)
        self.recovered = list(recovered or [])
        self.recovery_calls = 0
        self.lock_entries = 0
        self.calls: list[int] = []
        self.operations: list[str] = []

    @contextmanager
    def singleton_worker_lock(self):
        self.lock_entries += 1
        self.operations.append("lock-enter")
        try:
            yield
        finally:
            self.operations.append("lock-exit")

    def recover_interrupted_cover_jobs(self) -> list[str]:
        self.recovery_calls += 1
        self.operations.append("recover")
        return list(self.recovered)

    def run_pending_once(self, *, limit: int = 1) -> list[str]:
        self.operations.append("poll")
        self.calls.append(limit)
        if not self.batches:
            return []
        return self.batches.pop(0)


def test_publisher_worker_once_runs_single_batch(capsys) -> None:
    backend_jobs = FakeBackendJobs([[], ["late-job"]])

    run_publisher_worker_loop(
        backend_jobs,
        limit=2,
        once=True,
        poll_interval=0.5,
        sleep=lambda _seconds: None,
    )

    assert backend_jobs.calls == [2]
    assert backend_jobs.recovery_calls == 1
    assert backend_jobs.lock_entries == 1
    assert backend_jobs.operations == [
        "lock-enter",
        "recover",
        "poll",
        "lock-exit",
    ]
    assert capsys.readouterr().out == "no publisher backend jobs\n"


def test_publisher_worker_default_polls_after_idle() -> None:
    backend_jobs = FakeBackendJobs([[], ["job-1"]])
    sleeps: list[float] = []

    def stop_after_first_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise RuntimeError("stop test loop")

    with pytest.raises(RuntimeError, match="stop test loop"):
        run_publisher_worker_loop(
            backend_jobs,
            limit=1,
            once=False,
            poll_interval=1.25,
            sleep=stop_after_first_sleep,
        )

    assert backend_jobs.calls == [1]
    assert backend_jobs.recovery_calls == 1
    assert backend_jobs.lock_entries == 1
    assert backend_jobs.operations == [
        "lock-enter",
        "recover",
        "poll",
        "lock-exit",
    ]
    assert sleeps == [1.25]


def test_publisher_worker_recovers_only_once_before_multiple_polls() -> None:
    backend_jobs = FakeBackendJobs(
        [["recovered-job"], []],
        recovered=["recovered-job"],
    )
    sleeps: list[float] = []

    def stop_after_idle(seconds: float) -> None:
        sleeps.append(seconds)
        raise RuntimeError("stop after second poll")

    with pytest.raises(RuntimeError, match="stop after second poll"):
        run_publisher_worker_loop(
            backend_jobs,
            limit=1,
            once=False,
            poll_interval=0.25,
            sleep=stop_after_idle,
        )

    assert backend_jobs.recovery_calls == 1
    assert backend_jobs.lock_entries == 1
    assert backend_jobs.calls == [1, 1]
    assert backend_jobs.operations == [
        "lock-enter",
        "recover",
        "poll",
        "poll",
        "lock-exit",
    ]
    assert sleeps == [0.25]
