from __future__ import annotations

import pytest

from forwin.cli import run_publisher_worker_loop


class FakeBackendJobs:
    def __init__(self, batches: list[list[str]]) -> None:
        self.batches = list(batches)
        self.calls: list[int] = []

    def run_pending_once(self, *, limit: int = 1) -> list[str]:
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
    assert sleeps == [1.25]
