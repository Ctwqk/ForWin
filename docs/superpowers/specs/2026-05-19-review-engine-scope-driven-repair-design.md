# Review Engine Scope-Driven Repair Design

Date: 2026-05-19

Status: draft for user review

## Scope

This spec changes repair scope selection from attempt-count driven escalation to issue-scope driven routing. It depends on the engine shadow layer so new routing can be compared against old routing before it is enabled.

## Goals

- Introduce a central issue taxonomy.
- Classify the primary issue from review verdicts and canon-quality signals.
- Route repair by issue kind and source layer.
- Use attempts only as retry budget within a selected scope.
- Preserve provenance fields through normalization.
- Run new routing behind `review_engine.repair_v2_enabled`.

## Non-Goals

- Do not implement arc/book patch execution. This phase can output a needed scope, but Spec D makes it executable.
- Do not auto-approve chapters.
- Do not remove old `RepairPolicy`.
- Do not change writer prose generation.

## Issue Taxonomy

Create `forwin/review_engine/issue_taxonomy.py`.

Primary concepts:

- `IssueKind`
- `IssueScope`
- `IssueSourceLayer`
- `IssueTaxonomyEntry`

Scope values:

- `draft`
- `chapter_plan`
- `band_plan`
- `arc_plan`
- `book_plan`
- `subworld`
- `active_rules`
- `operator`

Examples:

- placeholder leakage -> `draft`
- single-chapter pacing -> `chapter_plan`
- band foreshadowing debt -> `band_plan`
- identity ambiguity -> `arc_plan`
- countdown explanation across accepted chapters -> `arc_plan`
- book structure violation -> `book_plan`
- form schema invalid -> `operator`
- missing canon entity admission -> `subworld`

## Primary Issue Classification

Classification input:

- review issues
- canon-quality signals
- obligation facts
- existing provenance fields

Priority rule:

1. blocking evidence with source metadata wins over unsupported generic verdict text
2. larger required scope wins when severity is comparable
3. error severity wins over warning when scope is comparable
4. infrastructure/source-layer issues route away from writer-facing scopes

This preserves the earlier architecture rule that `warn`, `uncertain`, and no-evidence outputs do not become blockers on their own.

## Repair V2 Decision

New decision helper:

```python
def decide_repair_v2(input: DecisionInput) -> Decision:
    primary = classify_primary_issue(input)
    scope = taxonomy.scope_for(primary)
    if input.attempts_completed >= max_attempts_for(scope):
        scope = escalate_after_scope_budget(scope)
    return decision_for_scope(scope, primary)
```

Attempts are a safety loop breaker. Attempts do not choose the initial scope.

## Shadow Comparison

When repair v2 is disabled:

- old repair policy remains live
- repair v2 computes a shadow decision
- engine audit records old and new scope
- mismatch reports include issue kind and provenance

When enabled:

- repair v2 becomes live for scopes that are executable
- scopes not yet executable become explicit `manual_review` or `system_block` with rule id such as `scope_not_executable`

## Tests

Focused tests:

- `tests/review_engine/test_issue_taxonomy.py`
- `tests/review_engine/test_repair_v2.py`
- `tests/review_engine/test_repair_v2_shadow.py`
- existing repair policy tests remain valid for old mode

Required fixture coverage:

- draft-level issue first attempt and retry budget exhausted
- chapter-plan issue first attempt and retry budget exhausted
- band-plan issue first attempt and retry budget exhausted
- arc-level issue first attempt and retry budget exhausted
- infrastructure issue routes to operator without writer repair

Verification commands:

```bash
python3 -m pytest tests/review_engine/test_issue_taxonomy.py tests/review_engine/test_repair_v2.py -q
python3 -m pytest tests/test_repair_scope_router.py tests/test_repair_scope_router_dispatch.py tests/test_chapter18_repair_routing_regression.py -q
python3 -m compileall -q forwin
git diff --check
```

## Done Criteria

- Issue taxonomy maps known issue kinds to intended scopes.
- Arc-level issues no longer start as draft repairs.
- Attempts only affect retry exhaustion.
- Feature flag off preserves old behavior.
- Feature flag on uses scope-driven repair where executable.
- Provenance fields survive the decision path.

## Risk Controls

- Keep old policy available.
- Start with shadow distribution reporting.
- Do not emit live arc/book patch outcomes until Spec D is available.
- Route non-executable structural scopes to explicit manual review instead of falling through.

## Self-Review

- Placeholder scan: taxonomy examples are concrete and implementation boundaries are named.
- Scope check: this can be planned independently after Spec B.
- Consistency check: Spec C does not claim arc/book patch execution.
