# Historical Canon Rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace a retried accepted chapter’s obsolete BookState contribution so the corrected draft can enter canon without leaving stale events.

**Architecture:** The retry path records the prior Canon commit as pending replacement. The Canon admission transaction retires the old chapter contribution, compiles the new candidate, rebuilds projections for its accepted successors, and marks the former commit superseded only if every stage commits.

**Tech Stack:** Python 3.11, SQLAlchemy, Pydantic, pytest, PostgreSQL.

**Spec:** Project 4cd0265445594f519ff6f4cbab88d268 reproduced this failure: its corrected chapter 74 v12 is blocked because its former v11 GraphDelta rows remain canonical and encode duplicate braking events.

## Global Constraints

- Preserve ordinary new-chapter Canon admission and idempotency.
- Never accept new text while preserving contradictory GraphDelta rows for the replaced chapter.
- Retain later accepted chapter drafts and replay their graph deltas in chapter order.
- Make the replacement transactional; a failure restores old active canon.
- Retain the superseded commit as audit evidence.

---

### Task 1: Specify the historical-rewrite regression

**Files:**

- Modify: tests/test_project_operation_guards.py
- Modify: tests/test_canon_atomic_transaction.py

**Interfaces:**

- Consumes: retry_chapter_review(...), CanonAdmissionService.commit_plan(...), GraphDeltaRow, CanonCommitRecord.
- Produces: a test fixture with a committed chapter 2, accepted chapter 3 successor delta, and a corrected replacement candidate for chapter 2.

- [ ] **Step 1: Write the failing retry marker test**

~~~python
payload = api_module.retry_chapter_review(
    project.id, 2,
    ChapterReviewRetryRequest(reason="correct historical drift", allow_accepted=True),
)
assert payload.ok is True
assert _rewrite_marker(session, project.id, 2).previous_commit_id == old_commit.id
~~~

- [ ] **Step 2: Write the failing replacement test**

~~~python
outcome = CanonAdmissionService(session_factory=Session).commit_plan(rewritten_plan)
assert outcome.committed is True
assert _active_delta_summaries(Session, project.id, 2) == ["corrected custody-only event"]
assert _active_delta_summaries(Session, project.id, 3) == ["later accepted consequence"]
assert "obsolete braking event" not in _world_snapshot(Session, project.id, 3)
~~~

- [ ] **Step 3: Run the test to establish the baseline failure**

Run: pytest tests/test_project_operation_guards.py::ProjectOperationGuardsTests::test_retry_chapter_review_can_reset_accepted_when_explicitly_allowed tests/test_canon_atomic_transaction.py::test_historical_rewrite_replaces_old_delta_and_replays_later_accepted_deltas -v

Expected: the new test fails with BookState already contains this chapter.

- [ ] **Step 4: Commit the tests**

~~~bash
git add tests/test_project_operation_guards.py tests/test_canon_atomic_transaction.py
git commit -m "test(canon): cover historical rewrite replacement"
~~~

### Task 2: Implement transactional contribution replacement

**Files:**

- Create: forwin/canon/historical_rewrite.py
- Modify: forwin/application/projects/reviews.py:933-1045
- Modify: forwin/canon/admission.py:50-260
- Modify: forwin/book_state/repository.py:600-790

**Interfaces:**

- Consumes: a pending historical-rewrite marker, the project’s GraphDelta log, and the new CanonCommitPlan.
- Produces: HistoricalCanonRewriteService.replace_contribution(session, plan) returning retired delta IDs, replayed chapters, and superseded commit ID.

- [ ] **Step 1: Mark accepted retries before setting the chapter back to planned**

~~~python
if previous_status == "accepted":
    HistoricalCanonRewriteRepository(session).mark_pending(
        project_id=project_id,
        chapter_number=chapter_number,
        reason=reason,
    )
~~~

The marker must reference the existing committed CanonCommitRecord. Nonaccepted retries remain unchanged.

- [ ] **Step 2: Add repository methods for precise retirement and projection replay**

~~~python
def retire_chapter_deltas(self, project_id: str, chapter_number: int) -> list[str]: ...
def rebuild_project_range(self, project_id: str, *, from_chapter: int, through_chapter: int) -> list[int]: ...
~~~

Retire exactly the old chapter’s graph-delta and patch rows after capturing their IDs in the superseded commit record. Invalidate materializations from that chapter onward, replay retained accepted deltas in order, and preserve all Genesis/base material before the range.

- [ ] **Step 3: Integrate replacement in CanonAdmissionService.commit_plan**

~~~python
rewrite = HistoricalCanonRewriteService(session).prepare_replacement(plan)
rewrite.retire_old_contribution()
self._revalidate_locked_plan(...)
compile_result = BookStateCompiler(session).compile(...)
rewrite.rebuild_successor_projections()
rewrite.mark_prior_commit_superseded()
~~~

All steps execute in the existing admission transaction. Any exception rolls back retirement, compilation, and projections.

- [ ] **Step 4: Run the focused regression tests**

Run: pytest tests/test_project_operation_guards.py::ProjectOperationGuardsTests::test_retry_chapter_review_can_reset_accepted_when_explicitly_allowed tests/test_canon_atomic_transaction.py::test_historical_rewrite_replaces_old_delta_and_replays_later_accepted_deltas -v

Expected: PASS. The successor snapshot contains the corrected event and excludes the retired event.

- [ ] **Step 5: Commit the implementation**

~~~bash
git add forwin/canon/historical_rewrite.py forwin/application/projects/reviews.py forwin/canon/admission.py forwin/book_state/repository.py tests
git commit -m "fix(canon): replace stale historical rewrite deltas"
~~~

### Task 3: Verify stable-runtime compatibility and recover chapter 74

**Files:**

- Create: deploy/forwin-runtime-hotfixes/<commit>/Dockerfile
- Create: deploy/forwin-runtime-hotfixes/<commit>/apply_runtime_hotfix.py
- Modify: /Users/magi1/.codex/forwin-runs/graytide-20260905/运行记录.json

**Interfaces:**

- Consumes: stable compat-a6082199361c base image and the chapter-74 review gate.
- Produces: a verified compatibility image, accepted chapter 74 with replacement BookState, and a recorded run result.

- [ ] **Step 1: Run focused and full tests**

Run: pytest tests/test_canon_atomic_transaction.py tests/test_project_operation_guards.py tests/test_historical_rewrite_anchors.py -v && pytest -q

Expected: all focused tests and full suite pass.

- [ ] **Step 2: Build and validate a compatibility image against the pinned base**

Run: docker build -f deploy/forwin-runtime-hotfixes/<commit>/Dockerfile -t forwin-forwin:compat-<commit> deploy/forwin-runtime-hotfixes/<commit>

Expected: the hotfix validates target-file hashes and compiles each changed module.

- [ ] **Step 3: Sync through the 150 path and update only app, MCP, and generation services**

Run: ssh 10.0.0.150 '/home/taiwei/deploy-github-sync/bin/deploy-github-sync.sh --apply --project forwin --sync-only'

Expected: sync completes, the three compatibility services converge, and scripts/check_codex_operator_ready.py passes.

- [ ] **Step 4: Recover only through ForWin MCP**

~~~text
task_active_generation_check(project_id)
chapter_review_approve(project_id, 74, continue_generation=false, reason="full-text continuity review accepted")
chapter_get(project_id, 74)
world_model_get(project_id, as_of_chapter=76)
~~~

Expected: chapter 74 is accepted, its custody/loading facts are canonical, and obsolete duplicate braking events are absent.

- [ ] **Step 5: Commit the hotfix layer and run record**

~~~bash
git add deploy/forwin-runtime-hotfixes
git commit -m "fix(deploy): layer historical canon rewrite recovery"
~~~

### Task 4: Preserve deterministic historical replay ordering

**Files:**

- Modify: tests/test_canon_atomic_transaction.py
- Modify: forwin/book_state/repository.py
- Modify: forwin/canon/historical_rewrite.py
- Modify: forwin/canon/admission.py

**Interfaces:**

- Consumes: `CanonCommitRecord.graph_delta_ids_json` as the ordered Canon contribution manifest, retained `KnowledgeEditProposalRow` deltas, and `BookStateProjection.load_runtime_as_of`.
- Produces: historical rewind/replay that applies every accepted Canon contribution in its committed manifest order and applies a retained cognition patch exactly once.

- [ ] **Step 1: Write the two failing regression tests**

~~~python
def test_historical_rewrite_replays_successor_manifest_order_not_delta_id_order(...):
    # The committed manifest is ["z-first", "a-second"].  Applying the
    # alphabetic order leaves the shared state at "intermediate"; Canon order
    # must leave it at the hand-checked final value "final".
    assert _world_snapshot(Session, project_id, 3)["event-order"]["phase"] == "final"

def test_historical_rewrite_applies_retained_first_cognition_patch_once(...):
    # A retained world-edit append supplies one evidence reference for a new
    # character observer.  After replacement it must occur once, not twice.
    assert cognition_snapshot.evidence_by_ref["event-order"] == ["proof-1"]
~~~

- [ ] **Step 2: Run the focused tests to verify the current failures**

Run: `.venv/bin/pytest tests/test_canon_atomic_transaction.py -k 'manifest_order or first_cognition_patch_once' -v`

Expected: the order test demonstrates the ID-order replay defect and the cognition test demonstrates duplicated evidence.

- [ ] **Step 3: Make the smallest replay-only correction**

~~~python
# Reorder Canon-owned GraphDelta rows from each committed record's ordered
# `graph_delta_ids_json`; reject an incomplete or ambiguous manifest rather
# than guessing.  Pass this order into range invalidation and successor replay.
# When loading the replacement snapshot, apply retained cognition patches only
# if that snapshot already covers the replacement chapter; otherwise the
# projection loader has already applied them.
~~~

Do not change ordinary `list_graph_deltas` callers, database schema, publisher code, or generation behavior.

Keep `BookStateCompiler` construction in `forwin/canon/admission.py`: construct it once for Canon admission and pass it explicitly through the historical rewrite service into repository replay helpers.  Repository helpers may consume that compiler but must not construct one themselves; the existing architecture-boundary test is the regression gate for this dependency direction.

- [ ] **Step 4: Run focused tests and the full suite**

Run: `.venv/bin/pytest tests/test_canon_atomic_transaction.py -v && .venv/bin/pytest -q`

Expected: both regressions pass, historical rollback coverage stays green, and the full suite has no failures.

- [ ] **Step 5: Commit the corrective change**

~~~bash
git add tests/test_canon_atomic_transaction.py forwin/book_state/repository.py forwin/canon/historical_rewrite.py
git commit -m "fix(canon): preserve historical replay order"
~~~

## Self-Review

- Spec coverage: Task 1 reproduces the reset defect, Task 2 provides atomic replacement and successor replay, Task 3 verifies compatibility deployment and live recovery, and Task 4 preserves the actual committed delta order plus exact-once cognition replay.
- Placeholder scan: no TODO/TBD placeholders. The deployment commit placeholder is intentionally resolved from the completed implementation commit.
- Type consistency: Task 2 defines HistoricalCanonRewriteService.replace_contribution, and Task 3 uses only the existing MCP approval interface.

## Execution Handoff

Plan saved to docs/superpowers/plans/2026-09-06-historical-canon-rewrite.md. This automation has prior authorization to execute inline; use superpowers:executing-plans and stop before any projection rewrite that cannot be verified.
