# ForWin Projection Outbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move a safe, non-canon projection refresh request onto the new Postgres outbox without changing the default synchronous API behavior.

**Architecture:** Keep `refresh_projection()` synchronous unless the caller explicitly passes `defer=True`. Deferred requests enqueue `knowledge.projection.refresh_requested`; the outbox worker registers a handler that runs the existing projection refresh code in its own transaction.

**Tech Stack:** Python 3.13, SQLAlchemy, FastAPI handler functions, pytest, Postgres outbox.

---

## File Structure

- Modify: `forwin/api_projection_routes.py`
- Create: `forwin/knowledge_system/projection_jobs.py`
- Create: `forwin/outbox/handlers.py`
- Modify: `forwin/cli.py`
- Create: `tests/test_projection_outbox.py`
- Modify: `docs/superpowers/plans/2026-06-08-forwin-service-process-roadmap.md`

## Task 1: RED Tests

- [x] **Step 1: Add deferred projection test**

Create `tests/test_projection_outbox.py` covering:

- `refresh_projection(..., defer=True)` returns an outbox event ack.
- The outbox row payload includes `project_id`, `projection_kind`, and `as_of_chapter`.
- The projection page is not written before the outbox worker runs.
- `run_one_outbox_event()` with default handlers processes the event and writes projection pages.

- [x] **Step 2: Add CLI handler registration test**

In the same test file, assert `forwin/cli.py` uses `build_default_outbox_handlers` so production `outbox-worker` can process moved side effects.

- [x] **Step 3: Run RED**

Run:

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_projection_outbox.py -q
```

Expected: FAIL because `defer` and default outbox handlers do not exist yet.

## Task 2: Implement Deferred Projection Outbox

- [x] **Step 1: Add projection job helpers**

Create `forwin/knowledge_system/projection_jobs.py` with:

- `KNOWLEDGE_PROJECTION_REFRESH_EVENT = "knowledge.projection.refresh_requested"`
- `refresh_projection_now(...)` for the current synchronous projection behavior.
- `enqueue_projection_refresh(...)` for outbox event creation.
- `build_projection_outbox_handlers(...)` for worker handler registration.

- [x] **Step 2: Update API projection handler**

Modify `refresh_projection()` to accept `defer: bool = False`. When `defer` is true, validate the project, enqueue the outbox event, commit, and return an ack. When false, call `refresh_projection_now(...)` and preserve the current response shape.

- [x] **Step 3: Register default outbox handlers**

Create `forwin/outbox/handlers.py` and modify `cmd_outbox_worker()` to pass `build_default_outbox_handlers(session_factory=Session, config=config)` into `run_outbox_worker_loop()`.

## Task 3: Verify and Commit

- [x] **Step 1: Run focused projection outbox tests**

Run:

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_projection_outbox.py -q
```

- [x] **Step 2: Run existing knowledge projection regression**

Run:

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_knowledge_system_v46.py::test_projection_api_refresh_status_and_pages -q
```

- [x] **Step 3: Run outbox worker regression**

Run:

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_outbox_worker.py -q
```

- [x] **Step 4: Mark Phase 3 side-effect progress**

Mark Phase 3 Step 3 complete in the master roadmap plan. Leave Phase 3 Step 4 unchecked until the full Phase 3 verification set is rerun.

- [x] **Step 5: Commit**

Run:

```bash
git add forwin/api_projection_routes.py forwin/knowledge_system/projection_jobs.py forwin/outbox/handlers.py forwin/cli.py tests/test_projection_outbox.py docs/superpowers/plans/2026-06-09-forwin-projection-outbox.md docs/superpowers/plans/2026-06-08-forwin-service-process-roadmap.md
git commit -m "feat: defer projection refresh via outbox"
```
