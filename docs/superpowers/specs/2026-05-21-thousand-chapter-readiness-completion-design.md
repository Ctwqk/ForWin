# Thousand-Chapter Readiness Completion Design

## Context

`docs/superpowers/specs/2026-05-21-thousand-chapter-readiness-design.md`
already defines the long-run readiness direction. The first implementation pass
landed several primitives on `master`: higher API chapter limits, `LongRunPolicy`,
same-run stop on hard-floor failure, memory-upsert deferred maintenance,
three-window extraction fallback, read-only pressure collection, task lease
columns/helpers, typed retrieval budgets, and persistent trope cooldown.

This completion spec defines the remaining backend work needed before the
design can be called implemented through P2. It intentionally does not include
P3/P4 macro-progression quality work.

## Goals

- Make entry defaults consistent across API, MCP, ORM, and bootstrap paths.
- Convert deterministic pulp beat checks from warning-only metadata into a
  policy gate that can fail after a configured consecutive payoff miss window.
- Record pulp beat evaluations for accepted chapters so pressure reports can
  compute payoff-missing metrics without re-reading chapter text.
- Record deferred structured extraction maintenance when all extraction windows
  fail, instead of only returning degraded metadata.
- Expand pressure reports to include the required KPI fields from the original
  spec: slopes, p95s, payoff missing rate, extraction failure rate, hard-floor
  failure rate, and trope repetition rates.
- Add a DB-lease generation worker entry point that can claim queued or expired
  generation tasks and run them through the existing generation/continue paths.
- Preserve current daemon-thread API behavior as a compatibility trigger; the
  new worker path is the durable execution path.

## Non-Goals

- No Saga or Volume layer.
- No Kafka, Celery, Temporal, or external queue.
- No second total-chapter target beside `Project.target_total_chapters`.
- No new legacy compatibility routes, aliases, or world-v4 projection writes.
- No P3 macro ladder, progression-rule table, or publishing feedback loop in
  this pass.

## Architecture

### Entry Contract Consistency

`Project.target_total_chapters` should default to 50 in ORM and SQLite/Postgres
bootstrap upgrade code. API and MCP clients already accept up to 5000; this pass
keeps that limit and adds tests proving ORM-created projects do not silently
become three-chapter projects.

### Pulp Beat Policy

`run_hard_floor()` remains the deterministic local chapter verifier. The
orchestrator records a `pulp_beat_evaluated` decision event whenever hard-floor
checks run. The payload includes the `PulpBeatResult`, warning reasons, and
current policy fields.

Policy failure is evaluated in the orchestrator because it has session access to
recent decision events. In `pulp` quality profile, or when
`LongRunPolicy.mode` is `factory_batch` or `soak_test`, two consecutive missing
visible-payoff chapters fail the current chapter and stop the current run. The
threshold comes from `LongRunPolicy.payoff_gap_limit`, with a default of `2`.
Plain standard-profile runs keep warning-only behavior.

### Deferred Structured Extraction

`ChapterWriter` continues to return degraded metadata when all extraction passes
fail. The orchestrator inspects accepted `WriterOutput.generation_meta`; if any
structured extraction part degraded, it records a deferred maintenance event with
task type `structured_extraction`. This keeps the accepted chapter durable while
making extraction debt visible to pressure reports and future workers.

### Pressure Metrics

`scripts/pulp_pressure_test.py` remains read-only. It consumes existing project
telemetry and emits:

- per-chapter CSV rows with pulp beat fields and extraction status
- summary p95 fields for wall time and reward gap
- prompt/context slope using first-to-last observed telemetry values
- visible payoff missing rate from `pulp_beat_evaluated`
- hard-floor failure rate from hard-gate events and chapter verdicts
- canon extraction failure rate from deferred structured extraction events
- repeated trope template/category rates from selected trope ids and categories

The script must still avoid project creation, task mutation, generation starts,
or direct state edits.

### Durable Worker Entry Point

`forwin.generation.task_lease` already owns claim and heartbeat helpers. This
pass adds a small worker module that:

- claims one queued or expired generation task with `SKIP LOCKED`
- derives resume chapter from task state
- dispatches to existing project generation functions
- refreshes the heartbeat before and after execution
- leaves failed-chapter blockers visible instead of skipping ahead

The worker is a library entry point and CLI-safe function for tests. API daemon
threads continue to run as they do today, but queued tasks can now be reclaimed
by the worker when a process dies before completion.

## Data Flow

1. API or MCP creates a generation task row.
2. Existing API path may start a daemon thread immediately.
3. Durable worker can also claim queued or expired rows.
4. Worker marks lease fields, computes resume point, and calls the existing
   generation/continue execution path.
5. Each chapter writes hard-floor and pulp-beat decision events.
6. Accepted chapters with extraction degradation record deferred maintenance.
7. Pressure report reads task, chapter, decision, draft, prompt, span, and trope
   telemetry without mutating state.

## Error Handling

- Fatal writer, hard-floor, canon, review, or fatal pulp beat failures stop the
  current run.
- Missing visible payoff is warning-only until the configured consecutive miss
  threshold is reached.
- Memory-index and structured-extraction degradation are deferred maintenance
  when the chapter body and acceptance state are already durable.
- Worker lease loss prevents heartbeat refresh and returns a failed worker
  result without mutating unrelated tasks.
- Expired leases are claimable by a new worker; non-expired leases are not.

## Tests

- ORM/default test for `Project.target_total_chapters == 50`.
- Pulp beat event recording test for accepted chapters.
- Consecutive missing payoff failure test under pulp/factory policy.
- Standard-profile warning-only test.
- Deferred structured extraction event test after accepted degraded output.
- Pressure summary test for slopes, p95, payoff missing rate, extraction failure
  rate, and trope repetition fields.
- Worker claim/resume test for queued tasks and expired running tasks.

## Completion Definition

This pass is complete when focused tests pass, compile succeeds, strict legacy
inventory audit passes, and no new legacy terms or compatibility paths are
introduced by the touched files.
