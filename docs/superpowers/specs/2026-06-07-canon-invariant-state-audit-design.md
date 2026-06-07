# Canon Invariant State Audit Design

## Context

ForWin currently has several strong-state audit mechanisms, but they are split by
state kind:

- `BookState` is the canon runtime and `BookStateQueryInterface` is the stable
  repair-time read boundary.
- `ChapterReviewForm` audits character state, countdowns, obligations, open
  signals, and final-chapter closure through structured LLM answers plus quote
  validation.
- `FuturePlanAudit` deterministically patches stale countdown and custody-state
  plan contracts, and separately promotes form-derived obligation/open-signal
  issues into plan patches.
- `ActiveRuleStore` persists active rule registration through
  `CanonQualitySignalRow`, but its current temporal semantics are closer to a
  mutable current-state table than an append-only as-of event history.

Countdown is the most complete strong-state path today: it has compatibility
rows, query helpers, context fields, writer prompt rendering, future-plan
parsing, form drift projection, gate types, and repair routing. That coverage is
useful, but it makes unrelated strong states look like countdowns. A deadline
such as `city_renovation_deadline` should not need to masquerade as a timer to
receive canon protection.

## Decision

Introduce a small `CanonInvariant` abstraction as the shared representation for
cross-chapter strong state. Countdown becomes a `monotonic_numeric` invariant
profile with `value_unit="minutes"`, not the root concept.

This is a compatibility migration:

- Do not create a new ledger table.
- Do not remove `CountdownLedgerRow` or existing countdown tests in the first
  phase.
- Do not merge `narrative_obligations` into invariants. Obligations remain
  payoff/debt contracts, not state facts.
- Do not add more countdown-specific branches for project-specific mechanisms.

## Model

Add `forwin/canon_quality/invariants.py` with Pydantic models:

- `CanonInvariant`
  - `invariant_key`
  - `kind`: `monotonic_numeric`, `deadline`, `terminal_state`,
    `state_transition`, `set_count`, `active_rule`, or `custom`
  - `subject_key`
  - `label`
  - `current_value`
  - `value_unit`
  - `status`
  - `valid_from_chapter`
  - `valid_until_chapter`
  - `last_updated_chapter`
  - `constraints`
  - `allowed_bridges`
  - `evidence_refs`
  - `source`
  - `payload`
- `InvariantDriftTarget`
  - normalized pre-write target consumed by `FuturePlanAudit`
- helper adapters:
  - `invariant_from_countdown_state`
  - `invariant_from_active_rule`
  - `legacy_countdown_key_for_invariant`

The first phase keeps signal compatibility:

- Legacy signal: `form_countdown_inconsistency`
- New generic signal: `form_invariant_drift`
- Generic patch kind: `ledger_state_drift`
- Legacy patch kind remains `countdown_drift` only for existing callers/tests.

## Query Boundary

Extend `BookStateQueryInterface`:

- Add `get_current_invariants(project_id, as_of_chapter)`.
- Add `invariants: dict[str, CanonInvariant]` to `InvariantStateSnapshot`.
- Keep `get_current_countdown_values()` and `countdowns` as compatibility views.

`SqlBookStateQueryInterface.get_current_invariants()` projects:

- countdown compatibility rows into `CanonInvariant(kind="monotonic_numeric",
  value_unit="minutes")`;
- active rules from `ActiveRuleStore` into `CanonInvariant(kind="active_rule")`.

Future additions such as deadline, terminal-state, artifact-count, reveal-state,
or custody-state invariants should enter through this method rather than direct
table reads.

## Active Rule Semantics

Fix `ActiveRuleStore` before relying on it for more generic state:

- `revoke_rule()` should preserve historical as-of reads. It may append an
  `active_rule_revoked` signal row or otherwise ensure `query_active_as_of()` is
  correct for chapters before and after revocation.
- `query_active_as_of()` must honor:
  - `valid_from_chapter <= as_of_chapter`
  - `valid_until_chapter is None or valid_until_chapter >= as_of_chapter`
  - no revocation at or before `as_of_chapter`
- Duplicate registration conflicts should only reject overlapping active
  intervals, not historical non-overlapping rules.

This still uses `CanonQualitySignalRow`; no new active-rule table is introduced.

## Chapter Review Form

Phase 1 does not add a new `invariants` form section. Instead:

- Keep the countdown section.
- When `canon_projector` emits `form_countdown_inconsistency`, enrich payload
  with generic fields:
  - `invariant_key`
  - `invariant_kind`
  - `expected`
  - `observed`
  - `allowed_bridges`
  - `generic_patch_kind="ledger_state_drift"`
  - `generic_suppression_key="invariant:<invariant_key>"`
- Preserve existing `patch_kind="countdown_drift"` and
  `suppression_key="countdown:<key>"` for compatibility.

Phase 2 may add a generic `invariants` section to the form and downgrade the
countdown section to a domain-specific compatibility profile.

## Future Plan Audit

Add `forwin/planning/ledger_state_drift_pre_audit.py`.

`select_ledger_state_drift_targets()` consumes:

- `form_invariant_drift`;
- legacy `form_countdown_inconsistency` with generic payload fields;
- legacy `form_countdown_inconsistency` without generic fields, by adapting it
  to `monotonic_numeric/minutes`.

`select_countdown_drift_targets()` remains as a thin compatibility wrapper.

`FuturePlanObligationMixin._audit_pre_write_obligations_and_signals()` should
consume generic ledger-state drift targets before the legacy countdown wrapper.
The resulting `NarrativePlanPatch` uses:

- `patch_type="ledger_state_drift_pre_write"` for generic targets;
- `writer_context_injections.type="ledger_state_drift_resolution"`;
- `suppression_key="invariant:<key>"`.

Existing countdown drift behavior remains available for legacy tests and prompts.

## Context And Prompt Rendering

Extend `canon_quality_context` with `invariant_constraints`, while keeping
`countdown_constraints` and `character_state_constraints` for compatibility.

Add invariant rendering in `writer/prompt_core/constraints.py`:

- `monotonic_numeric/minutes`: render the existing countdown upper-bound rules.
- `deadline`: render stale-deadline and allowed-bridge rules.
- `state_transition`: render latest-state and bridge-required rules.
- `active_rule`: render rule boundary, cost, and revocation conditions when
  available.

The renderer should suppress constraints using generic `invariant:<key>`
suppression keys. Countdown-specific suppression keys remain supported during
the migration.

## Gate And Repair Routing

Add `SignalKind.form_invariant_drift` and allow it to block canon admission when
severity is `error` and evidence is present.

Do not route all invariant drift to one repair scope. Determine repair scope by
payload and evidence:

- draft/chapter rewrite when accepted canon is contradicted in the current body;
- chapter-plan patch when stale plan context caused the drift;
- active-rule repair when accepted prior evidence exists but the active rule was
  not registered;
- operator review when evidence is ambiguous or the rule profile is unknown.

Legacy `form_countdown_inconsistency` remains accepted, but should be treated as
a countdown-profile alias for `form_invariant_drift`.

## Chapter 26 Handling

The current chapter 26 block should not be fixed by lowering gate severity or by
adding a `city_renovation_deadline` countdown branch.

Under this design:

- if the chapter body contradicts accepted deadline state without an explicit
  bridge, regenerate/rewrite the chapter;
- if the plan supplied stale deadline state, apply a plan patch then rewrite;
- if accepted prior text already established a deadline rule that was not
  registered, register it through active-rule repair with a prior accepted quote;
- if the form misclassified a deadline as a countdown, route to operator review
  and migrate the state to a deadline invariant profile.

## Tests

Add or extend focused tests:

- `tests/test_book_state_query_interface.py`
  - countdown compatibility rows project to invariants;
  - active rules project to invariants;
  - countdown compatibility view still returns the old shape.
- `tests/test_active_rule_store.py`
  - as-of query before revoke still returns the rule;
  - as-of query after revoke does not;
  - `valid_until_chapter` is honored;
  - non-overlapping historical rules do not conflict.
- `tests/test_invariant_drift_pre_audit.py`
  - generic `form_invariant_drift` creates a ledger-state drift target;
  - legacy countdown signal adapts to generic target;
  - deadline target does not use countdown wording.
- `tests/test_plan_patcher_loop_closure.py`
  - generic invariant drift creates a plan patch;
  - suppression key can be `invariant:<key>`;
  - legacy countdown cases still pass.
- prompt regression tests
  - countdown invariant renders existing minutes upper-bound text;
  - deadline invariant renders deadline bridge text;
  - suppression works for both generic and legacy keys.
- repair scope tests
  - `form_invariant_drift` does not default to active-rules blindly;
  - payload-driven routing distinguishes plan drift, body drift, rule missing,
    and operator ambiguity.

## Non-Goals

- No new `canon_invariant_ledgers` table.
- No migration that deletes `CountdownLedgerRow`.
- No project-specific countdown branches for `city_renovation_deadline`.
- No conversion of narrative obligations into invariant state.
- No prompt-only fix that bypasses plan patches and canon gate.
- No gate suppression that hides strong-state drift.

## Rollout

1. Add invariant models and query projection.
2. Fix active-rule temporal semantics.
3. Add generic drift selector and plan patch path.
4. Enrich existing countdown signals with generic payload.
5. Add generic context/prompt rendering while preserving legacy fields.
6. Add generic gate/router signal handling.
7. Re-check chapter 26 and choose rewrite, plan patch, active-rule repair, or
   operator review based on evidence.
