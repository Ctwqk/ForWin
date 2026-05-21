from __future__ import annotations

from forwin.config import Config
from forwin.generation.worker import GenerationWorkerResult
from forwin.generation.worker_cli import run_generation_worker_loop


def test_generation_worker_loop_once_exits_when_no_task() -> None:
    calls = []

    def fake_run_once(**kwargs):
        calls.append(kwargs)
        return GenerationWorkerResult(claimed=False, message="no_claimable_generation_task")

    exit_code = run_generation_worker_loop(
        session_factory=lambda: None,
        config=Config(minimax_api_key="sk-test"),
        worker_id="worker-test",
        lease_seconds=300,
        poll_interval=0,
        once=True,
        run_once=fake_run_once,
    )

    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0]["worker_id"] == "worker-test"


def test_generation_worker_loop_polls_until_stop_after_claim() -> None:
    calls = []

    def fake_run_once(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return GenerationWorkerResult(claimed=True, task_id="task-1", executed=True)
        return GenerationWorkerResult(claimed=False, message="no_claimable_generation_task")

    exit_code = run_generation_worker_loop(
        session_factory=lambda: None,
        config=Config(minimax_api_key="sk-test"),
        worker_id="worker-test",
        lease_seconds=300,
        poll_interval=0,
        once=False,
        max_loops=2,
        run_once=fake_run_once,
    )

    assert exit_code == 0
    assert len(calls) == 2
