# Thousand-Chapter Readiness Design

## Context

ForWin can already generate, review, repair, index, and publish long-form Chinese
web novel chapters. The current gap is not basic generation. The gap is proving
that a pulp profile can run at 1000+ chapter scale with bounded cost, bounded
context growth, low payoff drift, recoverable operations, and no new legacy
runtime dependencies.

This design supersedes the analysis in
`docs/designs/thousand-chapter-readiness.md` as the implementation contract. The
analysis document remains useful background; this spec defines the buildable
scope.

## Goals

- Make 1000+ chapter targets first-class enough that operators do not have to
  manage a stack of short-run extensions by hand.
- Produce real pressure-test evidence for 30, 100, and 300 chapter runs.
- Add deterministic pulp beat visibility before adding expensive evaluators.
- Prevent failed chapters from causing same-run continuity cascades.
- Treat observation and index failures as recoverable maintenance, not chapter
  failure, when core writing/review/canon already passed.
- Improve structured extraction so chapter-end payoff facts are not discarded by
  fallback truncation.
- Prepare durable generation resume without introducing Kafka, Celery, Temporal,
  or a second job system.
- Improve long-run retrieval and trope variety through typed budgets and
  cooldown state.

## Non-Goals

- Do not introduce Saga or Volume as new planning layers. Arc remains the
  long-run progression unit.
- Do not introduce a second total-chapter target. `Project.target_total_chapters`
  remains the single source of truth for the book target.
- Do not create a separate promise, wealth, enemy, or status ledger from scratch.
  Extend BookState projections, narrative obligations, and memory retrieval.
- Do not replace the ForWin MCP/API workflow with pressure scripts that mutate
  generation state directly.
- Do not add compatibility for old projects or old clients.

## Legacy Boundary

This work starts after the legacy inventory freeze. New long-run code must not
depend on:

- `legacy_entity_id`
- world v4 / legacy world model projection writes
- `state.location`
- `creation_status="legacy"`
- legacy review outcome adapters as a required runtime source
- old API constructor shapes or old env aliases

If a touched file still contains legacy removal work owned by another phase, the
implementation plan must either avoid that file or sequence behind the phase
owner. New long-run features must use canonical BookState ids, current project
creation states, current API schema, and current task records.

## Architecture

### LongRunPolicy

Add a lightweight execution policy instead of a new target object.

`Project.target_total_chapters` remains the total target used by Genesis,
Arc sizing, review forms, auto-continue, and future planning. Long-run behavior
is represented by a policy payload with fields such as:

```json
{
  "mode": "daily_serial | factory_batch | soak_test",
  "batch_size": 5,
  "stop_on_chapter_failure": true,
  "defer_observation_failures": true,
  "payoff_gap_limit": 2,
  "resume_policy": "manual_after_failed_chapter | auto_after_infrastructure_failure"
}
```

The policy can initially live in project automation/governance payloads or saved
runtime config. It should not duplicate `target_total_chapters`.

### Entry Contracts

The 200 chapter create limit and 100 chapter extend limit must be updated as a
single entry-contract change:

- FastAPI schema validation
- MCP client validation
- UI form validation and default copy
- tests for create, extend, and MCP validation

The default target should be product-facing, not a model default that silently
creates three-chapter projects. Existing DB rows may keep their stored values.

### Pressure Evidence

`scripts/pulp_pressure_test.py` becomes a read-only collector. It accepts a
project id and chapter range, then reads current source-of-truth rows:

- `GenerationTask`
- `ChapterPlan`
- `DecisionEvent`
- `CandidateDraftRecord`
- `PromptTrace`
- `PerformanceSpan`

It writes `metrics.csv`, `summary.json`, and a short README. It must not create
projects, start writing, continue generation, or bypass MCP/API workflow rules.

### PulpBeatVerifier

Add a deterministic, low-cost verifier that inspects accepted chapter text and
selected trope context. The first version is rules and dictionaries, not an LLM
evaluator.

Output fields:

- `pressure_present`
- `protagonist_action_present`
- `visible_payoff_present`
- `audience_reaction_present`
- `enemy_or_obstacle_damage_present`
- `new_gain_or_status_shift_present`
- `next_hook_present`

The verifier writes structured metadata to hard-floor results and decision
events. In P0 it mainly produces metrics and warnings. In P1 it can become fatal
only when policy thresholds are crossed, such as two consecutive missing-payoff
chapters or `reward_gap_p95` exceeding the configured limit.

### Failure Handling

Chapter failures are separated from recoverable maintenance failures.

Fatal chapter failures:

- hard floor failure
- fatal pulp beat policy failure
- fatal canon/review failure
- unrepaired writer failure

Fatal chapter failures mark the current chapter failed and stop the current run.
The runner must not continue to later chapters in the same run.

Recoverable maintenance failures:

- memory index upsert failure
- prompt/observability persistence failure when core chapter state is already
  durable
- deferred structured extraction failure after the accepted chapter body is
  durable
- non-core projection refresh failure

Recoverable failures create maintenance/deferred events and keep the chapter
accepted when writing, review, and canon admission already succeeded.

### Structured Extraction

The writer extraction fallback changes from first-1800-character truncation to a
three-window fallback:

- head window around 1200 characters
- middle window around 1200 characters
- tail window around 1600 characters

The normalized result merges state changes, events, thread beats, lore
candidates, timeline hints, and notes. Tail evidence is especially important for
payoff, enemy damage, item gain, wealth change, and status shift facts.

If all fallback windows fail, the chapter receives a
`structured_extraction_deferred` maintenance event. The pressure report counts
that as degraded or failed extraction.

### Durable Generation Runner

P2 extends `generation_tasks` for lease-based execution:

- `lease_owner`
- `lease_expires_at`
- `heartbeat_at`
- `resume_from_chapter`
- `run_until_chapter`
- `max_chapters`

A worker claims queued tasks or expired running leases and periodically refreshes
the heartbeat. Restart recovery no longer marks resumable running tasks failed by
default. A failed chapter remains a hard blocker until an explicit repair or
resume operation clears it.

Resume point derivation:

- If there is a failed chapter in the requested range, stop at the failed
  blocker.
- Otherwise resume from `accepted_max + 1`.
- Never skip planned or failed chapters to write later chapters.

The current daemon-thread API path can remain as a trigger, but DB lease state is
the execution authority.

### Typed Retrieval Budgets

Retrieval shifts from a single `max_memories=3` query to typed quotas. The first
implementation should keep the existing memory index but add stable categories:

- recent chapter memory
- promise or obligation memory
- enemy or obstacle memory
- wealth, item, and status memory
- relationship or faction memory
- world/context pages

Sources should be BookState projections, narrative obligations, and memory index
search. No legacy Entity bridge or world v4 projection is allowed.

### Trope Cooldown

Band-level `used_template_ids` is local to one scheduling call and is not enough
for 1000 chapters. Cooldown state becomes persistent:

- no same template in the last N bands
- no same category inside a configured K-band gap unless no alternative exists
- pressure report records repeated-template and repeated-category rates

The preferred storage is experience persistence or BookState projection. The
scheduler consumes cooldown state and emits selected templates plus cooldown
updates after acceptance.

## Phases

### P0: 30-Chapter Proof

Deliverables:

- Entry contract updates for 1000+ targets across API, MCP, and UI.
- `LongRunPolicy` payload and config plumbing without duplicating total target.
- Fatal chapter failure stops the current run.
- Memory index and observation failures become deferred maintenance when core
  chapter state already passed.
- Real read-only pressure collector.
- Deterministic `PulpBeatVerifier` metrics and warnings.

Acceptance:

- Targeted tests cover create/extend/MCP/UI validation.
- Hard-floor failure test proves the same run stops after the failed chapter.
- Memory upsert failure test proves accepted chapter status is preserved when
  the failure is recoverable.
- Pressure collector test uses seeded rows and produces non-placeholder metrics.
- Pulp beat verifier test covers pass, missing payoff, and missing hook cases.
- A 30 chapter report can show LLM calls, wall time, prompt/context slope,
  hard-floor failures, payoff missing rate, and reward gap.

### P1: 100-Chapter Quality Stability

Deliverables:

- Three-window structured extraction fallback.
- Deferred extraction maintenance event on full fallback failure.
- Pulp beat policy supports warning versus fatal based on consecutive misses or
  reward gap.
- Pressure report includes extraction failure rate and PulpBeatVerifier fields.

Acceptance:

- Extraction fallback tests prove tail-only payoff facts are captured.
- Full fallback failure test creates a deferred extraction event.
- Two-consecutive-missing-payoff test blocks under pulp/factory policy.
- A 100 chapter report shows stable context slope, reward gap p95, visible
  payoff missing rate, and extraction failure rate.

### P2: 300-Chapter Unattended Readiness

Deliverables:

- Lease and heartbeat fields on `generation_tasks`.
- Worker claim and resume logic.
- Restart recovery leaves resumable tasks claimable instead of failed.
- Typed retrieval budgets.
- Persistent trope cooldown.

Acceptance:

- Unit tests cover claim, heartbeat refresh, expired lease reclaim, failed
  chapter blocker, and resume from accepted max.
- Retrieval tests prove typed quotas include at least recent, promise, enemy,
  wealth/status, and world buckets.
- Trope scheduler tests prove template/category cooldown across bands.
- A 300 chapter report shows bounded prompt/context growth, task resume success,
  controlled trope repetition, and non-worsening payoff metrics.

## Metrics

Required pressure summary fields:

- `avg_llm_calls_per_chapter`
- `p95_wall_time_seconds`
- `prompt_char_count_slope`
- `context_pack_char_count_slope`
- `reward_gap_p95`
- `visible_payoff_missing_rate`
- `hard_floor_fail_rate`
- `canon_extraction_failure_rate`
- `repeat_trope_template_rate`
- `repeat_trope_category_rate`
- `task_resume_success_rate` after P2

Initial targets:

- Pulp average LLM calls per chapter at or below 3.
- Prompt/context slope approximately flat after warm-up.
- Reward gap p95 at or below 2.
- Visible payoff missing rate below 15%.
- Hard-floor failure rate below 5%.
- Canon extraction failure rate below 2%.
- P2 resume success above 99% in controlled restart tests.

## Sequencing

The implementation plan should split work into independent patch groups:

1. Entry contract and LongRunPolicy.
2. Failure handling and deferred maintenance.
3. Pressure collector and PulpBeatVerifier.
4. Three-window extraction and extraction maintenance.
5. Durable generation lease and resume.
6. Typed retrieval budgets.
7. Persistent trope cooldown.
8. Pressure-test scenarios and reporting documentation.

Patch groups 1-3 are required before a meaningful 30 chapter proof.
Patch group 4 is required before claiming 100 chapter quality stability.
Patch groups 5-7 are required before claiming unattended 300 chapter readiness.

## Risks

- A second target field would desynchronize planning and auto-continue. This
  design avoids it.
- A complex LLM evaluator before real metrics would raise cost without proving
  stability. Start deterministic.
- Durable runner changes can conflict with active generation task UI. Keep API
  trigger semantics stable while moving execution authority to DB leases.
- Retrieval buckets can become prompt bloat if quotas are not enforced. The
  pressure collector must report bucket sizes and total context size.
- Legacy-removal phases may touch the same files. The implementation plan must
  sequence around those phase owners and must not add new legacy references.
