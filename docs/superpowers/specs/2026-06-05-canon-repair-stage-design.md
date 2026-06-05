# Canon Repair Stage Design

## Context

The chapter generation loop currently treats the first review verdict as the only
entry point into automatic repair. In blackbox mode, `_review_and_maybe_rewrite()`
repairs only when the initial chapter review returns `fail`. A chapter with
`warn` can continue into canon admission, but if canon admission later rejects
the candidate, the outer loop marks the chapter `needs_review` without giving
the automatic repair system a chance to act.

This creates a bad blackbox outcome: a chapter can stop at `needs_review` with
zero repair attempts even though the canon quality gate found a repairable
blocking issue.

## Goal

Add a separate canon repair phase so a canon admission failure can upgrade a
previous `warn` or `pass` into an automatic repair path before the chapter is
paused for review.

## Non-Goals

- Do not delete or hide historical rewrite attempts.
- Do not collapse human review, operator system blocks, and repair exhaustion
  into the same generic status message.
- Do not refactor the whole orchestrator loop beyond the canon admission and
  repair boundary needed for this behavior.

## Chosen Approach

Use an additional `canon_repair` phase after canon admission.

The existing review repair phase remains responsible for initial review
failures. If the initial review is `warn` or `pass`, the chapter may still enter
canon admission. If canon admission returns `commit_allowed=false` and provides
a known repair scope, the orchestrator starts a new canon repair round with a
fresh repair budget.

The fresh budget is only a budget reset. Historical `ChapterRewriteAttempt`
records are preserved for auditability.

## Data Flow

```text
writer output
  -> review repair phase
  -> canon admission phase
  -> canon repair phase, only if canon blocks with a repairable scope
  -> canon commit or needs_review/system_block
```

Rules:

- `review repair phase` repairs initial review `fail` verdicts.
- `review=warn` and `review=pass` can proceed to canon admission.
- `canon admission` is allowed to be stricter than the initial review verdict.
- `canon=fail` with a repairable `required_repair_scope` starts canon repair.
- Canon repair re-runs review and canon admission after each attempt.
- Canon commit happens only after canon admission allows it.
- A chapter reaches `needs_review` after repair exhaustion, not before the canon
  repair opportunity.
- A non-repairable operator/system issue is reported as a system block, not as
  ordinary human review.

## Repair Budget Semantics

Canon repair receives a fresh per-phase budget. For example, if review repair
already used four attempts, canon repair can still receive its own full retry
budget.

Historical attempts remain stored. The implementation should distinguish total
attempts from current phase attempts internally. Existing API fields should not
lose information. If a new response field is needed, prefer additive fields such
as:

- `repair_attempt_count_total`
- `current_repair_phase`
- `current_phase_repair_attempt_count`

The existing `repair_attempt_count` field may continue to represent total
attempts for compatibility unless an existing caller clearly expects current
phase count.

## Component Boundaries

- `review repair phase`: keep using the existing review repair loop for initial
  review failures.
- `canon admission phase`: keep using the existing canon quality gate to decide
  whether a candidate can be committed.
- `canon repair phase`: add orchestration around canon admission blocks. This
  phase converts a gate block and its `required_repair_scope` into a repair
  attempt sequence.
- `ChapterRewriteAttempt`: preserve all attempts and add enough phase metadata
  to distinguish `review_repair` from `canon_repair`.
- Task/status reporting: distinguish `canon repair running`, `canon repair
  exhausted`, `operator system block`, and `needs human review`.

## Error Handling

- If canon admission returns `commit_allowed=false` and
  `required_repair_scope` is one of `draft`, `chapter_plan`, `band`, `arc`, or
  `book`, start canon repair.
- If the scope is missing, unknown, or explicitly operator-only, do not mark the
  chapter as ordinary `needs_review`. Report a system block with the gate reason.
- If canon repair exhausts its fresh budget, mark the chapter `needs_review`
  with `repair_exhausted=true`, `canon_risk_level=high`, and the latest repair
  scope.
- If canon repair succeeds, continue to canon commit and normal chapter
  completion.
- Task messages must accurately describe the blocking reason instead of using a
  generic "automatic repair or retry needed" message when no automatic route was
  taken.

## Testing

Add regression tests for:

- `review=warn` followed by canon admission failure with a repairable draft
  scope triggers canon repair and does not pause with zero attempts.
- Review repair attempts before canon admission do not consume the canon repair
  phase budget.
- Canon repair exhaustion pauses the chapter only after the canon repair budget
  is used and marks the chapter as exhausted/high risk.
- Canon repair success re-runs canon admission and then commits canon.
- Operator-only or unknown canon block scopes are surfaced as system blocks, not
  ordinary human review.

## Success Criteria

- The fifteen-chapter blackbox run cannot stop at chapter 2 with
  `repair_attempt_count=0` solely because a `warn` review later failed canon
  admission.
- A canon admission failure can upgrade the effective verdict into an automatic
  repair path.
- Repair history remains auditable.
- Existing APIs remain backward compatible, with any new phase fields added
  additively.
