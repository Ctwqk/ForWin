# ForWin v5 Candidate and Canon Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the durable candidate lifecycle and make Canon admission the single short, idempotent, atomic accepted-state transaction required by the v5 umbrella specification.

**Architecture:** Candidate versions are immutable records backed by immutable `ChapterDraft` rows. Expensive review, entity planning, BookState extraction, and BookState review produce a serialized `CanonCommitPlan` before the write transaction. `CanonAdmissionService` then locks the project and candidate, revalidates expected versions, applies every authoritative write, records a `CanonCommitRecord`, and enqueues deterministic outbox events in one transaction.

**Tech Stack:** Python 3.13, Pydantic v2, SQLAlchemy 2, PostgreSQL, Alembic, pytest.

## Global Constraints

- Existing project data and existing databases are not migrated; regenerate the single `0001_v5_baseline` after models stabilize.
- BookState and BookMap remain authoritative runtime state; projection and publisher work runs after commit through outbox events.
- No LLM, Qdrant, filesystem export, publisher browser, or network call may run inside the Canon write transaction.
- Spark or human gate approval cannot modify review, eligibility, entity admission, or Canon plan data.
- A rejected or failed candidate must not change BookState, BookMap, entities, aliases, obligations, chapter acceptance, or outbox state.
- Focused tests are run during implementation; broad regression and long-run gates remain in the release-verification slice.

---

### Task 1: Durable Immutable Candidate Versions

**Files:**
- Modify: `forwin/models/draft.py`
- Replace: `forwin/candidate_drafts.py`
- Modify: `forwin/generation/pipeline_core/review_autofix.py`
- Test: `tests/test_candidate_lifecycle_v5.py`

**Interfaces:**
- Consumes: `ChapterDraft`, `ChapterReview`, `WriterOutput`, `EntityAdmissionPlan`, `RuntimePolicy` version.
- Produces: `CandidateDraftRepository.create_reviewed_version(...)`, `transition(...)`, `attach_canon_plan(...)`, and `candidate_body_hash(...)`.

- [x] **Step 1: Write failing candidate lifecycle tests**

```python
def test_candidate_versions_are_append_only(session, project, chapter_plan):
    first = repository.create_reviewed_version(..., draft=first_draft, parent_candidate_id="")
    second = repository.create_reviewed_version(..., draft=second_draft, parent_candidate_id=first.id)
    assert first.id != second.id
    assert first.status == "reviewed"
    assert second.parent_candidate_id == first.id

def test_candidate_transition_rejects_illegal_state(session, candidate):
    with pytest.raises(CandidateTransitionError):
        repository.transition(candidate.id, "accepted")
```

- [x] **Step 2: Run the tests and confirm RED**

Run: `uv run pytest -q tests/test_candidate_lifecycle_v5.py`

Expected: import or attribute failures because the v5 lifecycle API and columns do not exist.

- [x] **Step 3: Add candidate contract columns and legal transitions**

`CandidateDraftRecord` gains non-null fields for `parent_candidate_id`, `body_hash`, `plan_revision`, `writer_artifact_ref`, `review_result_json`, `repair_history_json`, `entity_admission_plan_json`, `eligibility_decision_json`, `policy_version`, `canon_commit_plan_json`, `canon_commit_id`, and `idempotency_key`. Add a unique `(project_id, chapter_number, version)` index and a unique `candidate_draft_id` index.

Use this state graph:

```python
LEGAL_TRANSITIONS = {
    "drafted": {"reviewing", "needs_review", "failed"},
    "reviewing": {"reviewed", "repairing", "needs_review", "failed"},
    "reviewed": {"repairing", "ready_for_canon", "needs_review", "failed"},
    "repairing": {"reviewed", "ready_for_canon", "needs_review", "failed"},
    "ready_for_canon": {"committing", "needs_review", "failed"},
    "committing": {"accepted", "ready_for_canon"},
    "needs_review": {"reviewing", "repairing", "ready_for_canon", "failed"},
    "failed": set(),
    "accepted": set(),
}
```

- [x] **Step 4: Replace review upsert with append-only creation**

`_persist_draft_and_review` creates one candidate per new `ChapterDraft`, links repaired drafts to the prior candidate, stores body/plan fingerprints and policy version, and never rewrites an older candidate version.

- [x] **Step 5: Run candidate tests and focused legacy candidate tests**

Run: `uv run pytest -q tests/test_candidate_lifecycle_v5.py tests/test_candidate_draft_records.py tests/test_candidate_draft_alias.py`

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add forwin/models/draft.py forwin/candidate_drafts.py forwin/generation/pipeline_core/review_autofix.py tests/test_candidate_lifecycle_v5.py
git commit -m "Make candidate versions durable and immutable"
```

---

### Task 2: Prepare Complete Canon Commit Plans Outside the Transaction

**Files:**
- Create: `forwin/canon/plan.py`
- Create: `forwin/canon/preparation.py`
- Modify: `forwin/canon/types.py`
- Modify: `forwin/canon/__init__.py`
- Modify: `forwin/generation/pipeline_core/world_projection.py`
- Test: `tests/test_canon_commit_plan.py`

**Interfaces:**
- Consumes: reviewed candidate, `WriterOutput`, `ReviewVerdict`, `EntityAdmissionPlan`, approved `GraphDelta` set, current chapter/BookState versions.
- Produces: frozen Pydantic `CanonCommitPlan` and `CanonPreparationOutcome`.

- [ ] **Step 1: Write failing plan preparation tests**

```python
def test_prepare_plan_performs_no_authoritative_writes(session, prepared_candidate):
    before = authoritative_counts(session)
    outcome = preparation.prepare(...)
    assert outcome.plan is not None
    assert authoritative_counts(session) == before

def test_plan_idempotency_key_changes_with_candidate_hash(prepared_candidate):
    first = CanonCommitPlan.build(..., candidate_body_hash="a")
    second = CanonCommitPlan.build(..., candidate_body_hash="b")
    assert first.idempotency_key != second.idempotency_key
```

- [ ] **Step 2: Run and confirm RED**

Run: `uv run pytest -q tests/test_canon_commit_plan.py`

Expected: `forwin.canon.plan` and preparation service do not exist.

- [ ] **Step 3: Define the frozen plan DTO**

```python
class CanonCommitPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["v1"] = "v1"
    project_id: str
    chapter_number: int
    candidate_id: str
    candidate_body_hash: str
    plan_revision: str
    policy_version: int
    expected_previous_accepted_chapter: int
    expected_book_state_chapter: int
    approved_book_state_changes: ApprovedGraphDeltaSet
    entity_admission_plan: EntityAdmissionPlan
    acceptance_mode: str
    repair_attempt_count: int
    residual_review_issues: list[dict[str, Any]]
    canon_risk_level: str
    audit_events: tuple[CanonAuditEvent, ...]
    outbox_events: tuple[CanonOutboxEvent, ...]
    idempotency_key: str
```

- [ ] **Step 4: Split BookState prepare from compile**

Move extraction and `BookStateReviewGate` work out of `_commit_book_state_canon`. Preparation returns approved changes or an explicit block. It must not call `BookStateCompiler`, `KnowledgeProjectionRefresher`, memory index, publisher code, or `session.commit()`.

- [ ] **Step 5: Persist the plan on the candidate**

On success, transition the candidate to `ready_for_canon` and save `canon_commit_plan_json` plus `idempotency_key`. On a quality, entity, or BookState preparation block, transition to `needs_review` without authoritative writes.

- [ ] **Step 6: Run focused preparation tests**

Run: `uv run pytest -q tests/test_canon_commit_plan.py tests/test_book_state_direct_commit.py tests/test_canon_admission_gate.py`

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add forwin/canon forwin/generation/pipeline_core/world_projection.py tests/test_canon_commit_plan.py
git commit -m "Prepare Canon commit plans before transactions"
```

---

### Task 3: Atomic, Locked, Idempotent Canon Transaction

**Files:**
- Create: `forwin/models/canon.py`
- Modify: `forwin/models/__init__.py`
- Replace: `forwin/canon/admission.py`
- Modify: `forwin/canon/entity_admission.py`
- Modify: `forwin/book_state/review_gate_ext.py`
- Modify: `forwin/outbox/store.py`
- Test: `tests/test_canon_atomic_transaction.py`

**Interfaces:**
- Consumes: persisted `CanonCommitPlan` and a `sessionmaker` owned by `CanonAdmissionService`.
- Produces: `CanonAdmissionOutcome` containing `commit_id`, compile result, idempotent flag, or a typed stale/block result.

- [ ] **Step 1: Write failing atomicity, rollback, stale, and idempotency tests**

```python
@pytest.mark.parametrize("failure_stage", ["book_state", "entity", "obligation", "chapter", "outbox"])
def test_canon_failure_rolls_back_every_authoritative_write(failure_stage, prepared_plan):
    outcome = service.commit_plan(prepared_plan, failure_injector=fail_at(failure_stage))
    assert outcome.blocked
    assert_authoritative_state_unchanged()

def test_same_idempotency_key_returns_prior_commit(prepared_plan):
    first = service.commit_plan(prepared_plan)
    second = service.commit_plan(prepared_plan)
    assert second.commit_id == first.commit_id
    assert second.idempotent is True
```

- [ ] **Step 2: Run and confirm RED**

Run: `uv run pytest -q tests/test_canon_atomic_transaction.py`

Expected: missing `CanonCommitRecord` and `commit_plan` failures.

- [ ] **Step 3: Add `CanonCommitRecord`**

The table has unique `idempotency_key` and `candidate_id`, plus project/chapter, expected versions, graph delta ids, snapshot ids, status, result JSON, and timestamps.

- [ ] **Step 4: Implement the transaction**

`commit_plan` opens `with session_factory.begin() as session`, locks `Project`, `ChapterPlan`, and `CandidateDraftRecord` with `FOR UPDATE`, revalidates candidate body hash, plan revision, previous accepted chapter, and latest BookState chapter, then applies in order:

1. transition candidate to `committing`;
2. compile approved BookState changes;
3. apply entity/alias plan;
4. activate obligation and plan lifecycle mutations;
5. mark chapter accepted and candidate accepted;
6. insert Canon audit rows;
7. insert deterministic projection and publisher outbox rows;
8. insert committed `CanonCommitRecord`.

No exception is swallowed inside the transaction. A stale revalidation raises `CanonStaleVersion`; the outer handler returns the candidate to `ready_for_canon`. Other failures leave all authoritative tables unchanged and mark the candidate `failed` in a separate diagnostic transaction.

- [ ] **Step 5: Run atomic tests and BookState compiler tests**

Run: `uv run pytest -q tests/test_canon_atomic_transaction.py tests/test_book_state_compiler.py tests/test_book_state_final.py tests/test_entity_admission_planner.py`

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add forwin/models forwin/canon forwin/book_state/review_gate_ext.py forwin/outbox/store.py tests/test_canon_atomic_transaction.py
git commit -m "Make Canon admission atomic and idempotent"
```

---

### Task 4: Adopt Prepared Canon and Remove In-Transaction Side Effects

**Files:**
- Modify: `forwin/generation/pipeline_core/project_chapters.py`
- Modify: `forwin/generation/pipeline_core/acceptance.py`
- Modify: `forwin/generation/pipeline_core/world_projection.py`
- Modify: `forwin/runtime/container.py`
- Create: `forwin/knowledge_system/canon_outbox.py`
- Modify: `forwin/outbox/handlers.py`
- Test: `tests/test_candidate_to_canon_flow_v5.py`
- Test: `tests/test_projection_outbox.py`

**Interfaces:**
- Consumes: `CanonPreparationService.prepare(...)`, `CanonAdmissionService.commit_plan(...)`.
- Produces: accepted pipeline result plus retryable post-commit outbox work.

- [ ] **Step 1: Write failing end-to-end focused tests**

```python
def test_rejected_candidate_changes_no_authoritative_state(...): ...
def test_projection_failure_preserves_accepted_chapter_and_retries(...): ...
def test_pipeline_does_not_call_memory_or_projection_before_commit(...): ...
```

- [ ] **Step 2: Run and confirm RED**

Run: `uv run pytest -q tests/test_candidate_to_canon_flow_v5.py tests/test_projection_outbox.py`

- [ ] **Step 3: Replace direct commit calls**

Both automatic generation and manual acceptance prepare a plan, durably save `ready_for_canon`, then submit the same plan to `commit_plan`. Remove chapter acceptance writes, entity writes, obligation activation, knowledge refresh, and memory-index mutation from `project_chapters.py`.

- [ ] **Step 4: Handle post-commit work through outbox**

Add a deterministic `canon.post_commit.requested` handler that refreshes knowledge/Qdrant/Obsidian and indexes accepted chapter text. Failure uses existing outbox retry semantics and cannot change accepted state. Publisher notification remains isolated and is represented by a separate deterministic outbox event consumed by the production publisher application boundary.

- [ ] **Step 5: Make post-acceptance planning failure non-destructive**

Phase 3/4 planning and simulation run after the Canon transaction. Their failure records deferred maintenance and stops or degrades the task without changing the accepted chapter to failed.

- [ ] **Step 6: Run focused flow, outbox, publisher, and pipeline tests**

Run: `uv run pytest -q tests/test_candidate_to_canon_flow_v5.py tests/test_projection_outbox.py tests/test_project_publish_bindings.py tests/test_world_v4_orchestrator_gate.py tests/test_gate_delegation_chapter.py`

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add forwin/generation forwin/runtime/container.py forwin/knowledge_system/canon_outbox.py forwin/outbox/handlers.py tests/test_candidate_to_canon_flow_v5.py tests/test_projection_outbox.py
git commit -m "Route chapter acceptance through prepared Canon commits"
```

---

### Task 5: Schema Baseline and Architecture Guards

**Files:**
- Regenerate: `forwin/migrations/versions/0001_v5_baseline.py`
- Modify: `tests/test_architecture_boundaries.py`
- Create: `tests/test_v5_canon_architecture.py`
- Modify: `Design-docs/CURRENT_ARCHITECTURE.md`
- Modify: `Design-docs/DESIGN_STATUS.md`
- Modify: `forwin_architecture_consolidation_audit.md`

**Interfaces:**
- Consumes: completed candidate and Canon implementation.
- Produces: one clean v5 baseline and static guards against regression.

- [ ] **Step 1: Add failing architecture guards**

Guards reject Canon-path `KnowledgeProjectionRefresher`, memory-index writes, publisher calls, chapter acceptance outside `forwin/canon`, mutable candidate upsert, missing unique Canon idempotency key, and authoritative writes outside `CanonAdmissionService`.

- [ ] **Step 2: Run and confirm RED before final deletion**

Run: `uv run pytest -q tests/test_v5_canon_architecture.py`

- [ ] **Step 3: Regenerate the only Alembic baseline**

Run the repository baseline generator against current metadata, then verify upgrade, schema check, downgrade, and upgrade on a disposable PostgreSQL database.

- [ ] **Step 4: Correct documentation status**

Record Slice 3/4 as implementation-complete only after focused evidence exists. Keep release verification, 200 chapters, push, and deployment explicitly incomplete.

- [ ] **Step 5: Run slice verification**

Run:

```bash
uv run python -m compileall -q forwin scripts
uvx ruff check forwin scripts --select F401,F403,F405,F821
uv run pytest -q tests/test_candidate_lifecycle_v5.py tests/test_canon_commit_plan.py tests/test_canon_atomic_transaction.py tests/test_candidate_to_canon_flow_v5.py tests/test_v5_canon_architecture.py
uv run pytest --collect-only -q
git diff --check
```

- [ ] **Step 6: Commit**

```bash
git add forwin/migrations tests Design-docs forwin_architecture_consolidation_audit.md
git commit -m "Complete v5 candidate and Canon convergence"
```

---

## Self-Review

- Spec coverage: this plan covers candidate lifecycle, entity plan persistence, Canon preparation, atomic authoritative writes, idempotency, stale conflicts, outbox isolation, rollback tests, and the schema baseline portions of umbrella Slices 3 and 4.
- Deliberate exclusions: pipeline method-family ownership, audit/governance package renaming, HTTP/application adapters, layered review UI, full regression, long runs, push, and deployment each require their own implementation plan after this slice is green.
- Placeholder scan: no implementation step relies on a compatibility shim or unspecified legacy migration.
- Type consistency: `CanonCommitPlan`, `CanonPreparationOutcome`, `CanonAdmissionOutcome`, and candidate states retain the same names across tasks.
