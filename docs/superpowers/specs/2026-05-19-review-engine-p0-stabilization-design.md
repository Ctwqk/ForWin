# Review Engine P0 Stabilization Design

Date: 2026-05-19

Status: draft for user review

## Scope

This spec fixes three verified bugs that currently force unnecessary manual review or hide failed constraints. It intentionally avoids introducing `AutoDecisionEngine`.

## Goals

- Preserve arc-level repair instructions when multiple reviewers merge repair scope.
- Make `ProductionPlan.review_chapters` executable instead of only informational.
- Enforce deferred-obligation budget by passing budget state into canon admission.
- Add focused regression tests for each bug.

## Non-Goals

- Do not create `forwin/review_engine`.
- Do not change `RepairPolicy` scope strategy.
- Do not add arc/book patch outcomes.
- Do not auto-approve any chapter.
- Do not redesign scheduler policy beyond consuming existing review quota.

## Current Bugs

### A1: Arc Scope Downgrade

`forwin/reviewer/hub.py` merges repair instructions and then maps arc scope down to band-level behavior. This hides arc-level problems such as identity ambiguity, artifact explanation debt, and countdown explanation debt.

Desired behavior:

- If a reviewer emits `repair_scope="arc"`, the merged instruction preserves `arc`.
- Compatibility normalization may still preserve legacy scopes where explicitly intended, but it must not silently erase arc.

### A2: Production Review Quota Is Not Executed

`forwin/production/planner.py` fills `ProductionPlan.review_chapters`, but `forwin/production/executor.py` does not consume it. Users can configure a review quota and see planned review work without any review work running.

Desired behavior:

- Scheduler/executor consumes `review_chapters`.
- `drafted` chapters run review once.
- `needs_review` chapters follow the existing review-approval equivalent path for P0.
- Review work does not start new generation when the plan contains no write work.

### A3: Deferred Acceptance Drops Budget Result

`DeferAcceptanceTransaction.run()` creates and plans an obligation, then calls `evaluate_canon_admission()` without passing `over_budget`. The budget policy result is therefore lost on the deferred path.

Desired behavior:

- The transaction evaluates obligation budget using existing open obligations plus the new obligation.
- `budget.over_budget` is passed to `evaluate_canon_admission()`.
- If the gate blocks with `obligation_budget_exceeded`, the nested transaction rolls back the new obligation and patch.

## Design

### Arc Scope Preservation

Modify only the scope merge path. The fix should:

- preserve `arc` when it wins the merge ranking
- keep existing `world_model` preservation behavior
- leave downstream policy behavior unchanged for P0

Regression test:

- Build a chapter-level instruction and an arc-level instruction.
- Merge them through `HistoricalReviewHub._merge_repair_instructions`.
- Assert final `repair_scope == "arc"`.

### Production Review Execution

Extend `ProductionPlan` with enough status metadata to route review jobs:

- `review_chapters: list[int]`
- `review_chapter_statuses: dict[int, str]`

The planner fills status metadata from backlog source:

- `needs_review` means approval path
- `drafted` means review path

The executor gains injected callbacks so scheduler wiring stays testable:

- `review_chapter(project_id: str, chapter_number: int)`
- `approve_chapter_review(project_id: str, chapter_number: int)`

The executor returns a review-specific action such as `ran_review_jobs` when review was the only work executed.

P0 callback rule:

- For `drafted`, call the injected review action. The first implementation may delegate to the existing accept-review equivalent if no standalone review endpoint exists, but the callback name must keep the design open for P1.
- For `needs_review`, call the injected approval action.

Regression test:

- Plan contains chapter 2 as `needs_review` and chapter 3 as `drafted`.
- Executor calls approval for 2 and review for 3.
- Result reports `ran_review_jobs` and count 2.

### Deferred Budget Enforcement

The transaction should evaluate budget before entering the nested persistence block. Required inputs:

- open obligations for the same project with statuses `proposed`, `planned`, `active`, or `expired`
- the new prepared obligation
- current chapter
- band bounds from target band or affected chapters
- arc bounds from target plan, target band, target arc, or current project arc range

The budget result is not itself persisted in P0. It is fed into canon admission:

```python
evaluate_canon_admission(..., over_budget=budget_result.over_budget)
```

Regression test:

- Seed two existing structural P1 obligations in the same arc.
- Add one more structural P1 obligation.
- Transaction returns `success=False`.
- Errors contain `obligation_budget_exceeded`.
- The newly proposed obligation and patch are not persisted.

## Error Handling

- If a review callback is absent, executor skips that review job and does not count it.
- If review callback raises, scheduler transaction should roll back as it does for other execution errors.
- Deferred budget failure uses existing gate blocking reasons rather than a new exception type.

## Tests

Focused tests:

- `tests/test_reviewer_split.py`
- `tests/test_production_executor.py`
- `tests/test_production_planner.py`
- `tests/test_defer_acceptance_transaction.py`
- `tests/test_obligation_budget.py`

Verification commands for the implementation plan:

```bash
python3 -m pytest tests/test_reviewer_split.py tests/test_repair_scope_router.py -q
python3 -m pytest tests/test_production_executor.py tests/test_production_planner.py tests/test_production_scheduler.py -q
python3 -m pytest tests/test_defer_acceptance_transaction.py tests/test_obligation_budget.py tests/test_orchestrator_deferred_acceptance.py -q
python3 -m compileall -q forwin
git diff --check
```

## Done Criteria

- Arc scope survives reviewer merge.
- Production review quota has a real execution path.
- Deferred acceptance respects over-budget state.
- All new tests fail before the fix and pass after the fix.
- No `AutoDecisionEngine` code exists in this P0 change.

## Risk Controls

- Keep P0 narrow and directly test each bug.
- Avoid changing scheduler due-project selection.
- Avoid changing canon admission rules; only pass the missing input.

## Self-Review

- Placeholder scan: no open design placeholders.
- Scope check: this is a single small implementation plan.
- Consistency check: P0 explicitly avoids engine work and feature-flag architecture.
