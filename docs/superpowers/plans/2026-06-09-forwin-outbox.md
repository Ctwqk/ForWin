# ForWin Outbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a minimal Postgres-backed outbox table, store, worker, and CLI command.

**Architecture:** Use SQLAlchemy ORM and Postgres row locks. The outbox worker receives a mapping of event handlers. No existing side effects move in this first slice.

**Tech Stack:** Python 3.13, SQLAlchemy, Alembic, pytest.

---

## File Structure

- Create: `forwin/models/outbox.py`
- Modify: `forwin/models/__init__.py`
- Modify: `forwin/models/base.py`
- Create: `forwin/migrations/versions/0021_outbox_events.py`
- Create: `forwin/outbox/store.py`
- Create: `forwin/outbox/worker.py`
- Modify: `forwin/cli.py`
- Create: `tests/test_outbox_worker.py`
- Modify: `docs/superpowers/plans/2026-06-08-forwin-service-process-roadmap.md`

## Task 1: RED Tests

- [x] **Step 1: Add `tests/test_outbox_worker.py`**

Cover enqueue, claim availability, worker success, worker retry/failure, and CLI help.

- [x] **Step 2: Run RED**

Run:

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_outbox_worker.py -q
```

Expected: FAIL because outbox modules do not exist.

## Task 2: Implement Outbox

- [x] **Step 1: Add model and migration**

Create `OutboxEvent` and `0021_outbox_events.py`.

- [x] **Step 2: Add bootstrap upgrade**

Import `OutboxEvent` in `forwin/models/__init__.py` and add `_upgrade_outbox_events()`.

- [x] **Step 3: Add store helpers**

Implement enqueue, claim, complete, and fail/retry helpers.

- [x] **Step 4: Add worker**

Implement `run_one_outbox_event()` and `run_outbox_worker_loop()`.

- [x] **Step 5: Add CLI**

Add `forwin outbox-worker --once --worker-id ...`.

## Task 3: Verify and Commit

- [x] **Step 1: Run focused tests**

Run:

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_outbox_worker.py -q
```

- [x] **Step 2: Run roadmap regression tests**

Run:

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_generation_task_lease.py tests/test_generation_auto_continue.py -q
```

- [x] **Step 3: Mark Phase 3 plan progress**

Mark Phase 3 steps 1 and 2 complete in the master plan.

- [x] **Step 4: Commit**

Run:

```bash
git add forwin/models/outbox.py forwin/models/__init__.py forwin/models/base.py forwin/migrations/versions/0021_outbox_events.py forwin/outbox/store.py forwin/outbox/worker.py forwin/cli.py tests/test_outbox_worker.py docs/superpowers/specs/2026-06-09-forwin-outbox-design.md docs/superpowers/plans/2026-06-09-forwin-outbox.md docs/superpowers/plans/2026-06-08-forwin-service-process-roadmap.md
git commit -m "feat: add ForWin outbox worker"
```
