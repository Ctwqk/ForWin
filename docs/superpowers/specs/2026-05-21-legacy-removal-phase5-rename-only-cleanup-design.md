# Legacy Removal Phase 5 Rename-Only Cleanup Design

## Context

Phase 5 removes misleading legacy names from behavior that is still current.
This phase does not delete external compatibility shims; those remain Phase 6.

Targets:

- `genesis.book_genesis_facade_names`
- `review_engine.legacy_telemetry_names`

The user has pre-authorized implementation after this spec and its plan are
written. No approval pause is required.

## Selected Approach

Rename current behavior, do not add compatibility aliases.

1. Genesis workspace facades import the current `forwin.book_genesis` facade as
   `book_genesis`, through `_book_genesis()` helpers where lazy import is still
   needed.
2. Review-engine cutover telemetry uses neutral baseline names:
   - `baseline_outcome`
   - `baseline_shadow_evaluated`
   - `baseline_safety_net_used`
   - `baseline_safety_net_chapters`
3. Review actions use `review_action` only. The old `legacy_action` fallback is
   removed.
4. Final acceptance rules use `final_acceptance_gate` and
   `final_gate_decision`, not `legacy_final_acceptance_gate` or
   `legacy_decision`.
5. Band checkpoint issue hints expose `severity`, not `legacy_severity`.

## Runtime Design

`build_decision_event_payload()` changes its public keyword arguments and
payload keys from legacy names to baseline names. Callers in
`orchestrator_loop_core` pass the new arguments.

`summarize_live_cutover_audit()` reads `baseline_safety_net_used` and returns
`baseline_safety_net_chapters`. It does not read old payload keys.

`review_action_from_decision()` checks `sub_action["review_action"]` and then
falls back to the outcome mapping. It does not check `legacy_action`.

`_final_gate_from_engine_decision()` reads `sub_action["final_gate_decision"]`.

## Inventory

After code and tests are renamed, mark these entries deleted:

- `genesis.book_genesis_facade_names`
- `review_engine.legacy_telemetry_names`

Their allow patterns remain the old strings only, so the deleted-entry gate
blocks reintroduction.

## Testing

Required verification:

```bash
python3 -m pytest tests/test_book_genesis_flow.py tests/test_genesis_workspace_service.py tests/test_mcp_server.py -q
python3 -m pytest tests/review_engine/test_audit.py tests/review_engine/test_shadow_mode.py tests/review_engine/test_rule_parity.py tests/review_engine/test_obligation_scope.py -q
python3 -m pytest tests/test_production_scheduler.py tests/test_architecture_boundaries.py -q
python3 scripts/audit_legacy_inventory.py --check --strict-patterns
git grep -n -E 'def _legacy\\(\\):|legacy = _legacy|_legacy\\(\\)\\.|from forwin import book_genesis as legacy|legacy\\._|legacy\\.GENESIS_STAGE_ORDER|legacy_outcome|legacy_shadow_evaluated|legacy_safety_net_used|legacy_safety_net_chapters|legacy_action|legacy_decision|legacy_severity|legacy_final_acceptance_gate|_ENGINE_OUTCOME_TO_LEGACY_REVIEW_ACTION' -- forwin/genesis_handoff forwin/genesis_workspace forwin/orchestrator_loop_core forwin/review_engine
python3 -m compileall -q forwin
git diff --check
```

The grep must return no production hits.

## Rollback

Rollback is a git revert of the Phase 5 implementation commit. There is no
runtime flag because this phase only renames current behavior.
