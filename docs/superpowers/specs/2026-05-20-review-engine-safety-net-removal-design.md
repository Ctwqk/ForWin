# Review Engine Safety-Net Removal Design

## Context

The review engine has already completed a 60-chapter live pilot with engine live
for every chapter, no legacy safety-net chapters, and no severe shadow
mismatches. The next step is to remove the runtime safety net that can still
make legacy dispatchers the live decision source.

This design covers review cutover safety-net removal only. It does not remove
non-review legacy compatibility paths that still emitted runtime audit events,
such as subworld legacy entity bridges or legacy world-model projection.

## Goals

- Make the review engine the only live decision source.
- Remove runtime fallback to legacy review dispatchers.
- Remove legacy dispatchers that have engine-native replacements.
- Keep deployment compatibility for one release by deprecating, not deleting,
  the old cutover configuration fields.
- Preserve audit evidence that every chapter is engine-live and no legacy
  safety net was used.

## Non-Goals

- No deletion of non-review `legacy_compatibility_used` features.
- No deletion of `FinalAcceptanceGate` as a callable helper used by engine
  rules.
- No runtime fallback back to legacy dispatchers after this removal.
- No config cleanup in this pass beyond deprecation warnings.

## Runtime Boundary

Review engine decisions become the only live runtime path.

`quality_gates.py` should call
`AutoDecisionEngine(build_review_outcome_rules()).decide(...)` directly for
review-outcome decisions. It should not build a `ReviewOutcomeRouter` result,
convert it to a legacy decision, or pass both decisions through
`select_cutover_pair()`.

`repair_loop.py` should call `decide_repair_v2(...)` directly for repair scope
decisions. It should not call `RepairPolicy.decide()` as live path, fallback, or
shadow source.

Obligation scope decisions should use the engine-native obligation rules. The
`ObligationScopeRouter` class and direct call sites should be removed.

`FinalAcceptanceGate` remains in the codebase as an internal callable used by
`review_engine.rules.final_acceptance`. Orchestrator/runtime code should not
inject or call it as an outer legacy gate.

## Audit Semantics

`REVIEW_ENGINE_DECISION` events remain required. The payload should keep the
existing cutover fields so audit tooling and dashboards can prove the engine is
live:

- `live_or_shadow`
- `live_source`
- `engine_live`
- `legacy_safety_net_used`
- `shadow_mismatch`
- `severe_shadow_mismatch`

After removal, the review safety-net fields have fixed meaning:

- `live_source` is always `engine`.
- `engine_live` is always `true`.
- `legacy_safety_net_used` is always `false`.
- Legacy reverse-shadow is no longer required and should not be treated as a
  pass condition.

Audit failure remains strict: any chapter with `legacy_safety_net_used=true`,
`live_source!=engine`, or `engine_live=false` fails the review cutover audit.

## Deprecated Cutover Config

The following config fields remain for one release but no longer change runtime
behavior:

- `review_engine_live_cutover_enabled`
- `review_engine_live_cutover_project_allowlist`

If either field is set, configuration loading should emit a deprecation warning
explaining that the review engine is globally live and the fields are ignored.
The next cleanup pass may delete the fields and remove matching container env
entries after deployment config has been updated.

## Removal Targets

The implementation should remove these review safety-net targets:

- `forwin/reviewer/outcome.py:ReviewOutcomeRouter`
- `forwin/reviser/policy.py:RepairPolicy.decide()`
- `forwin/planning/obligation_scope_router.py:ObligationScopeRouter`
- `forwin/review_engine/cutover.py:select_cutover_pair()` legacy/live
  selection semantics
- Runtime-container construction or injection paths for legacy dispatcher
  objects
- Tests that only validate the deleted legacy dispatchers

The implementation should retain or replace coverage with engine-native tests
before deleting legacy tests.

## Explicit Exclusions

Do not remove these in this pass:

- `subworld.legacy_entity_id_bridge`
- `projection.legacy_world_model_projection`
- `characters.create_legacy_entity_default_true`
- `subworld.create_legacy_entity`
- `legacy_compatibility_used` event recording and summary logic
- `FinalAcceptanceGate` as an engine-rule helper

Those paths need separate migration or compatibility-removal designs because
the 60-chapter audit still showed runtime usage for several of them.

## Testing Strategy

Boundary tests should assert that orchestrator/runtime modules no longer import
or call `ReviewOutcomeRouter`, `RepairPolicy`, `ObligationScopeRouter`, or
`select_cutover_pair`. The same boundary tests should allow
`review_engine.rules.final_acceptance` to import `FinalAcceptanceGate`.

Engine rule tests should cover the behavior formerly protected by:

- `ReviewOutcomeRouter` tests
- `RepairPolicy` tests
- `ObligationScopeRouter` tests

Audit tests should verify that a complete 60-chapter engine-live event stream
passes without legacy shadow, and that any `legacy_safety_net_used=true`,
`live_source=legacy`, or `engine_live=false` still fails.

Container verification after implementation should run a 60-chapter project or
continue an existing clean 60-chapter pilot and then run:

```bash
python3 scripts/audit_review_engine_cutover.py \
  --project-id <project_id> \
  --expected-chapters 60 \
  --include-legacy-compat
```

Expected review cutover result:

- `engine_live_chapters=60`
- `legacy_safety_net_chapters=[]`
- `severe_mismatch_chapters=[]`
- `passed=true`

Legacy compatibility blockers may still appear in the separate
`legacy_compat` section and do not invalidate this review safety-net removal.

## Rollback Strategy

Rollback is deployment-level, not runtime-level. After this change, the
application should not contain an automatic legacy dispatcher fallback. If the
engine-only path fails in production, rollback means reverting to the previous
image or commit.

This is intentional: keeping an in-process safety net would preserve the same
dual-dispatch ambiguity this removal is meant to eliminate.
