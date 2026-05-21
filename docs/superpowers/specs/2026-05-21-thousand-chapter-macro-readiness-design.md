# Thousand-Chapter Macro Readiness Design

## Context

`docs/superpowers/specs/2026-05-21-thousand-chapter-readiness-completion-design.md`
is implemented on `master` through its P0-P2 backend loop: pulp beat policy,
deferred extraction maintenance, read-only pressure KPIs, task leases, worker
claiming, typed retrieval budgets, trope cooldown, and long-run defaults are in
place and covered by focused tests.

The remaining work comes from `docs/designs/thousand-chapter-readiness.md`
sections P3 and P4. This pass keeps the earlier decisions intact:

- do not introduce Saga or Volume as new planning layers
- do not create a second total chapter target
- do not create a separate ledger stack beside BookState, narrative
  obligations, and publisher feedback aggregates
- do not add legacy compatibility paths or world-v4 projection writes

One small entry-contract cleanup is included because current UI markup still
contains a `max=200` chapter input while the API, MCP, and JS validation allow
5000.

## Goals

- Make arc-scale progression explicit without adding Saga or Volume.
- Add project-level progression rules that can constrain planning and trope
  selection across hundreds of chapters.
- Expose a BookState-facing protagonist macro-status projection derived from
  accepted story state, not from a new canonical ledger.
- Audit arc boundaries so an arc that promised a status, wealth, enemy, or
  market-space shift cannot silently finish without the shift.
- Feed existing publisher and audience aggregates back into experience
  calibration in a deterministic, testable way.
- Add the report plumbing needed to prove a 1000-chapter dry run at the state
  machine and telemetry layer.
- Align the remaining UI chapter-target input with the 5000-chapter contract.

## Non-Goals

- No live 1000-chapter generation runner in this pass. The dry-run proof is a
  read-only or seeded-state validation of state machine, lease, pressure, and
  metric aggregation paths.
- No LLM evaluator for macro quality.
- No external queue or scheduler.
- No new project creation state, legacy route, old-client alias, or
  compatibility storage path.
- No duplicated `AudienceCalibrationProfile` expansion inside old phase code
  unless the current canonical service needs a compatibility adapter.

## Approach Options

### Option A: Add Saga and ledger tables

This would directly mirror the external review wording, but it conflicts with
the existing Arc/Band/Chapter model and creates duplicate truth beside
BookState and narrative obligations.

### Option B: Store macro progression as JSON on Arc and Project rules

This keeps Arc as the long-run progression unit and adds one structured JSON
payload to `ArcPlanVersion` plus one project-level rule table. BookState derives
macro status from current canonical rows. This is the recommended option
because it gives the planner hard contracts without introducing a second
coordinate system.

### Option C: Keep everything as prompt-only text

This is lowest cost, but it cannot support deterministic arc-boundary audits or
1000-chapter evidence. It leaves the original P3 risk unsolved.

The implementation should follow Option B.

## Architecture

### Arc Macro Progression

Add `macro_progression_json` to `ArcPlanVersion`. The payload is validated by a
Pydantic model, `ArcMacroProgression`, with these fields:

- `status_promise`: plain-language promise for the protagonist's public status
  change in the arc
- `status_tier_from` and `status_tier_to`: normalized integer tiers
- `wealth_tier_from` and `wealth_tier_to`: normalized integer tiers
- `enemy_tier_from` and `enemy_tier_to`: normalized integer tiers
- `market_space_from` and `market_space_to`: textual arena labels
- `ladder_rung_target`: concise target such as `village_to_county`,
  `outer_to_inner_sect`, or `small_vendor_to_regional_operator`
- `required_boundary_evidence`: facts that should be visible by arc end
- `forbidden_repetition_patterns`: repeated plots that should be suppressed
  inside this arc

Storage is JSON text to avoid schema churn for every tier field. Access must go
through helpers that return a typed model and normalize missing historical rows
to an empty progression contract.

### Project Progression Rules

Add a `project_progression_rules` table and repository. A rule has:

- `rule_type`: `trope_filter`, `repetition_ban`, `status_floor`,
  `wealth_ceiling`, `enemy_tier_floor`, `market_space_lock`, or
  `macro_boundary`
- `chapter_start` and `chapter_end`
- `severity`: `warning` or `blocking`
- `payload_json`: typed rule details
- `active`: boolean

The first consumers are the trope scheduler and future-plan auditor. Rules are
project scoped and chapter ranged. They do not replace BookState canon; they
guide planning and audits.

### BookState Macro Status Projection

Add `ProtagonistMacroStatus` and a query-interface method:

```python
get_protagonist_macro_status(project_id: str, as_of_chapter: int) -> ProtagonistMacroStatus
```

The initial implementation derives status from accepted chapter plans, decision
events, narrative obligations, and BookState runtime summaries. It returns:

- `status_tier`
- `wealth_tier`
- `enemy_tier`
- `market_space`
- `evidence_refs`
- `source`: `book_state_macro_projection`

This is a projection, not a ledger. It is recalculable and safe to improve.

### Arc Boundary Audit

Extend `FuturePlanAuditor` with a focused macro-progression audit. When a run
reaches an arc boundary or audits a just-finished arc, compare the arc's
`*_tier_to` and `market_space_to` fields with `ProtagonistMacroStatus`. If a
blocking requirement is unmet, produce a `FuturePlanAuditIssue` with
`issue_type="arc_macro_progression_not_met"` and a blocking reason. The audit
must not write repair text directly; it should emit plan-patchable metadata so
existing plan patch mechanisms can add boundary payoff instructions.

### Trope and Rule Consumption

`BandPlanService` already passes recent trope usage into
`AudienceCalibrationProfile`. Add active progression rules to that calibration
or a nearby explicit selector input. The scheduler filters blocked template ids,
blocked categories, and banned repetition patterns before normal cooldown
selection. If every candidate is blocked, the scheduler may fall back to the
least-bad candidate but must record a warning in the selected schedule metadata.

### Publisher Feedback Calibration

The project already has `SignalWindowAggregate`, `ReaderScaleSnapshot`, and
`ExperiencePlanningService.build_audience_calibration_profile()`. Extend the
canonical `forwin.experience.service.AudienceCalibrationProfile` with compact
fields instead of creating a new feedback service:

- `favor_visible_payoff`
- `reduce_setup_ratio`
- `avoid_trope_categories`
- `boost_status_payoff`

Map existing aggregates deterministically:

- pacing/read-through weakness raises reward density and visible payoff
- confusion/risk raises rule legibility
- character heat protects relationship and character beats
- repeated complaint categories populate avoid categories
- scale growth raises status-payoff emphasis

The existing phase24 local dataclass can remain unchanged unless tests prove a
runtime compatibility issue; new canonical code should use
`forwin.experience.service`.

### 1000-Chapter Dry-Run Report

Extend `scripts/pulp_pressure_test.py` with a dry-run validation mode that reads
existing or seeded telemetry and reports:

- `task_resume_success_rate`
- `arc_macro_boundary_failure_rate`
- `progression_rule_violation_rate`
- `macro_status_evidence_gap_rate`
- existing P0-P2 pressure fields

The script remains read-only against production project data.

### UI Entry Cleanup

Align `forwin/ui_assets/home/body.html` target chapter input with 5000 and add a
test or static assertion so API, MCP, JS, and markup cannot drift again.

## Data Flow

1. Genesis or arc materialization creates `ArcPlanVersion` rows.
2. Macro-progression helpers attach or normalize `macro_progression_json`.
3. Project rules are loaded for the current chapter range.
4. Band planning passes rule context plus audience calibration into the
   scheduler.
5. Accepted chapters update the existing canon, decision, obligation, and
   feedback rows.
6. BookState macro status is derived as-of a chapter.
7. Future-plan audit checks arc boundary promises against derived macro status.
8. Pressure reporting computes dry-run macro and P0-P2 metrics without mutating
   generation state.

## Error Handling

- Missing macro progression on historical arcs normalizes to an empty contract
  and produces no blocking audit.
- Invalid `macro_progression_json` is treated as an audit warning and does not
  crash generation.
- Blocking project progression rules can block planning or audit output, but
  they do not rewrite accepted chapters.
- Publisher feedback calibration ignores incomplete aggregate rows and only acts
  on confirmed, watchlist, or high-scoring candidate signals.
- Dry-run reporting treats missing telemetry as unavailable metrics rather than
  fabricated success.

## Tests

- Arc macro progression model normalization and migration/default tests.
- Project progression rule repository tests for active ranged rules.
- Trope scheduler tests for rule-based template/category filtering plus
  cooldown fallback warning.
- BookState macro-status projection tests using accepted chapters and decision
  events.
- Future-plan auditor tests for passing and failing arc boundary checks.
- Experience calibration tests for publisher/audience aggregate mappings.
- Pressure report tests for 1000 dry-run macro fields.
- UI/static contract test proving target chapter max is 5000 in schema, MCP, JS,
  and markup.

## Completion Definition

The pass is complete when focused tests pass, `python3 -m compileall -q forwin
scripts` passes, `git diff --check` passes, strict legacy inventory audit
passes, and pressure reports can emit both the existing P0-P2 KPI fields and
the new macro dry-run fields without mutating project state.
