# Legacy Compatibility Audit Correction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make legacy compatibility removal audit safe enough to identify deletion candidates without false positives from uninstrumented or rare paths.

**Architecture:** Runtime code records factual compatibility use only. `forwin.review_engine.audit` owns registry metadata, static scan helpers, and summary verdicts. API/runtime instrumentation sends facts into the existing `legacy_compatibility_used` event stream without mixing it with review-engine live cutover safety-net reporting.

**Tech Stack:** Python 3.13, pytest, SQLAlchemy models, existing ForWin audit and governance event helpers.

---

## File Structure

- Modify `forwin/review_engine/audit.py`: registry v2 metadata, summary verdict buckets, static scan helper.
- Modify `scripts/audit_review_engine_cutover.py`: pass static counts into legacy compatibility summary.
- Modify `forwin/book_state/runtime.py`: expose legacy location fallback facts through an observer callback.
- Modify `forwin/api_governance_support.py`: record legacy checkpoint status use idempotently.
- Modify `forwin/characters/creation.py`: record legacy entity creation facts when the request creates a legacy `Entity`.
- Modify `forwin/canon_quality/rule_profile.py`: remove the unused current-book fallback parameter and data.
- Delete `forwin/orchestrator/repair_coordinator.py` after confirming there are no runtime imports.
- Modify tests under `tests/review_engine`, `tests/test_book_state_runtime.py`, and `tests/test_governance_decision_api.py`.
- Update `docs/designs/review-engine-cutover-spec.md`.

### Task 1: Audit Summary Safety

**Files:**
- Modify: `tests/review_engine/test_legacy_compatibility_audit.py`
- Modify: `forwin/review_engine/audit.py`

- [ ] **Step 1: Write failing tests**

Add tests that assert uninstrumented features never become delete candidates, static-only features require targeted tests, and runtime+static features block removal.

- [ ] **Step 2: Verify red**

Run:

```bash
python3 -m pytest tests/review_engine/test_legacy_compatibility_audit.py -q
```

Expected: failures mentioning missing `static_counts` handling and missing `uninstrumented_no_delete_signal`.

- [ ] **Step 3: Implement registry v2 and summary buckets**

Add `removal_mode`, `instrumentation_status`, and `static_patterns` handling. Keep backward compatibility with old `default_assessment` where needed.

- [ ] **Step 4: Verify green**

Run the same pytest command and expect all tests in that file to pass.

### Task 2: Static Scan Integration

**Files:**
- Modify: `tests/review_engine/test_legacy_compatibility_audit.py`
- Modify: `forwin/review_engine/audit.py`
- Modify: `scripts/audit_review_engine_cutover.py`

- [ ] **Step 1: Write failing static scan test**

Create a temporary source tree and assert the static counter counts source call sites while excluding the registry/audit module and tests.

- [ ] **Step 2: Verify red**

Run:

```bash
python3 -m pytest tests/review_engine/test_legacy_compatibility_audit.py::test_static_legacy_compatibility_counts_exclude_registry_and_tests -q
```

Expected: import or assertion failure because the helper does not exist.

- [ ] **Step 3: Implement helper and CLI wiring**

Add `collect_legacy_compatibility_static_counts()` and pass it to `summarize_legacy_compatibility_audit()` when `--include-legacy-compat` is used.

- [ ] **Step 4: Verify green**

Run the same targeted test and `python3 scripts/audit_review_engine_cutover.py --help`.

### Task 3: Runtime Instrumentation

**Files:**
- Modify: `tests/test_book_state_runtime.py`
- Modify: `tests/test_governance_decision_api.py`
- Modify: `forwin/book_state/runtime.py`
- Modify: `forwin/api_governance_support.py`
- Modify: `forwin/characters/creation.py`

- [ ] **Step 1: Write failing instrumentation tests**

Assert BookState legacy location fallback calls an observer and legacy checkpoint API serialization records exactly one compatibility event for a legacy status.

- [ ] **Step 2: Verify red**

Run:

```bash
python3 -m pytest tests/test_book_state_runtime.py::test_distance_between_world_nodes_reports_legacy_location_fallback tests/test_governance_decision_api.py::GovernanceDecisionApiTests::test_legacy_approved_band_checkpoint_serializes_without_validation_error -q
```

Expected: observer argument missing and no compatibility event.

- [ ] **Step 3: Implement minimal instrumentation**

Use callback facts in BookState runtime, idempotent event persistence for checkpoint status, and character creation events via `StateUpdater.save_decision_event`.

- [ ] **Step 4: Verify green**

Run the targeted tests again.

### Task 4: Dead Code Cleanup and Docs

**Files:**
- Delete: `forwin/orchestrator/repair_coordinator.py`
- Modify: `forwin/canon_quality/rule_profile.py`
- Modify: `docs/designs/review-engine-cutover-spec.md`

- [ ] **Step 1: Verify static-dead status**

Run:

```bash
git grep -n "ChapterRepairCoordinator" -- forwin tests docs Design-docs
git grep -n "use_legacy_fallback" -- forwin tests
```

Expected: no runtime callers for `ChapterRepairCoordinator`; only the definition for `use_legacy_fallback`.

- [ ] **Step 2: Remove dead code**

Delete the dead coordinator module and remove the unused legacy fallback parameter/data.

- [ ] **Step 3: Update cutover spec**

Document the corrected registry semantics, explicit summary buckets, static scan requirement, and why import-only compatibility is not a delete candidate.

- [ ] **Step 4: Verify broad tests**

Run:

```bash
python3 -m pytest tests/review_engine -q
python3 -m pytest tests/test_book_state_runtime.py tests/test_governance_decision_api.py::GovernanceDecisionApiTests::test_legacy_approved_band_checkpoint_serializes_without_validation_error -q
python3 scripts/audit_review_engine_cutover.py --help
git diff --check
```

Expected: all tests pass and diff check is clean.
