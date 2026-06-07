# Canon Invariant State Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lift countdown drift handling into a generic canon invariant state audit path while preserving current countdown compatibility and unblocking strong-state repair routing.

**Architecture:** Add a small `CanonInvariant` model layer and project existing countdown and active-rule state through `BookStateQueryInterface`. Introduce a generic ledger-state drift selector, plan patch path, and prompt context while keeping legacy countdown signals and fields during migration.

**Tech Stack:** Python 3.13, Pydantic, SQLAlchemy, pytest, existing ForWin BookState, canon-quality, planning, review-engine, and writer modules.

---

## Constraints

- Do not create a new invariant ledger table.
- Do not delete `CountdownLedgerRow` or break countdown compatibility tests.
- Do not merge `narrative_obligations` into invariant state.
- Do not add project-specific countdown branches for `city_renovation_deadline`.
- Do not approve chapter 26 manually to bypass the gate.
- Use TDD for behavior changes: write failing tests, verify red, implement, verify green.

## Task 1: CanonInvariant Model And BookState Projection

**Files:**
- Create `forwin/canon_quality/invariants.py`
- Modify `forwin/book_state/query_interface.py`
- Extend `tests/test_bookstate_query_interface.py`

- [x] Write failing tests proving countdown rows project to `CanonInvariant(kind="monotonic_numeric", value_unit="minutes")` while the old countdown view still works.
- [x] Write a failing test proving active rules with an `invariant` payload project through `get_current_invariants()`.
- [x] Run `.venv/bin/python -m pytest tests/test_bookstate_query_interface.py -q` and confirm the failures are caused by missing invariant support.
- [x] Implement `CanonInvariant`, `InvariantDriftTarget`, `invariant_from_countdown_state()`, `invariant_from_active_rule()`, and `legacy_countdown_key_for_invariant()`.
- [x] Add `BookStateQueryInterface.get_current_invariants()` and `InvariantStateSnapshot.invariants`.
- [x] Re-run the focused test until green.
- [x] Commit: `feat: project canon invariants through book state query`.

## Task 2: ActiveRuleStore Temporal Semantics

**Files:**
- Modify `forwin/canon_quality/active_rule_store.py`
- Extend `tests/test_active_rule_store.py`

- [x] Write failing tests for as-of reads before and after a revoke.
- [x] Write a failing test proving `valid_until_chapter` hides expired rules.
- [x] Write a failing or characterization test for duplicate registration after a non-overlapping revoke.
- [x] Run `.venv/bin/python -m pytest tests/test_active_rule_store.py -q` and confirm the historical revoke case fails for the current mutable-row behavior.
- [x] Change revocation to append an `active_rule_revoked` signal or otherwise preserve historical reads.
- [x] Update `query_active_as_of()` to honor valid-from, valid-until, and revocation chapters.
- [x] Keep current `query_active()` behavior compatible with existing callers.
- [x] Re-run active-rule tests until green.
- [x] Commit: `fix: preserve active rule as-of history`.

## Task 3: Generic Ledger-State Drift Selector And Plan Patch

**Files:**
- Create `forwin/planning/ledger_state_drift_pre_audit.py`
- Modify `forwin/planning/countdown_drift_pre_audit.py`
- Modify `forwin/planning/future_plan_audit/obligations.py`
- Add `tests/test_invariant_drift_pre_audit.py`
- Extend `tests/test_plan_patcher_loop_closure.py`

- [x] Write failing tests for `form_invariant_drift` producing an `InvariantDriftTarget`.
- [x] Write failing tests for legacy `form_countdown_inconsistency` adapting into a generic monotonic-minutes drift target.
- [x] Write a failing test proving a deadline target does not use countdown-specific wording.
- [x] Write or extend a failing plan-patcher test proving `ledger_state_drift_pre_write` patches use `suppression_key="invariant:<key>"`.
- [x] Run focused tests and confirm red.
- [x] Implement `select_ledger_state_drift_targets()` and keep `select_countdown_drift_targets()` as a compatibility wrapper.
- [x] Add a generic pre-write patch path that dedupes legacy countdown signals by `source_signal_id`.
- [x] Re-run focused tests until green.
- [x] Commit: `feat: add generic ledger state drift pre-audit`.

## Task 4: Signal Payloads, Gate, And Review Routing

**Files:**
- Modify `forwin/canon_quality/chapter_review_form/canon_projector.py`
- Modify `forwin/canon_quality/signals.py`
- Modify `forwin/canon_quality/gate.py`
- Modify `forwin/reviewer/repair_scope_router.py`
- Modify `forwin/review_engine/issue_taxonomy.py`
- Modify `forwin/review_engine/rules/review_outcome.py` as needed
- Extend review-engine and plan-patcher tests

- [x] Write failing tests proving countdown form drift payloads contain both legacy fields and generic invariant fields.
- [x] Write failing tests proving `form_invariant_drift` is a blocking canon signal when evidence supports an error.
- [x] Write a failing review-outcome test for `form_countdown_inconsistency`/`form_invariant_drift` so the issue no longer falls to the "no automatic route" system block.
- [x] Implement generic signal kind and gate classification.
- [x] Enrich legacy countdown drift payloads with `invariant_key`, `invariant_kind`, `expected`, `observed`, `allowed_bridges`, `generic_patch_kind`, and `generic_suppression_key`.
- [x] Route by payload/evidence instead of blindly choosing active-rule repair. For the current ChapterReviewForm drift profile, default to rewrite or plan patch when the contradiction is in the current body or stale plan context, and keep operator review for ambiguous profiles.
- [x] Re-run focused tests until green.
- [x] Commit: `feat: route invariant drift through review repair`.

## Task 5: Context And Prompt Rendering

**Files:**
- Modify `forwin/context/assembler_core/canon_quality_context.py`
- Modify `forwin/writer/prompt_core/constraints.py`
- Extend prompt/context regression tests

- [x] Write failing tests proving `invariant_constraints` appears in canon quality context while legacy `countdown_constraints` remains.
- [x] Write failing prompt tests for monotonic-minutes countdown invariants, deadline invariants, and generic suppression keys.
- [x] Implement context assembly using `BookStateQueryInterface.get_current_invariants()` or an equivalent compatibility projection through existing query boundaries.
- [x] Render invariant constraints in writer prompts:
  - `monotonic_numeric` minutes as upper-bound / no-increase-without-bridge guidance;
  - `deadline` as deadline bridge guidance;
  - `state_transition` and `active_rule` as latest-state / revocation-boundary guidance when available.
- [x] Keep legacy countdown suppression keys supported during migration.
- [x] Re-run focused tests until green.
- [x] Commit: `feat: render canon invariant constraints`.

## Task 6: Verification And Runtime Recovery

**Files / Tools:**
- Local pytest suite
- Docker Compose services
- ForWin MCP tools for project/task/chapter truth

- [ ] Run focused suite:
  - `.venv/bin/python -m pytest tests/test_book_state_query_interface.py tests/test_active_rule_store.py tests/test_invariant_drift_pre_audit.py tests/test_plan_patcher_loop_closure.py -q`
  - plus the touched review-engine and prompt/context tests.
- [ ] Run broader risk suite around canon repair and pulp pipeline if focused tests pass.
- [ ] Check `git status --short --branch`.
- [ ] Rebuild ForWin services that need code changes.
- [ ] Use ForWin MCP state tools to confirm project `8ac86975f5a345abb9c781e7246b48b9` has no active generation task before mutating.
- [ ] Retry or continue chapter 26 through the normal review/rewrite path, not manual approval.
- [ ] Monitor until either the run resumes cleanly, reaches another actionable gate, or reaches 60 accepted chapters.
- [x] Runtime discovery: chapter 27 repair verification built a 304k-char LLM prompt and timed out. Added bounded repair-verification prompt serialization plus regression coverage so full review/draft payloads no longer leak into verifier input.
- [ ] If all 60 chapters are accepted and no active/failing gate remains, mark the active goal complete.
