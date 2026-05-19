# Review Engine Arc and Book Patch Outcomes Design

Date: 2026-05-19

Status: draft for user review

## Scope

This spec makes `arc_patch` and `book_patch` executable outcomes. It turns structural plan debt into auditable plan patches instead of routing those cases to manual review by default.

## Goals

- Add arc and book plan patchers with interfaces aligned to existing chapter and band patchers.
- Validate structural patches before they can unblock acceptance.
- Inject active arc/book patch debt into future writer context.
- Add completion gates that prevent unresolved arc/book debt from silently escaping.
- Register outcomes through `AutoDecisionEngine`.

## Non-Goals

- Do not change BookState canon ownership.
- Do not auto-waive obligations.
- Do not use an LLM supervisor to decide patch validity.
- Do not delete chapter or band patchers.

## New Components

```text
forwin/planning/arc_plan_patcher.py
forwin/planning/book_plan_patcher.py
forwin/planning/arc_patch_validator.py
forwin/planning/book_patch_validator.py
```

Patchers accept:

- project id
- triggering chapter
- issue facts
- source obligation ids
- source signal ids
- target arc or book scope
- must-preserve constraints

Patchers return a `NarrativePlanPatch` compatible with existing obligation and admission flows.

## Engine Outcomes

New live outcomes:

- `arc_patch`
- `book_patch`

Decision sub-action includes:

- target scope
- target arc id when available
- patch type
- source signal ids
- source obligation ids

If a patcher flag is off, engine returns `manual_review` with rule id `arc_patcher_disabled` or `book_patcher_disabled`. This makes policy-disabled manual review explicit.

## Validation

Validators must check:

- patch does not contradict accepted canon
- patch has source evidence
- patch has a deadline or completion condition
- patch identifies affected future chapters or arc/book scope
- patch does not mutate already accepted chapter plans directly
- patch includes writer and reviewer context injection

Warnings do not block. Missing required evidence blocks.

## Future Context Injection

Writer context should include active structural debt:

- active arc patch debt for the current arc
- active book patch debt that applies project-wide
- payoff tests and deadlines
- source chapter references

This context is operational, not explanatory UI text.

## Completion Gates

Arc completion gate:

- At the final chapter of an arc, unresolved active arc patch debt blocks with `system_block`.
- Resolved debt passes.
- Waived debt requires human actor and reason.

Book completion gate:

- At final project chapters, unresolved P0/P1 book patch debt blocks.
- Lower-priority book debt can remain as manual review if policy allows, but it must be visible.

## Tests

Focused tests:

- `tests/test_arc_plan_patcher.py`
- `tests/test_book_plan_patcher.py`
- `tests/test_arc_patch_validator.py`
- `tests/review_engine/test_arc_book_outcomes.py`
- `tests/test_writer_prompt_contract.py`
- `tests/test_arc_execution_scoping.py`

Fixture coverage:

- identity ambiguity routes to `arc_patch`
- arc patch validates and persists
- future chapter writer context contains arc debt
- arc completion blocks unresolved arc debt
- book-level structure issue routes to `book_patch`
- disabled patcher flag routes to explicit manual review

Verification commands:

```bash
python3 -m pytest tests/test_arc_plan_patcher.py tests/test_book_plan_patcher.py tests/test_arc_patch_validator.py -q
python3 -m pytest tests/review_engine/test_arc_book_outcomes.py tests/test_writer_prompt_contract.py tests/test_arc_execution_scoping.py -q
python3 -m compileall -q forwin
git diff --check
```

## Done Criteria

- Arc/book structural issues no longer fall through to manual review when patchers are enabled and evidence is sufficient.
- Patches are persisted, validated, and auditable.
- Future writer context carries active debt.
- Completion gates block unresolved structural debt.
- Disabled policy is visible in decision audit.

## Risk Controls

- Keep patchers flag-gated.
- Block only on missing required evidence or unresolved deadline debt.
- Preserve original dispatcher fallback while engine rollout remains reversible.

## Self-Review

- Placeholder scan: components, data flow, and validation rules are concrete.
- Scope check: this spec starts only after engine and repair scope foundations.
- Consistency check: no auto-approve behavior is included.
