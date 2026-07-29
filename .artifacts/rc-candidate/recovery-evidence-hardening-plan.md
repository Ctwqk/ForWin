# ForWin v5 Live Recovery Evidence Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make all eleven RC live-recovery results independently reproducible
from source-bound snapshots instead of trusting report-supplied assertions.

**Architecture:** Add one pure semantic evaluator shared by subsystem runners
and the finalizer. Keep Compose lifecycle control in `recovery_stack.py`, and
put deterministic fault setup, business-state collection, cleanup, and report
production in three focused runners.

**Tech Stack:** Python 3.13, FastMCP, httpx, SQLAlchemy/psycopg, PostgreSQL
advisory locks and scoped triggers, Docker Compose, Qdrant, MinIO, pytest.

## Global Constraints

- No production runtime mode, compatibility path, workflow engine, or global
  metrics framework.
- No assertion may be accepted from report prose or copied booleans.
- Every PASS is derived from immutable `before`, `during`, and `after`
  snapshots and one terminal destroyed stack.
- Every fault uses a new database volume, evidence directory, and fault ID.
- A missing deterministic boundary is `setup_blocked`, never PASS.
- Fixtures are general and projectless where the subsystem permits it.
- No real publisher mutation is allowed in RC recovery fixtures.
- Barrier installation is scoped by fault ID and fixture identity; cleanup is
  mandatory and residue is a failed run.
- The R6 matrix remains bound to `80af1c8`; all harness changes must remain
  inside the collector's bounded-successor allowlist.

---

### Task 1: Pure Recovery Evidence Contract

**Files:**
- Create: `.artifacts/rc-candidate/recovery_evidence.py`
- Create: `.artifacts/rc-candidate/test_recovery_evidence.py`
- Modify: `.artifacts/rc-candidate/release-source-files.txt`

**Interfaces:**
- Produces:
  `load_snapshot(path: Path) -> dict[str, Any]`,
  `derive_assertions(kind: str, snapshots: Mapping[str, dict]) -> dict[str, Any]`,
  `snapshot_violations(kind: str, snapshots: Mapping[str, dict]) -> list[str]`,
  `assertion_violations(kind: str, assertions: Mapping[str, Any]) -> list[str]`,
  `stable_hash(value: Any) -> str`.
- Consumes only JSON-compatible values; it performs no I/O except loading
  explicitly named snapshots.

- [ ] **Step 1: Add failing schema tests**

  Cover every fault kind with empty `state={}` and assert that validation
  reports required-path violations. Add tests that reject unknown keys,
  mutable timestamp substitution in stable identities, duplicate natural
  keys, and cross-fault snapshot identities.

- [ ] **Step 2: Run the focused test and confirm failure**

  Run:
  `uv run --offline python -m pytest -q .artifacts/rc-candidate/test_recovery_evidence.py`

  Expected: import failure because `recovery_evidence.py` does not exist.

- [ ] **Step 3: Implement schema version 2 and common helpers**

  Define:

  ```python
  SNAPSHOT_SCHEMA_VERSION = 2
  STAGES = ("before", "during", "after")

  def required_path(value: Mapping[str, Any], dotted: str) -> Any: ...
  def stable_hash(value: Any) -> str: ...
  def duplicate_excess(rows: Sequence[Mapping[str, Any]], keys: tuple[str, ...]) -> int: ...
  ```

  Canonical JSON must use sorted keys and compact separators. Missing paths
  raise a typed `EvidenceContractError`; validators convert it to a precise
  violation.

- [ ] **Step 4: Implement all eleven derived contracts**

  Use one private function per fault kind. Exact formulas are:

  - generation pre-commit: same task ID, higher lease epoch, zero Canon during,
    one Canon after, zero duplicate authoritative identities;
  - generation post-commit: same task/higher epoch, identical Canon and
    accepted bundles during/after, zero duplicates;
  - Qdrant: identical Canon, retry attempt observed, all projections
    converged, zero duplicate point identities;
  - projection consumer: identical Canon, stable outbox identity/payload,
    converged projections, zero duplicate projection identities;
  - MinIO pre-Canon: zero Canon during, stable candidate identity, one Canon
    after, zero duplicates;
  - MinIO post-Canon: stable Canon and accepted bundle, same maintenance
    natural key with increased attempt/epoch, identical artifact key, zero
    barrier residue and duplicates;
  - publisher backend: stable Canon inventory, same job with a new owner token,
    stale token rejected, shared path readable, orphan residue zero, duplicate
    job/attempt/receipt counts zero;
  - publisher browser: stable Canon inventory, same pending job through browser
    stop/start, heartbeat recovered, zero attempts and receipts;
  - risk faults: same job, paused state/fence, one authenticated resume action,
    idempotent replay, bypass false, zero receipts.

- [ ] **Step 5: Run focused tests**

  Run the Task 1 test file and require PASS.

- [ ] **Step 6: Commit**

  Commit message:
  `test: derive live recovery assertions from snapshots`

---

### Task 2: Fail-Closed Recovery Finalizer

**Files:**
- Modify: `.artifacts/rc-candidate/finalize_recovery.py`
- Modify: `.artifacts/rc-candidate/test_finalize_recovery.py`
- Modify: `.artifacts/rc-candidate/collect_rc_manifest.py`
- Modify: `.artifacts/rc-candidate/test_collect_rc_manifest.py`

**Interfaces:**
- Consumes Task 1's `load_snapshot`, `derive_assertions`,
  `snapshot_violations`, and `assertion_violations`.
- Produces recovery manifest schema version 2.

- [ ] **Step 1: Replace empty-state passing fixtures**

  Build one minimal valid snapshot factory per fault family. Add failures for:
  an empty state, a modified snapshot with unchanged report assertions, a
  modified report assertion with unchanged snapshots, and missing destroy.

- [ ] **Step 2: Confirm the tests fail against the current finalizer**

  Run:
  `uv run --offline python -m pytest -q .artifacts/rc-candidate/test_finalize_recovery.py`

- [ ] **Step 3: Import and bind the semantic evaluator**

  Record its source path/hash in both per-fault and aggregate manifests. Remove
  the local authoritative `FAULT_CONTRACTS`; expose a compatibility alias only
  for readers that enumerate kinds.

- [ ] **Step 4: Recompute report assertions**

  After loading each snapshot, call `derive_assertions`. Reject a report when:

  ```text
  report.assertions != derived assertions
  report.result != pass
  report.replay_result != pass
  derived assertions violate the expected contract
  ```

  `expected[]` and `actual[]` remain explanatory text and never affect PASS.

- [ ] **Step 5: Require an independent terminal lifecycle for all faults**

  Require exactly one `fresh_up_started`, one `fresh_up_completed`, the typed
  fault marker before its recovery marker, and one final `destroyed` event.
  The destroy snapshot must contain the exact nine services, each with
  `exists=false` and `running=false`. Risk faults are subject to the same
  lifecycle requirement.

- [ ] **Step 6: Bind the evaluator in the final RC collector**

  Add it to `RELEASE_HARNESS_PATHS` and final-manifest hash validation. Update
  collector fixtures to prove a swapped evaluator is rejected.

- [ ] **Step 7: Run focused tests and commit**

  Run both modified test files. Commit:
  `test: fail closed on recovery evidence semantics`

---

### Task 3: Confirmed Stack Lifecycle Events

**Files:**
- Modify: `.artifacts/rc-candidate/recovery_stack.py`
- Modify: `.artifacts/rc-candidate/test_recovery_stack.py`
- Modify: `.artifacts/rc-candidate/docker-compose.recovery.yml`

**Interfaces:**
- Produces event schema version 2 with `requested_at` and confirmed
  `fault_time`/`recovery_time`.
- Adds:
  `mark <fault_kind> <fault|recovery> --fault-id ID`.

- [ ] **Step 1: Add failing event tests**

  Prove stop/kill timestamps are recorded after non-running inspection,
  recovery timestamps after readiness/probe, destroy is terminal, duplicate
  markers are rejected, and risk markers require one of the three typed risk
  kinds.

- [ ] **Step 2: Correct harness identity**

  Bind both `finalize_v1.py` and `finalize_recovery.py` under unambiguous names
  instead of labeling the V1 finalizer as the recovery finalizer.

- [ ] **Step 3: Record confirmed service times**

  Capture `requested_at` before the Compose operation and set
  `fault_time`/`recovery_time` only after the postcondition has been observed.

- [ ] **Step 4: Add typed API-fault markers**

  `mark` writes only evidence metadata. It accepts:
  `publisher_captcha`, `publisher_mfa`, or `publisher_account_risk`, plus
  `fault|recovery`. The business snapshot evaluator remains authoritative.

- [ ] **Step 5: Strengthen destroy**

  Refuse destroy without one completed fresh-up, write one terminal event, and
  fail if any service still exists. Reject all later controller operations in
  the same evidence directory.

- [ ] **Step 6: Run tests and commit**

  Commit:
  `test: seal recovery stack lifecycle events`

---

### Task 4: Generation and Projection Live Runner

**Files:**
- Create: `.artifacts/rc-candidate/generation_projection_recovery.py`
- Create: `.artifacts/rc-candidate/test_generation_projection_recovery.py`
- Modify: `.artifacts/rc-candidate/generation-worker-crash-barriers.md`

**Interfaces:**
- CLI:

  ```text
  run --fault-kind KIND --fault-id ID --candidate-manifest PATH
      --mcp-url URL --api-url URL --database-url-env NAME
      --evidence-dir PATH
  ```

- Supports:
  `generation_worker_precommit_crash`,
  `generation_worker_postcommit_crash`,
  `qdrant_unavailable`,
  `projection_consumer_unavailable`.

- [ ] **Step 1: Test deterministic advisory-lock barriers**

  Use fake psycopg connections to prove generated function/trigger names are
  fault-ID scoped, SQL parameters carry project/chapter identity, one holder
  and one waiter are required, and cleanup is symmetric.

- [ ] **Step 2: Test pure collectors and report production**

  Mock MCP/API/SQL/Qdrant reads. Ensure reports are atomic, no-clobber, and
  `setup_blocked` is emitted when a one-chapter fixture cannot reach its
  required boundary.

- [ ] **Step 3: Implement the one-chapter supported MCP lifecycle**

  Parameterize MCP URL. Create, generate, and lock all six Genesis stages;
  verify no active task before writing. Never modify story content after
  creation to reach a barrier. Treat chapter-plan creation and candidate
  creation as distinct readiness boundaries: allow the model-backed pipeline
  up to 900 seconds to create the candidate, and fail closed immediately if
  the task becomes terminal first.

- [ ] **Step 4: Implement pre/post-commit barriers**

  Pre-commit blocks scoped `canon_commit_records` insertion. Post-commit blocks
  scoped planning maintenance insertion. Prove the blocked worker PID before
  SIGKILL and clean all database objects in `finally`. Start the bounded
  blocked-waiter timer only after the candidate boundary is visible; stop it
  immediately if the task reaches a terminal status before the Canon boundary.

- [ ] **Step 5: Implement Qdrant/outbox faults**

  Prepare a review-ready candidate, stop the selected service through the
  controller, approve through the supported API, capture degraded durable
  state, recover, wait for convergence, then replay the existing projection
  refresh endpoint.

- [ ] **Step 6: Derive, write, re-open, and revalidate each report**

  The runner must call Task 1's evaluator before writing PASS and once more
  after reading the written artifacts.

- [ ] **Step 7: Run focused tests and commit**

  Commit:
  `test: automate generation and projection recovery proof`

---

### Task 5: MinIO Live Runner

**Files:**
- Create: `.artifacts/rc-candidate/minio_recovery.py`
- Create: `.artifacts/rc-candidate/test_minio_recovery.py`
- Modify: `.artifacts/rc-candidate/post-canon-minio-barrier.md`

**Interfaces:**
- Same CLI shape as Task 4.
- Supports:
  `minio_pre_canon_unavailable`,
  `minio_post_canon_unavailable`.

- [ ] **Step 1: Test MinIO inventory normalization**

  Prove object identity is `{key, etag, size, content_type, content_sha256}`;
  timestamps are excluded. Verify the expected post-Canon world key is derived
  from project and Canon IDs.

- [ ] **Step 2: Test the post-Canon barrier**

  Require one holder, one waiter at `review_approved`, exactly one committed
  Canon before MinIO stop, and zero trigger/function residue after cleanup.

- [ ] **Step 3: Implement pre-Canon outage**

  Stop MinIO after a review-ready artifact exists, issue the supported approval
  request, require failure before Canon, recover MinIO, replay the identical
  request, and wait for Canon/outbox/maintenance convergence.

- [ ] **Step 4: Implement post-Canon outage**

  Stop outbox, install the review-event barrier, start approval asynchronously,
  prove Canon committed and the request blocked, stop MinIO, release the
  barrier, and require `maintenance_pending`.

- [ ] **Step 5: Add deterministic same-event replay**

  In the disposable database only, conditionally release the exact processed
  phase-3 outbox event back to pending without changing its event ID, payload,
  aggregate, or idempotency key. Record this deliberate fault operation, let
  the real outbox worker process it, and prove no new maintenance or artifact
  identity.

- [ ] **Step 6: Run focused tests and commit**

  Commit:
  `test: automate minio recovery proof`

---

### Task 6: Publisher Live Runner

**Files:**
- Create: `.artifacts/rc-candidate/publisher_recovery.py`
- Create: `.artifacts/rc-candidate/test_publisher_recovery.py`
- Modify: `.artifacts/rc-candidate/publisher-worker-reclaim-brief.md`

**Interfaces:**
- Same CLI shape as Task 4.
- Supports:
  `publisher_backend_unavailable`,
  `publisher_browser_unavailable`,
  `publisher_captcha`,
  `publisher_mfa`,
  `publisher_account_risk`.

- [ ] **Step 1: Test the projectless fixture**

  Assert fixed generic content, `publish=false`, no project ID, unique logical
  key from fault ID, no external receipt, and no stored credentials.

- [ ] **Step 2: Implement deterministic backend reclaim**

  Stop the worker, create a scoped cover job, install a terminal-update
  advisory barrier, start the worker, wait for a durable backend owner token
  and blocked terminal write, then SIGKILL. Recover and prove a new token,
  stale-token rejection, shared cover readability, orphan cleanup, and no
  duplicate job/attempt/receipt.

- [ ] **Step 3: Implement browser pre-claim outage**

  Create a projectless pending chapter job, stop the browser, prove heartbeat
  stale while the job remains unchanged, restart and prove healthy heartbeat.
  Require zero attempts, mutations, and receipts. This is the release plan's
  browser-unavailable contract; uncertain remote mutation remains covered by
  deterministic unit gates and is not faked without a real platform.

- [ ] **Step 4: Implement three typed risk flows**

  For each independent stack, claim through the supported extension API, pause
  with the exact fence and typed reason at pre-mutation, mark the confirmed
  fault, resume through authenticated operator API, replay the same resume,
  mark recovery, and prove same job, one operator action, no bypass, no receipt.

- [ ] **Step 5: Run focused tests and commit**

  Commit:
  `test: automate publisher recovery proof`

---

### Task 7: Release Harness Binding and Runbooks

**Files:**
- Modify: `.artifacts/rc-candidate/collect_rc_manifest.py`
- Modify: `.artifacts/rc-candidate/test_collect_rc_manifest.py`
- Modify: `.artifacts/rc-candidate/release-source-files.txt`
- Modify: `.artifacts/rc-candidate/recovery-evidence-runbook.md`
- Modify: `.artifacts/rc-candidate/rc-freeze-runbook.md`
- Modify: `.artifacts/rc-candidate/release-evidence-ledger.md`

**Interfaces:**
- Collector binds the evaluator and all three runner hashes.

- [ ] **Step 1: Add all new executable and test paths to the reviewed inventory**

- [ ] **Step 2: Require one candidate identity across runner reports**

  Revalidate source SHA/tree, five image IDs, candidate-manifest hash,
  evaluator hash, runner hash, endpoints, and event-chain Docker identity.

- [ ] **Step 3: Replace prose-only recovery steps with exact runner commands**

  Document one new directory and fault ID per run. State that `setup_blocked`
  cannot satisfy finalization.

- [ ] **Step 4: Run all release-harness unit tests and commit**

  Commit:
  `docs: bind executable live recovery evidence`

---

### Task 8: Freeze and Execute the Exact Candidate

**Files:**
- Evidence only under `.artifacts/v1-release-gate/`,
  `.artifacts/v5-recovery-*`, `.artifacts/v5-post-decision-smoke/`,
  `.artifacts/v5-rc/`.

**Interfaces:**
- Consumes every prior task at one clean candidate SHA.

- [ ] **Step 1: Wait for L100 to reach 100 and finalize the immutable matrix**

- [ ] **Step 2: Run focused release-harness tests and the required RC gates**

- [ ] **Step 3: Commit any generalized harness defect and restart Task 8 from
  image build**

  A code change invalidates all candidate evidence. Never patch a live fault
  run.

- [ ] **Step 4: Build runtime and browser images at the final source SHA**

- [ ] **Step 5: Collect the draft candidate manifest**

- [ ] **Step 6: Execute V1, all eleven independent faults, and fresh30**

- [ ] **Step 7: Create the annotated RC tag and collect the final manifest**

- [ ] **Step 8: Start the fresh L200 no-hotfix gate**

  No code, prompt, route, config, schema, rule, or threshold changes are
  permitted after this point.
