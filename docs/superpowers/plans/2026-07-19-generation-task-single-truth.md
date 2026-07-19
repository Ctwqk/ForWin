# Generation Task Single Truth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make PostgreSQL the only generation-task authority and recover expired pause/cancel requests without API process state.

**Architecture:** Remove the HTTP task cache, cache/DB merge logic, automatic terminal-row pruning, and API-startup task rewriting. Persist API control mutations directly to locked task rows; let workers reclaim expired running tasks and acknowledge durable pause/cancel flags before pipeline construction.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy, PostgreSQL, pytest, Ruff.

## Global Constraints

- This is a v5-only destructive update; no old cache or task migration is required.
- PostgreSQL is the only generation-task source of truth.
- Missing tasks must never be synthesized by an update path.
- Terminal task rows are removed only by explicit task deletion.
- Canon recovery remains fenced by `lease_owner` and `lease_epoch`.
- Do not add a cache, sweeper, workflow engine, compatibility layer, or alternate task model.

---

### Task 1: Delete Split Task Truth And Recover Durable Control Requests

**Files:**

- Modify: `forwin/http/runtime.py`
- Modify: `forwin/http/tasks.py`
- Modify: `forwin/http/project_support.py`
- Modify: `forwin/http/generation.py`
- Modify: `forwin/http/app.py`
- Modify: `forwin/application/task_center.py`
- Modify: `forwin/generation/task_lease.py`
- Modify: `forwin/application/generation.py`
- Modify: `forwin/generation/task_repository.py`
- Modify: `tests/http_runtime_harness.py`
- Modify: `tests/test_generation_task_persistence.py`
- Create: `tests/test_task_center_service.py`
- Modify: `tests/test_generation_worker_canon_recovery.py`
- Modify: `tests/test_architecture_boundaries.py`

**Interfaces:**

- Consumes: `GenerationTask`, `GenerationTaskRepository`, `claim_generation_task`, and the existing task API schemas.
- Produces: database-only task get/list/active-check/update behavior and worker acknowledgement of expired pause/cancel requests.

- [ ] **Step 1: Add database-authority regressions**

Add tests proving that an old terminal row remains readable after list polling, a
missing task update does not create a row, and active-check reports only persisted
nonterminal rows. Add an architecture scan for these removed production symbols:

```text
_cached_generation_task
_sync_task_cache
_prefer_cached_generation_task
_prune_generation_tasks_db
task_retention_seconds
tasks_lock
```

- [ ] **Step 2: Verify the database-authority tests fail for the observed reason**

Run:

```bash
python -m pytest \
  tests/test_generation_task_persistence.py \
  tests/test_task_center_service.py \
  tests/test_architecture_boundaries.py -q
```

Expected: at least the retention/cache architecture regressions fail against the
current split-truth implementation.

- [ ] **Step 3: Add expired control-request recovery regressions**

Create two worker tests with an expired `running` task:

```python
task.pause_requested = True
# run_one_generation_task(...) -> task.status == "paused"

task.cancel_requested = True
# run_one_generation_task(...) -> task.status == "cancelled"
```

The injected runner must raise if called, proving no pipeline/LLM work occurs.

- [ ] **Step 4: Verify the recovery tests fail for the observed reason**

Run:

```bash
python -m pytest \
  tests/test_generation_worker_canon_recovery.py -q
```

Expected: the worker reports no claimable task because the current lease query
excludes pause/cancel requests.

- [ ] **Step 5: Remove the HTTP task cache and automatic pruning**

Make `TaskCenterService` database-only. Remove cache fields from `HttpRuntime`,
cache merge/fallback helpers from HTTP task code, cache overlays from active
detection, and age/count-based physical deletion. Preserve explicit soft deletion
and the existing retry behavior for database writes.

- [ ] **Step 6: Make task mutation one locked database transaction**

Load the existing `GenerationTask` with `SELECT ... FOR UPDATE`, apply the current
normalization rules, and commit. Return without writing when the row is absent. Do
not create `GenerationTask(id=task_id)` in update code.

- [ ] **Step 7: Move interrupted control acknowledgement to the worker**

Allow expired running tasks through `claim_generation_task` regardless of request
flags while retaining request filtering for queued tasks. In
`GenerationApplicationService.execute_claimed`, terminalize durable pause/cancel
requests before recovery or pipeline construction. Remove API-startup invocation
and implementation of `recover_interrupted_generation_tasks`.

- [ ] **Step 8: Run focused verification**

Run:

```bash
python -m pytest \
  tests/test_generation_task_persistence.py \
  tests/test_task_center_service.py \
  tests/test_generation_worker_cli.py \
  tests/test_generation_worker_canon_recovery.py \
  tests/test_project_operation_guards.py \
  tests/test_mcp_server.py \
  tests/test_architecture_boundaries.py -q
```

Expected: all pass.

- [ ] **Step 9: Run release-level verification**

Run:

```bash
uv run --no-sync ruff check forwin tests
python -m compileall -q forwin tests
python -m pytest -q
git diff --check
```

Expected: all pass, with only the repository's documented skip.

- [ ] **Step 10: Commit**

```bash
git add forwin tests
git commit -m "refactor: make generation tasks database authoritative"
```
