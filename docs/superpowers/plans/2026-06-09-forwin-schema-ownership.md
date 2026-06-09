# ForWin Schema Ownership Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add schema ownership documentation and narrow enforcement tests without changing physical database layout.

**Architecture:** Keep shared Postgres/Qdrant/MinIO. Encode logical owner domains in operations docs and add source scans for ownership boundaries that are already stable enough to enforce.

**Tech Stack:** Markdown operations docs, pytest source-boundary tests.

---

## File Structure

- Create: `docs/operations/forwin-schema-ownership.md`
- Create: `tests/test_schema_ownership.py`
- Modify: `docs/operations/forwin-service-process-roadmap.md`
- Modify: `docs/superpowers/plans/2026-06-08-forwin-service-process-roadmap.md`

## Task 1: RED Tests

- [x] **Step 1: Add schema ownership doc test**

Create `tests/test_schema_ownership.py` to assert the ownership doc names each
owner domain, says shared infrastructure remains on 150, lists physical split
candidates in order, and requires a separate design before physical DB split.

- [x] **Step 2: Add outbox ownership source scan**

Add a source scan that allows `OutboxEvent` imports only in:

- `forwin/models/outbox.py`
- `forwin/models/__init__.py`
- `forwin/outbox/*.py`
- approved producer/handler adapter `forwin/knowledge_system/projection_jobs.py`

- [x] **Step 3: Run RED**

Run:

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_schema_ownership.py -q
```

Expected: FAIL because `docs/operations/forwin-schema-ownership.md` does not exist yet.

## Task 2: Implement Schema Ownership Docs

- [x] **Step 1: Create operations doc**

Create `docs/operations/forwin-schema-ownership.md` with owner tables,
enforcement notes, physical split candidate order, and explicit non-goals.

- [x] **Step 2: Link roadmap summary**

Update `docs/operations/forwin-service-process-roadmap.md` Phase 5 summary to
point operators at the schema ownership doc.

## Task 3: Verify and Commit

- [x] **Step 1: Run schema ownership tests**

Run:

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_schema_ownership.py -q
```

- [x] **Step 2: Run boundary regression tests**

Run:

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_service_process_boundaries.py -q
```

- [x] **Step 3: Mark Phase 5 complete**

Mark Phase 5 steps 1-4 complete in the master roadmap plan.

- [x] **Step 4: Commit**

Run:

```bash
git add docs/operations/forwin-schema-ownership.md docs/operations/forwin-service-process-roadmap.md tests/test_schema_ownership.py docs/superpowers/specs/2026-06-09-forwin-schema-ownership-design.md docs/superpowers/plans/2026-06-09-forwin-schema-ownership.md docs/superpowers/plans/2026-06-08-forwin-service-process-roadmap.md
git commit -m "docs: define ForWin schema ownership"
```
