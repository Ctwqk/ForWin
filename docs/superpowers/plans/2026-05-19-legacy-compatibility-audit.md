# Legacy Compatibility Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a unified legacy compatibility usage audit stream and include it in the 60-chapter cutover audit without weakening the existing review safety-net gate.

**Architecture:** Runtime call sites emit fact-only `legacy_compatibility_used` `DecisionEvent`s through a small helper. `forwin.review_engine.audit` owns the compatibility registry and derives deletion/removal conclusions only when summarizing a full run. `scripts/audit_review_engine_cutover.py` gains `--include-legacy-compat` to include the new summary beside the existing strict review cutover result.

**Tech Stack:** Python 3.13, SQLAlchemy models, existing `DecisionEvent` governance log, pytest.

---

## File Structure

- Modify `forwin/governance.py`: add `DecisionEventType.LEGACY_COMPATIBILITY_USED` to the known event registry.
- Modify `forwin/review_engine/audit.py`: add compatibility registry, fact payload builder, and summary/assessment helpers.
- Modify `forwin/orchestrator_loop_core/governance.py`: add `_record_legacy_compatibility_event()` helper that writes fact-only events.
- Modify `forwin/orchestrator_loop_core/service.py`: attach the new helper to `WritingOrchestrator`.
- Modify generation-adjacent call sites:
  - `forwin/orchestrator_loop_core/governance.py`: record `legacy_relaxed` fallback when project governance falls back to legacy mode.
  - `forwin/orchestrator_loop_core/world_projection.py`: record legacy projection compatibility usage before attempting legacy projection.
  - `forwin/orchestrator_loop_core/finalization.py`: record legacy projection compatibility usage before legacy finalization projection.
  - `forwin/book_state/runtime.py`: expose fact metadata when legacy `state.location` fallback is used.
  - `forwin/book_state/reviewer.py`: expose fact metadata when legacy `state.location` patch downgrade is used.
  - `forwin/subworld_manager.py`: optionally accept an event callback and record `legacy_entity_id` bridge/create usage when orchestrator supplies it.
- Modify `scripts/audit_review_engine_cutover.py`: add `--include-legacy-compat` and query/summarize `legacy_compatibility_used` events.
- Modify `docs/designs/review-engine-cutover-spec.md`: document the new fact-only event and summary rules.
- Add/modify tests:
  - `tests/review_engine/test_audit.py`
  - `tests/review_engine/test_legacy_compatibility_audit.py`
  - targeted tests near existing call-site tests if lightweight.

## Task 1: Audit Model And Summary

**Files:**
- Modify: `forwin/governance.py`
- Modify: `forwin/review_engine/audit.py`
- Test: `tests/review_engine/test_legacy_compatibility_audit.py`

- [ ] **Step 1: Write failing tests**

Create `tests/review_engine/test_legacy_compatibility_audit.py` with:

```python
from __future__ import annotations

from forwin.governance import DecisionEventType, ensure_decision_event_type
from forwin.review_engine.audit import (
    LEGACY_COMPATIBILITY_REGISTRY,
    build_legacy_compatibility_payload,
    summarize_legacy_compatibility_audit,
)


def test_legacy_compatibility_event_type_is_registered() -> None:
    assert (
        ensure_decision_event_type(DecisionEventType.LEGACY_COMPATIBILITY_USED)
        == DecisionEventType.LEGACY_COMPATIBILITY_USED
    )


def test_legacy_compatibility_payload_records_facts_only() -> None:
    payload = build_legacy_compatibility_payload(
        compat_layer="book_state",
        compat_feature="book_state.state.location_fallback",
        usage_kind="read_fallback",
        source_module="forwin.book_state.runtime",
        usage_reason="state.location present",
        compat_key="state.location",
        legacy_identifier="旧城",
        canonical_identifier="",
        related_stage="compile_runtime",
        metadata={"field_path": "state.location"},
    )

    assert payload["compat_layer"] == "book_state"
    assert payload["compat_feature"] == "book_state.state.location_fallback"
    assert payload["usage_kind"] == "read_fallback"
    assert payload["source_module"] == "forwin.book_state.runtime"
    assert payload["usage_reason"] == "state.location present"
    assert "delete_candidate" not in payload
    assert "blocking_for_removal" not in payload


def test_legacy_compatibility_summary_assesses_usage_after_collection() -> None:
    summary = summarize_legacy_compatibility_audit(
        [
            {
                "payload": build_legacy_compatibility_payload(
                    compat_layer="book_state",
                    compat_feature="book_state.state.location_fallback",
                    usage_kind="read_fallback",
                    source_module="forwin.book_state.runtime",
                    usage_reason="state.location present",
                )
            },
            {
                "payload": build_legacy_compatibility_payload(
                    compat_layer="projection",
                    compat_feature="projection.legacy_world_model_projection",
                    usage_kind="projection_compat",
                    source_module="forwin.orchestrator_loop_core.world_projection",
                    usage_reason="projection compatibility path invoked",
                )
            },
        ],
        registry=LEGACY_COMPATIBILITY_REGISTRY,
    )

    assert summary["total_events"] == 2
    assert summary["by_layer"]["book_state"] == 1
    assert summary["by_feature"]["book_state.state.location_fallback"] == 1
    blockers = summary["removal_assessment"]["blocking_for_removal"]
    assert {
        "compat_feature": "book_state.state.location_fallback",
        "reason": "used during audit window",
        "events": 1,
    } in blockers
    assert "delete_candidates" in summary["removal_assessment"]
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
python3 -m pytest tests/review_engine/test_legacy_compatibility_audit.py -q
```

Expected: FAIL because `LEGACY_COMPATIBILITY_USED`, `build_legacy_compatibility_payload`, and `summarize_legacy_compatibility_audit` do not exist.

- [ ] **Step 3: Implement event type and audit helpers**

In `forwin/governance.py`, add:

```python
LEGACY_COMPATIBILITY_USED = "legacy_compatibility_used"
```

to `DecisionEventType`, and add it to `KNOWN_DECISION_EVENT_TYPES`.

In `forwin/review_engine/audit.py`, add:

```python
LEGACY_COMPATIBILITY_REGISTRY = {
    "book_state.state.location_fallback": {
        "compat_layer": "book_state",
        "default_assessment": "must_migrate_if_used",
        "description": "Fallback from BookState runtime location to legacy state.location.",
    },
    "book_state.state.location_patch_warning": {
        "compat_layer": "book_state",
        "default_assessment": "must_migrate_if_used",
        "description": "Legacy state.location patches downgraded to warnings.",
    },
    "projection.legacy_world_model_projection": {
        "compat_layer": "projection",
        "default_assessment": "must_migrate_if_used",
        "description": "Legacy world model projection compatibility path.",
    },
    "subworld.legacy_entity_id_bridge": {
        "compat_layer": "subworld",
        "default_assessment": "must_migrate_if_used",
        "description": "Bridge from SubWorld node metadata legacy_entity_id to canonical entity.",
    },
    "subworld.create_legacy_entity": {
        "compat_layer": "subworld",
        "default_assessment": "must_migrate_if_used",
        "description": "Create legacy entity rows for SubWorld compatibility.",
    },
    "governance.legacy_relaxed_fallback": {
        "compat_layer": "governance",
        "default_assessment": "candidate_if_unused",
        "description": "Project governance fallback to legacy_relaxed mode.",
    },
    "api.legacy_checkpoint_status": {
        "compat_layer": "api",
        "default_assessment": "candidate_if_unused",
        "description": "Normalize legacy checkpoint status strings in API responses.",
    },
    "migration.legacy_book_state_import": {
        "compat_layer": "migration",
        "default_assessment": "keep_for_import_only",
        "description": "Legacy BookState import/migration compatibility.",
    },
}
```

Then implement `build_legacy_compatibility_payload()` and `summarize_legacy_compatibility_audit()` so payloads contain only facts and summary produces `delete_candidates`, `blocking_for_removal`, `keep_for_import_only`, and `out_of_scope`.

- [ ] **Step 4: Run tests to verify GREEN**

Run:

```bash
python3 -m pytest tests/review_engine/test_legacy_compatibility_audit.py -q
```

Expected: PASS.

## Task 2: DecisionEvent Recording Helper

**Files:**
- Modify: `forwin/orchestrator_loop_core/governance.py`
- Modify: `forwin/orchestrator_loop_core/service.py`
- Test: `tests/review_engine/test_legacy_compatibility_audit.py`

- [ ] **Step 1: Add failing helper test**

Append:

```python
from forwin.governance import DecisionEventInfo
from forwin.orchestrator_loop_core.governance import _record_legacy_compatibility_event


def test_record_legacy_compatibility_event_writes_fact_event() -> None:
    class Recorder:
        _governance_task_id = ""
        _governance_root_event_id = ""

        def __init__(self) -> None:
            self.events: list[DecisionEventInfo] = []

        def _record_decision_event(self, **kwargs) -> None:
            self.events.append(
                DecisionEventInfo(
                    project_id=kwargs["project_id"],
                    chapter_number=kwargs["chapter_number"],
                    scope=kwargs["scope"],
                    event_family=kwargs["event_family"],
                    event_type=kwargs["event_type"],
                    summary=kwargs["summary"],
                    reason=kwargs["reason"],
                    payload=kwargs["payload"],
                )
            )

    recorder = Recorder()
    _record_legacy_compatibility_event(
        recorder,
        updater=object(),
        project_id="project-1",
        chapter_number=7,
        compat_layer="book_state",
        compat_feature="book_state.state.location_fallback",
        usage_kind="read_fallback",
        source_module="forwin.book_state.runtime",
        usage_reason="state.location present",
    )

    event = recorder.events[0]
    assert event.event_type == DecisionEventType.LEGACY_COMPATIBILITY_USED
    assert event.event_family == "runtime_observation"
    assert event.payload["compat_feature"] == "book_state.state.location_fallback"
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
python3 -m pytest tests/review_engine/test_legacy_compatibility_audit.py::test_record_legacy_compatibility_event_writes_fact_event -q
```

Expected: FAIL because `_record_legacy_compatibility_event` does not exist.

- [ ] **Step 3: Implement helper and attach to orchestrator**

In `forwin/orchestrator_loop_core/governance.py`, add helper that calls `build_legacy_compatibility_payload()` and `_record_decision_event()` with `event_family="runtime_observation"` and `event_type=DecisionEventType.LEGACY_COMPATIBILITY_USED`.

In `forwin/orchestrator_loop_core/service.py`, import and assign:

```python
WritingOrchestrator._record_legacy_compatibility_event = _record_legacy_compatibility_event
```

- [ ] **Step 4: Run helper test**

Run:

```bash
python3 -m pytest tests/review_engine/test_legacy_compatibility_audit.py::test_record_legacy_compatibility_event_writes_fact_event -q
```

Expected: PASS.

## Task 3: Script Summary Integration

**Files:**
- Modify: `scripts/audit_review_engine_cutover.py`
- Test: `tests/review_engine/test_legacy_compatibility_audit.py`

- [ ] **Step 1: Add failing script-level test for summary helper**

Append a test that calls `summarize_legacy_compatibility_audit()` with one observed event and asserts the JSON-compatible shape includes `legacy_compat.total_events`, `by_layer`, and `removal_assessment`.

- [ ] **Step 2: Run test to verify RED or current gap**

Run:

```bash
python3 -m pytest tests/review_engine/test_legacy_compatibility_audit.py -q
```

Expected: PASS for helper tests after Task 1, but script still lacks CLI flag.

- [ ] **Step 3: Implement CLI option**

In `scripts/audit_review_engine_cutover.py`:

```python
parser.add_argument("--include-legacy-compat", action="store_true")
```

When set, query `DecisionEventType.LEGACY_COMPATIBILITY_USED` for the same project and merge:

```python
summary["legacy_compat"] = summarize_legacy_compatibility_audit([...])
```

Do not change the exit code semantics for review safety-net failures. Legacy compatibility blockers should be present in output but should not by themselves make the review cutover audit fail in this pass.

- [ ] **Step 4: Verify CLI**

Run:

```bash
python3 scripts/audit_review_engine_cutover.py --help
```

Expected: help includes `--include-legacy-compat`.

## Task 4: First Instrumentation Pass

**Files:**
- Modify: `forwin/orchestrator_loop_core/governance.py`
- Modify: `forwin/orchestrator_loop_core/world_projection.py`
- Modify: `forwin/orchestrator_loop_core/finalization.py`
- Modify: `forwin/book_state/runtime.py`
- Modify: `forwin/book_state/reviewer.py`
- Modify: `forwin/subworld_manager.py`
- Modify tests only where local helper behavior is already covered.

- [ ] **Step 1: Record governance fallback**

In `_project_governance()`, after normalization, if the resulting mode is `legacy_relaxed`, call `_record_legacy_compatibility_event()` when a `Project` has an id and the helper is available. Use:

```text
compat_layer=governance
compat_feature=governance.legacy_relaxed_fallback
usage_kind=config_fallback
source_module=forwin.orchestrator_loop_core.governance
```

- [ ] **Step 2: Record legacy projection compatibility**

Before legacy projection paths in `world_projection.py` and `finalization.py`, call the helper with:

```text
compat_layer=projection
compat_feature=projection.legacy_world_model_projection
usage_kind=projection_compat
source_module=<current module>
```

- [ ] **Step 3: Expose BookState legacy location facts**

In `book_state.runtime`, keep behavior unchanged but expose a small helper or metadata return path used by orchestrator-side callers. Do not write `DecisionEvent` from pure BookState runtime if no updater/session is available.

In `book_state.reviewer`, expose fact metadata for `state.location` warning downgrade. Keep warning behavior unchanged.

- [ ] **Step 4: Record SubWorld legacy bridge facts where orchestrator has an updater**

Add an optional callback parameter to the relevant `SubWorldManager` path, and call it when `legacy_entity_id` is used as a bridge or `create_legacy_entity=True` is selected. Keep existing behavior unchanged.

- [ ] **Step 5: Run focused tests**

Run:

```bash
python3 -m pytest tests/review_engine/test_legacy_compatibility_audit.py tests/review_engine/test_audit.py -q
python3 -m pytest tests/test_repair_progress.py tests/test_runtime_container.py -q
```

Expected: PASS.

## Task 5: Docs And Final Verification

**Files:**
- Modify: `docs/designs/review-engine-cutover-spec.md`

- [ ] **Step 1: Document event-vs-summary boundary**

Add a short section stating:

- single `legacy_compatibility_used` events are fact-only;
- deletion candidates and blockers are summary conclusions;
- `--include-legacy-compat` adds a report to the 60-chapter audit.

- [ ] **Step 2: Run full verification**

Run:

```bash
python3 -m pytest tests/review_engine -q
python3 -m pytest tests/test_review_outcome_router.py tests/test_plan_patch_scope_router.py tests/test_repair_progress.py tests/test_runtime_container.py -q
python3 scripts/audit_review_engine_cutover.py --help
git diff --check
```

Expected:

- review engine tests pass;
- related router/repair/container tests pass;
- CLI help includes `--include-legacy-compat`;
- `git diff --check` has no output.

- [ ] **Step 3: Commit implementation**

```bash
git add forwin/governance.py forwin/review_engine/audit.py forwin/orchestrator_loop_core/governance.py forwin/orchestrator_loop_core/service.py forwin/orchestrator_loop_core/world_projection.py forwin/orchestrator_loop_core/finalization.py forwin/book_state/runtime.py forwin/book_state/reviewer.py forwin/subworld_manager.py scripts/audit_review_engine_cutover.py tests/review_engine/test_legacy_compatibility_audit.py docs/designs/review-engine-cutover-spec.md
git commit -m "feat: audit legacy compatibility usage"
```

## Self-Review

- Spec coverage: event model, registry, script output, summary-only deletion conclusions, first instrumentation scope, and tests are covered.
- Placeholder scan: no TBD/TODO placeholders are present.
- Type consistency: payload keys match the design spec and script summary uses `legacy_compat` as the top-level compatibility report key.
