# Review Engine Cutover Design

## Context

`docs/designs/review-engine-cutover-spec.md` audits the current
`codex/review-engine-upgrade` branch and identifies the remaining gap between
the original review-engine plan and the implementation. The branch already has
the engine skeleton, shadow comparison, structural arc/book patch outcomes, and
some dashboard/UI plumbing. The remaining work is to make the engine the
auditable source of decisions, close the repair and deferred-acceptance gaps,
and prepare a controlled production cutover.

This design treats the cutover as one program with explicit phase gates. Code
mechanisms can be implemented continuously, but production cutover progress is
limited by real observation windows and mismatch evidence.

## Goals

- Persist every review-engine decision as an auditable `DecisionEvent`.
- Make dashboard and review-detail surfaces consume real engine decision data.
- Wire `repair_v2` into orchestration without losing rollback safety.
- Add scope-aware retry limits and escalation to avoid attempt-count driven
  repairs.
- Promote local rewrite and `commit_with_obligation` to first-class engine
  outcomes.
- Add arc/book obligation budget enforcement with historical calibration.
- Prepare live cutover with project allowlists and reverse shadowing.
- Define legacy dispatcher removal ownership without deleting legacy code before
  production stability is proven.

## Non-Goals

- No LLM supervisor layer.
- No new decision outcome outside the existing cutover spec.
- No production cutover claim without the required live observation window.
- No legacy dispatcher deletion in the first implementation wave.
- No direct database inspection or ad hoc HTTP workflow bypass for live ForWin
  project/task/chapter state.

## Scope And Phase Strategy

The implementation is one program, not a set of unrelated patches. The phases
are ordered by dependency:

1. **Audit foundation:** persist `REVIEW_ENGINE_DECISION` events, then wire the
   dashboard three-state chip to real event data.
2. **Repair shadow:** route `repair_v2` decisions through the orchestrator in
   shadow mode first, using legacy repair scope as the live path.
3. **Repair pilot:** enable `repair_v2` for allowlisted projects and introduce
   the local rewrite executor behind its own flag.
4. **Structural semantics:** add arc/book budget enforcement after historical
   audit and promote `commit_with_obligation` to an engine outcome.
5. **Cutover readiness:** implement the live cutover flag, project allowlist,
   reverse shadowing, mismatch classification, and test coverage.
6. **Legacy removal follow-up:** start only after global live cutover has been
   stable for at least 30 days.

The implementation plan should include all phases, with a review checkpoint
after phase 2 because that is the first behavior-relevant shadow milestone.
Subsequent code mechanisms can continue in the same session after checkpoint
approval, but their live flags must remain off until their gates are met.

## Decision Audit

Add `DecisionEventType.REVIEW_ENGINE_DECISION`. A helper should record engine
decisions with payload fields:

- `rule_id`
- `outcome`
- `reason`
- `missing_evidence`
- `routed_from`
- `sub_action`
- `input_digest`
- `shadow_mismatch`
- `live_or_shadow`
- `legacy_outcome`
- `engine_outcome`

Required call sites:

- Original review-outcome dispatcher shadow comparison.
- `repair_v2` shadow/live selection.
- Arc/book structural patch decisions.
- `commit_with_obligation` decisions.
- Auto-approve interval decisions.
- Reverse shadow comparisons after live cutover.

Event recording failures should log a warning and not block generation. Tests
must still assert event creation in normal paths, because dashboard and review
detail depend on the events.

## Dashboard Status Chips

`build_waiting_review_breakdown()` should consume only real
`REVIEW_ENGINE_DECISION` event payloads for review-engine rows. The status chip
classification is:

- `outcome == "system_block"` -> `系统阻断`
- `manual_review` plus policy-disabled evidence -> `可自动处理但策略关闭`
- other `manual_review` -> `需要人工判断`

Aggregation should group by `rule_id`, `outcome`, and `status_chip` so a single
rule cannot merge materially different states.

## Repair V2 Routing

`repair_v2` becomes the source of the shadow decision for repair scope. Legacy
`RepairPolicy.decide()` remains the live path until the flag allows the project
to use v2.

Configuration:

- `review_engine_repair_v2_enabled: bool`

Default behavior:

- Flag off: legacy scope is live, `repair_v2` is shadow.
- Flag on: `repair_v2` may become live only inside the active cutover stage.
  Project-scoped rollout is controlled by
  `review_engine_live_cutover_project_allowlist`, not by a separate repair
  allowlist.

Each repair decision event must include legacy scope, v2 scope, selected scope,
attempt count, issue kind, issue scope, and shadow mismatch status.

## Scope Retry Limits And Escalation

`repair_v2` must apply per-scope retry limits. The limits are weighted by scope
cost:

| Scope | Max attempts |
| --- | ---: |
| `draft` | 2 |
| `chapter_plan` | 2 |
| `band_plan` | 2 |
| `arc_plan` | 1 |
| `book_plan` | 1 |
| `subworld` | 2 |
| `active_rules` | 1 |
| `operator` | 0 |

Escalation order:

```text
draft -> chapter_plan -> band_plan -> arc_plan -> book_plan -> manual_review
```

Rules:

- `operator` routes directly to `manual_review`.
- `subworld` and `active_rules` are not normal prose rewrite scopes. If the
  relevant metadata or rule patch executor is unavailable, live routing should
  choose `manual_review` or `system_block`, not a draft rewrite.
- Exhausting `book_plan` routes to `manual_review`.
- The escalation decision must be visible in the decision event payload.

## Local Rewrite Executor

Introduce a `LocalRewriteExecutor` behind
`review_engine_local_rewrite_enabled`. It is the canonical executor for
`local_repair` outcomes, replacing scattered ad hoc autofix entry points over
time.

Initial issue coverage:

- `placeholder_leakage`
- `bare_role_placeholder_leakage`
- `body_truncated`
- `body_duplicate_span`
- `internal_state_key_leakage`
- `subworld_admission_unauthorized_new_entity`

Execution discipline:

- Re-review after every rewrite; success requires the re-review to pass.
- If rewrite fails or re-review still fails, route back into `repair_v2`
  escalation.
- Do not run both local executor and canon-gate autofix for the same issue in
  the same pass.
- Prefer the engine outcome entry point when the local executor flag is on.

`body_truncated` remains `draft` scope, but the local executor must use a
continuation mode: continue from the last complete scene rather than rewrite the
whole chapter. One failed continuation consumes the draft-scope attempt and then
the normal escalation path can raise the repair to `chapter_plan`.

## Commit With Obligation

`commit_with_obligation` becomes a first-class engine outcome instead of an
implicit side effect of legacy deferred paths.

Eligibility:

- The primary issue scope is `chapter_plan` or `band_plan`.
- A valid plan patch is available for that scope.
- Obligation budget is not exceeded.
- The obligation transaction and canon admission can execute.

Fallback decisions:

- Wrong scope -> `manual_review`.
- Missing or invalid plan patch -> `manual_review`.
- Budget exceeded -> `system_block`.
- Transaction failure -> preserve legacy fallback when available, but record the
  engine decision and failure context.

The old `defer_with_chapter_plan_patch` and `defer_with_band_plan_patch` paths
remain as fallback until cutover stability is proven.

## Arc And Book Obligation Budget

Add arc/book budget enforcement behind
`review_engine_arc_book_budget_enabled`.

Default thresholds:

| Bucket | Default |
| --- | ---: |
| `arc_p0_p1` | 2 |
| `arc_p1_p2` | 4 |
| `book_p0` | 1 |
| `book_p1_p2` | 3 |

Before enabling the flag, run:

```text
scripts/audit_obligation_distribution.py
```

The script should inspect historical projects and report P0/P1/P2 obligation
counts per arc and per book. If the defaults would block more than 5% of
historical projects, raise the defaults before enabling the flag.

Enforcement:

- Extend `evaluate_obligation_budget()` to count arc/book obligations.
- Evaluate budget before creating arc/book structural obligations.
- If over budget, emit engine `system_block` and create no new obligation.
- Decision payload must include the budget bucket, current count, threshold, and
  arc/book identifier.

## Auto-Approve Interval Discipline

The `review_interval_safe` rule must use the full-review interval, not a counter
that resets on auto-approve.

Rules:

- Every accepted chapter increments the interval counter, whether approval is
  human, checkpoint, or automatic.
- Full-review boundaries are computed from
  `chapters_since_last_full_review % review_interval_chapters == 0`.
- At interval boundaries, even warn-only chapters must go through full review.
- Auto-approve decision payload includes `chapters_since_last_full_review` and
  `review_interval_chapters`.

Acceptance fixture: with interval `5` and 12 consecutive warn-only passing
chapters, chapters 5 and 10 must hit full review; the other eligible chapters
may auto-approve.

## Live Cutover Allowlist

Configuration:

- `review_engine_live_cutover_enabled: bool`
- `review_engine_live_cutover_project_allowlist: list[str]`

Semantics:

- Flag off: legacy dispatcher is live; engine is shadow.
- Flag on with non-empty allowlist: engine is live only for allowlisted
  projects; legacy remains live elsewhere.
- Flag on with empty allowlist: engine is live globally.

When engine is live, legacy must still run as reverse shadow until the legacy
removal trigger conditions are satisfied.

## Cutover Stages And Gates

Production cutover moves through four stages:

| Stage | Scope |
| --- | --- |
| Phase 1 | One short pilot project |
| Phase 2 | Three small projects under 50 chapters |
| Phase 3 | All small and medium projects under 200 chapters |
| Phase 4 | All projects, including long-form projects |

Each phase requires at least seven days with zero severe mismatches before
advancing.

Severe mismatch means engine and legacy disagree on an outcome category that can
change chapter fate: commit, repair, manual review, or system block. Reason text,
payload detail, or non-routing audit differences are not severe, but they should
still be logged and counted.

## Legacy Removal Ownership

Legacy removal starts only after all trigger conditions are met:

- Global live cutover has been stable for at least 30 days.
- Production has zero severe mismatches during that period.
- Historical replay has zero severe mismatches.
- Rule parity tests still pass.
- `review_engine_live_cutover_enabled` is stable for all projects.

Removal is owned by one implementation owner and split into four independent
PRs in this order:

1. Remove direct dependency on `ReviewOutcomeRouter`.
2. Remove direct dependency on `ObligationScopeRouter`.
3. Remove direct dependency on `RepairPolicy`.
4. Remove orchestrator direct calls into `FinalAcceptanceGate`.

`FinalAcceptanceGate` may remain as a callable used by engine rules. The target
is removing orchestrator direct dispatch, not deleting useful validation logic.

## Testing Strategy

Required coverage:

- Audit integration: accepting or reviewing a chapter creates a
  `REVIEW_ENGINE_DECISION` event.
- Dashboard: three chip states render from event payloads.
- Repair v2 unit tests: issue classes route to expected scopes/outcomes.
- Repair v2 integration tests: flag off preserves legacy behavior; flag on uses
  v2 for allowlisted projects.
- Scope retry tests: per-scope attempt limits and escalation are deterministic.
- Local rewrite tests: each supported issue rewrites, re-reviews, and escalates
  on failure.
- Commit-with-obligation tests: eligible path commits with obligation; missing
  patch goes manual; over budget blocks.
- Arc/book budget tests: thresholds block new obligations and emit system-block
  events.
- Cutover tests: flag/allowlist matrix selects live/shadow source correctly and
  records reverse shadow comparisons.
- Interval tests: review interval boundaries are not skipped by auto-approve.

Broad verification should include the existing orchestrator, review-engine,
obligation, and API rendering tests affected by the touched modules.

## Rollback

Every behavior-changing mechanism has a flag:

- `review_engine_repair_v2_enabled`
- `review_engine_local_rewrite_enabled`
- `review_engine_commit_with_obligation_enabled`
- `review_engine_arc_book_budget_enabled`
- `review_engine_live_cutover_enabled`

Turning a flag off returns to the current branch behavior for that mechanism.
Arc/book patchers already have their own enablement flags and remain separate.

Audit event recording should be non-blocking so an event-store failure cannot
force rollback of chapter generation by itself.
