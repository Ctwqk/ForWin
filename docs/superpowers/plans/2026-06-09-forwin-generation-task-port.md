# ForWin Generation Task Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the generation worker's dependency on `forwin.api_core.generation` by injecting the continue-task factory through an explicit generation port boundary.

**Architecture:** Keep task creation behavior in the API boundary for this slice, but pass the factory into worker execution from the CLI/worker-loop boundary. `forwin/generation/*` defines protocol types and consumes injected callables; it must not import `forwin.api_core.*`.

**Tech Stack:** Python 3.13, Callable/Protocol typing, pytest source-boundary tests, existing generation worker CLI tests.

---

## File Structure

- Create: `forwin/generation/ports.py`
  - Defines `CreateContinueGenerationTask` as the callable contract used by auto-continue.
- Modify: `forwin/generation/worker.py`
  - Accepts an injected continue-task factory and passes it to the auto-continue completion handler.
- Modify: `forwin/generation/worker_cli.py`
  - Accepts the injected factory and forwards it into `run_one_generation_task`.
- Modify: `forwin/cli.py`
  - Imports `_create_continue_generation_task` at the CLI boundary and passes it into the worker loop.
- Modify: `tests/test_service_process_boundaries.py`
  - Removes the temporary allowlist for `generation.worker -> api_core.generation`.
- Modify: `tests/test_generation_worker_cli.py`
  - Proves `run_generation_worker_loop()` forwards the injected factory to the run-once function.
- Modify: `docs/superpowers/plans/2026-06-08-forwin-service-process-roadmap.md`
  - Marks Phase 2 Step 1 complete after verification.

## Task 1: Add Failing Boundary and Worker-Loop Tests

- [x] **Step 1: Update boundary test to remove the allowlist**

Change `tests/test_service_process_boundaries.py`:

```python
def test_generation_package_does_not_depend_on_api_core() -> None:
    for path in sorted((ROOT / "forwin" / "generation").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        matches = {
            line.strip()
            for line in source.splitlines()
            if "forwin.api_core" in line
        }
        assert matches == set(), f"{path.relative_to(ROOT)} has unexpected api_core dependency: {matches}"
```

- [x] **Step 2: Add worker-loop forwarding test**

Append to `tests/test_generation_worker_cli.py`:

```python
def test_generation_worker_loop_forwards_continue_task_factory() -> None:
    calls = []

    def create_continue_generation_task(**kwargs):
        return f"task-for-{kwargs['project_id']}"

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
        create_continue_generation_task=create_continue_generation_task,
    )

    assert exit_code == 0
    assert calls[0]["create_continue_generation_task"] is create_continue_generation_task
```

- [x] **Step 3: Run tests to verify RED**

Run:

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_service_process_boundaries.py::test_generation_package_does_not_depend_on_api_core tests/test_generation_worker_cli.py::test_generation_worker_loop_forwards_continue_task_factory -q
```

Expected: FAIL because `forwin/generation/worker.py` still imports `forwin.api_core.generation`, and `run_generation_worker_loop()` does not accept `create_continue_generation_task`.

## Task 2: Implement the Port and Injection Path

- [x] **Step 1: Create `forwin/generation/ports.py`**

Use:

```python
from __future__ import annotations

from collections.abc import Callable
from typing import Any


CreateContinueGenerationTask = Callable[..., str]
GenerationTaskRunner = Callable[..., Any]
```

- [x] **Step 2: Modify `forwin/generation/worker.py`**

Import `CreateContinueGenerationTask`, add a `create_continue_generation_task`
parameter to `run_one_generation_task()`, pass it into `_default_continue_executor()`,
`_default_new_executor()`, and `_worker_completion_handler()`, and remove the
inner import from `forwin.api_core.generation`.

The completion handler must skip auto-continue only when payload auto-continue is
false. If payload auto-continue is true and the injected factory is missing, it
must raise `RuntimeError("create_continue_generation_task is required for auto-continue")`.

- [x] **Step 3: Modify `forwin/generation/worker_cli.py`**

Add `create_continue_generation_task: CreateContinueGenerationTask | None = None`
to `run_generation_worker_loop()` and forward it to `run_once(...)`.

- [x] **Step 4: Modify `forwin/cli.py`**

Inside `cmd_generation_worker()`, import `_create_continue_generation_task` from
`forwin.api_core.generation` and pass it to `run_generation_worker_loop()`.

This keeps the API dependency at the CLI boundary, outside `forwin/generation/*`.

## Task 3: Verify and Mark Master Plan Progress

- [x] **Step 1: Run targeted tests**

Run:

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_service_process_boundaries.py tests/test_generation_worker_cli.py -q
```

Expected: PASS.

- [x] **Step 2: Run generation worker regression tests**

Run:

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_generation_task_lease.py tests/test_generation_worker_cutover.py tests/test_generation_auto_continue.py -q
```

Expected: PASS.

- [x] **Step 3: Mark Phase 2 Step 1 complete in the master plan**

Change only Phase 2 Step 1 in `docs/superpowers/plans/2026-06-08-forwin-service-process-roadmap.md` from `[ ]` to `[x]`.

- [x] **Step 4: Commit**

Run:

```bash
git add forwin/generation/ports.py forwin/generation/worker.py forwin/generation/worker_cli.py forwin/cli.py tests/test_service_process_boundaries.py tests/test_generation_worker_cli.py docs/superpowers/plans/2026-06-08-forwin-service-process-roadmap.md docs/superpowers/plans/2026-06-09-forwin-generation-task-port.md
git commit -m "refactor: inject generation continue task factory"
```
