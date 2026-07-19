# Generation Task Single Truth Design

## Context

The sixth immutable V4 matrix exposed a split-brain generation task model.
Generation workers wrote terminal state to PostgreSQL, the API process retained an
older nonterminal copy in `HttpRuntime.tasks`, and automatic six-hour database
retention later removed the terminal row. Active-task detection then treated the
cache-only copy as a running task even though its lease had expired and successor
tasks had completed.

The immutable failure evidence is stored at:

```text
.artifacts/v4-matrix/failed-db5dfcf-task-cache-ghost/
```

This run is not release evidence and must be restarted from fresh projects after
the defect is fixed.

## Decision

PostgreSQL is the only source of truth for generation tasks.

- Delete the API generation-task cache and all cache/DB merge rules.
- Delete automatic age/count pruning of durable generation-task rows.
- Keep explicit task deletion as the only retention mutation.
- API task reads, lists, active checks, pause, terminate, and delete operations use
  one database transaction and never synthesize a missing row.
- Workers remain the only owner of execution progress and terminal completion.
- API startup does not rewrite interrupted task state.
- A worker may reclaim an expired `running` task even when pause or cancel was
  requested. It acknowledges the request directly as `paused` or `cancelled`
  before constructing a generation pipeline.

No migration for existing task rows or process-local cache state is required.

## Alternatives Rejected

### Synchronize the existing cache

Adding cache invalidation, lease-aware merge precedence, or terminal tombstones
would preserve two authorities and add more reconciliation code. It would also
leave task history vulnerable to process lifetime and retention timing.

### Keep a read-through cache only

A read-through cache would save little at the current task volume while retaining
staleness, invalidation, and restart semantics. PostgreSQL already provides the
required indexed reads.

### Mark cache-only tasks inactive after lease expiry

This masks the observed symptom but leaves list/get disagreement, missing audit
history, and future cache resurrection paths. It is not an architectural fix.

## Components

### HTTP Runtime And Task Center

`HttpRuntime` no longer owns task dictionaries, locks, retention timers, or task
cache limits. `TaskCenterService` loads and lists `GenerationTask` rows directly.
Database errors propagate; there is no process-memory fallback.

The task update path locks the existing row, applies the existing state-transition
normalization, and commits once. A missing row remains missing. Explicit deletion
sets `deleted_at`; read paths exclude it.

### Active Task Detection

`_active_generation_task_ids` queries only non-deleted PostgreSQL rows whose status
is not terminal. `pause_requested` and `cancel_requested` do not make a running
task terminal until a worker acknowledges them.

### Worker Recovery

The lease query distinguishes two cases:

1. A `queued` task is claimable only when neither pause nor cancel is requested.
2. An expired `running` task is claimable regardless of those request flags.

After reclaim, `GenerationApplicationService.execute_claimed` checks the durable
flags. It writes `paused` or `cancelled` immediately and returns without building
a pipeline. Normal expired tasks continue through the existing Canon-safe recovery.

### Startup

API startup no longer calls `recover_interrupted_generation_tasks`. Lease expiry and
worker reclaim are the durable recovery protocol; API process lifetime has no task
state semantics.

## Invariants

- A task absent from PostgreSQL cannot appear in task get/list/active-check output.
- Terminal rows survive elapsed wall time and list polling until explicit deletion.
- A missing task is never recreated by pause, terminate, delete, or internal update.
- An expired pause/cancel request reaches a terminal status without LLM or pipeline
  work.
- Existing Canon commit recovery remains fenced by `lease_owner` and `lease_epoch`.
- No compatibility alias, fallback cache, sweeper, or new workflow framework is
  introduced.

## Verification

- Regression: an old terminal row remains readable and is not physically pruned.
- Regression: active-check ignores no state outside PostgreSQL.
- Regression: an update against a missing task does not create a row.
- Recovery: expired `running + pause_requested` becomes `paused` without pipeline
  execution.
- Recovery: expired `running + cancel_requested` becomes `cancelled` without
  pipeline execution.
- Architecture scan: removed cache/prune symbols have zero production references.
- Focused task persistence, task center, worker, Canon recovery, MCP, and project
  guard suites pass, followed by Ruff, compileall, and the full suite.
