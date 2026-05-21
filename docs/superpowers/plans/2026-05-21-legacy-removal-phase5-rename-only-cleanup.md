# Legacy Removal Phase 5 Rename-Only Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove misleading legacy names from current Genesis and review-engine paths.

**Architecture:** Rename current behavior in place, update tests to assert current names, then mark the rename-only inventory entries deleted.

**Tech Stack:** Python 3.12, pytest, legacy inventory audit.

---

## Scope

Implements `docs/superpowers/specs/2026-05-21-legacy-removal-phase5-rename-only-cleanup-design.md`.

Do not remove Phase 6 external compatibility in this phase: route dependency
flat kwargs, config aliases, publisher plaintext upgrade, model/protocol
aliases, UI labels, and migration scripts remain for Phase 6.

## File Structure

- `forwin/genesis_handoff/chapter_materializer.py`: rename lazy `legacy` facade helper.
- `forwin/genesis_workspace/fallbacks.py`: rename lazy `legacy` facade helper.
- `forwin/genesis_workspace/name_suggestions.py`: rename lazy `legacy` facade helper.
- `forwin/genesis_workspace/service.py`: rename lazy `legacy` facade helper.
- `forwin/review_engine/audit.py`: rename event payload fields and live cutover summary keys.
- `forwin/orchestrator_loop_core/governance.py`: pass renamed event payload fields.
- `forwin/orchestrator_loop_core/quality_gates.py`: rename outcome mapping and event kwargs.
- `forwin/orchestrator_loop_core/repair_loop.py`: read `final_gate_decision` and pass renamed event kwargs.
- `forwin/orchestrator_loop_core/common.py`: rename checkpoint hint `legacy_severity` to `severity`.
- `forwin/review_engine/rules/final_acceptance.py`: rename rule id and sub_action key.
- `forwin/review_engine/rules/review_outcome.py`: remove `legacy_action` fallback.
- `docs/designs/legacy-inventory.yaml`: mark the two Phase 5 entries deleted.
- Tests:
  - `tests/review_engine/test_audit.py`
  - `tests/review_engine/test_shadow_mode.py`
  - `tests/review_engine/test_rule_parity.py`
  - `tests/review_engine/test_obligation_scope.py`
  - `tests/test_production_scheduler.py`

## Task 1: Write Failing Tests

- [ ] **Step 1: Update audit payload tests**

In `tests/review_engine/test_audit.py`, call:

```python
payload = build_decision_event_payload(
    decision=Decision("manual_review", "needs human", "rule-1", ["deadline"], "router", {}),
    input_digest="abc123",
    shadow_mismatch=True,
    live_or_shadow="live",
    baseline_outcome="manual_review",
    engine_outcome="auto_approve",
    live_source="engine",
    shadow_source="baseline",
    engine_live=True,
    baseline_shadow_evaluated=True,
    baseline_safety_net_used=False,
    severe_shadow_mismatch=True,
)
```

Assert the payload contains `baseline_*` keys and not old `legacy_*` keys.

- [ ] **Step 2: Update cutover summary tests**

In the same file, replace payload key `legacy_safety_net_used` with
`baseline_safety_net_used` and assert:

```python
assert summary["baseline_safety_net_chapters"] == [2]
```

- [ ] **Step 3: Update review action tests**

In `tests/review_engine/test_shadow_mode.py`, use
`{"review_action": "manual"}` instead of `{"legacy_action": "manual"}`.

In parity/scope tests, assert old `legacy_action` is absent and
`review_action` is the only action key when present.

- [ ] **Step 4: Update production scheduler test name**

Rename `test_scheduler_runs_due_projects_and_preserves_legacy_actions` to a
current name such as `test_scheduler_runs_due_projects_and_preserves_actions`.

- [ ] **Step 5: Run red tests**

Run:

```bash
python3 -m pytest tests/review_engine/test_audit.py tests/review_engine/test_shadow_mode.py tests/test_production_scheduler.py -q
```

Expected before implementation: failures for unexpected keyword arguments or
missing `baseline_*` payload keys.

## Task 2: Rename Genesis Facade Helpers

- [ ] **Step 1: Rename helper functions**

In all Genesis target files, replace:

```python
def _legacy():
    from forwin import book_genesis as legacy
    return legacy
```

with:

```python
def _book_genesis():
    from forwin import book_genesis
    return book_genesis
```

- [ ] **Step 2: Rename local variables**

Replace `legacy = _legacy()` with `book_genesis = _book_genesis()` and update
all `legacy.` calls to `book_genesis.`.

- [ ] **Step 3: Run Genesis tests**

Run:

```bash
python3 -m pytest tests/test_book_genesis_flow.py tests/test_genesis_workspace_service.py tests/test_mcp_server.py -q
```

Expected: pass.

## Task 3: Rename Review Telemetry

- [ ] **Step 1: Update `forwin/review_engine/audit.py`**

Rename parameters and payload keys:

```text
legacy_outcome -> baseline_outcome
legacy_shadow_evaluated -> baseline_shadow_evaluated
legacy_safety_net_used -> baseline_safety_net_used
legacy_safety_net_chapters -> baseline_safety_net_chapters
_uses_legacy_safety_net -> _uses_baseline_safety_net
```

- [ ] **Step 2: Update orchestrator callers**

Use the renamed kwargs in governance, quality gates, and repair loop.

- [ ] **Step 3: Rename rule/action keys**

In final acceptance:

```text
legacy_final_acceptance_gate -> final_acceptance_gate
legacy_decision -> final_gate_decision
```

In review outcome, remove the `legacy_action` lookup.

In common checkpoint hints:

```text
legacy_severity -> severity
```

- [ ] **Step 4: Run review tests**

Run:

```bash
python3 -m pytest tests/review_engine/test_audit.py tests/review_engine/test_shadow_mode.py tests/review_engine/test_rule_parity.py tests/review_engine/test_obligation_scope.py tests/test_production_scheduler.py -q
```

Expected: pass.

## Task 4: Update Inventory And Residual Gate

- [ ] **Step 1: Mark entries deleted**

In `docs/designs/legacy-inventory.yaml`, change both Phase 5 entries to:

```yaml
category: deleted
removal_phase: complete
status: deleted
```

- [ ] **Step 2: Run inventory and residual grep**

Run:

```bash
python3 scripts/audit_legacy_inventory.py --check --strict-patterns
git grep -n -E 'def _legacy|legacy = _legacy|_legacy\\(|from forwin import book_genesis as legacy|legacy\\.|legacy_outcome|legacy_shadow_evaluated|legacy_safety_net_used|legacy_safety_net_chapters|legacy_action|legacy_decision|legacy_severity|legacy_final_acceptance_gate|_ENGINE_OUTCOME_TO_LEGACY_REVIEW_ACTION' -- forwin scripts ':!forwin/migrations/versions'
```

Expected: audit passes and grep returns no production hits.

## Task 5: Verification And Commit

- [ ] **Step 1: Run final verification**

Run:

```bash
python3 -m pytest tests/test_book_genesis_flow.py tests/test_genesis_workspace_service.py tests/test_mcp_server.py -q
python3 -m pytest tests/review_engine/test_audit.py tests/review_engine/test_shadow_mode.py tests/review_engine/test_rule_parity.py tests/review_engine/test_obligation_scope.py tests/test_production_scheduler.py tests/test_architecture_boundaries.py -q
python3 scripts/audit_legacy_inventory.py --check --strict-patterns
python3 -m compileall -q forwin
git diff --check
```

- [ ] **Step 2: Commit Phase 5 only**

Do not stage unrelated dirty LLM/minimax files.

```bash
git commit -m "refactor: rename legacy-labeled current paths"
```
