# ForWin Domain Ports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add lightweight in-process port contracts for canon, review, publisher jobs, and knowledge indexing without changing runtime behavior.

**Architecture:** Define narrow Protocol/dataclass contracts in each domain package. Add thin adapters that wrap current in-process services or callables. These ports are contract boundaries, not remote services.

**Tech Stack:** Python dataclasses, Protocol typing, pytest.

---

## File Structure

- Create: `forwin/book_state/ports.py`
  - Defines `CanonPort` and `BookStateCanonPort`.
- Modify: `forwin/reviewer/ports.py`
  - Keeps `ReviewerPort` and adds chapter-level `ReviewPort`.
- Create: `forwin/publisher_runtime/ports.py`
  - Defines `PublisherJobBatchRequest`, `PublisherJobClient`, and `PublisherRuntimeJobClient`.
- Create: `forwin/retrieval/ports.py`
  - Defines `KnowledgeRebuildRequest`, `KnowledgeSearchRequest`, and `KnowledgeIndexPort`.
- Create: `tests/test_domain_ports.py`
  - Verifies adapters forward to wrapped services/callables.
- Modify: `docs/superpowers/plans/2026-06-08-forwin-service-process-roadmap.md`
  - Marks Phase 2 steps 2-5 complete after tests pass.

## Task 1: RED Tests

- [x] **Step 1: Add `tests/test_domain_ports.py`**

Write tests that import each port module and prove:

- `BookStateCanonPort.compile()` forwards to `compile_approved()`.
- `CallableReviewPort.review_chapter()` forwards a `ReviewChapterRequest`.
- `PublisherRuntimeJobClient.create_upload_jobs_batch()` forwards request fields.
- `CallableKnowledgeIndexPort.rebuild()` and `.search()` forward request objects.

- [x] **Step 2: Run RED**

Run:

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_domain_ports.py -q
```

Expected: FAIL because the new modules/classes do not exist yet.

## Task 2: Implement Ports

- [x] **Step 1: Add `forwin/book_state/ports.py`**

Define `CanonPort` and `BookStateCanonPort`.

- [x] **Step 2: Extend `forwin/reviewer/ports.py`**

Add `ReviewChapterRequest`, `ReviewChapterResult`, `ReviewPort`, and
`CallableReviewPort`.

- [x] **Step 3: Add `forwin/publisher_runtime/ports.py`**

Define `PublisherJobBatchRequest`, `PublisherJobClient`, and
`PublisherRuntimeJobClient`.

- [x] **Step 4: Add `forwin/retrieval/ports.py`**

Define `KnowledgeRebuildRequest`, `KnowledgeSearchRequest`,
`KnowledgeIndexPort`, and `CallableKnowledgeIndexPort`.

## Task 3: Verify and Commit

- [x] **Step 1: Run focused tests**

Run:

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_domain_ports.py tests/test_service_process_boundaries.py -q
```

Expected: PASS.

- [x] **Step 2: Mark master plan progress**

Mark Phase 2 steps 2, 3, 4, and 5 complete in
`docs/superpowers/plans/2026-06-08-forwin-service-process-roadmap.md`.

- [x] **Step 3: Commit**

Run:

```bash
git add forwin/book_state/ports.py forwin/reviewer/ports.py forwin/publisher_runtime/ports.py forwin/retrieval/ports.py tests/test_domain_ports.py docs/superpowers/plans/2026-06-08-forwin-service-process-roadmap.md docs/superpowers/plans/2026-06-09-forwin-domain-ports.md
git commit -m "refactor: add ForWin domain port contracts"
```
