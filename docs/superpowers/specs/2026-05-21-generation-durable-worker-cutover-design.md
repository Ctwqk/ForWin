# Generation Durable Worker Cutover Design

## Context

The thousand-chapter readiness work added durable task primitives:
`GenerationTask` lease fields, `claim_generation_task()`,
`heartbeat_generation_task()`, `generation_task_resume_from_chapter()`, and
`run_one_generation_task()`. Those pieces are tested, but the production API
path still starts generation with daemon threads from
`forwin/api_core/generation.py`.

That means the system has two incompatible execution authorities:

- API request handlers create a task and immediately execute it in-process.
- The lease worker can claim queued or expired tasks, but nothing starts it in
  production.

For long-running thousand-chapter generation, this is not durable enough. A
process restart can still kill in-flight daemon work, and simply starting a
worker alongside the old API path would risk duplicate execution.

## Decision

Use the external-worker-only cutover.

The API becomes enqueue-only for generation work. It persists a generation task
and returns the task id. It does not start a daemon generation thread. A separate
worker process is the only component allowed to claim a generation task lease
and run generation.

This deliberately does not keep an inline compatibility executor. Local
development, MCP workflows, and production deployments must run a generation
worker when they expect queued generation tasks to execute.

## Goals

- Remove daemon-thread generation execution from the API production path.
- Make DB lease claim the single authority for generation execution.
- Add a durable worker CLI that can run once or poll continuously.
- Ensure restarted tasks are reclaimed through expired/requeued leases.
- Ensure resume information is not computed and then ignored.
- Preserve existing task, project, Genesis, MCP, and UI request contracts where
  possible: calls still return a task id, and clients continue polling task
  state.
- Avoid new legacy compatibility paths or alternate task tables.

## Non-Goals

- No Celery, Kafka, Temporal, Redis queue, or external scheduler dependency.
- No inline worker fallback in the API.
- No parallel writing of adjacent chapters.
- No direct SQLite or ad hoc task-table mutation outside existing persistence
  helpers.
- No redesign of Genesis or project handoff workflows.
- No unrelated update to the stale thousand-chapter design document in this
  cutover pass.

## Architecture

### API Enqueue Boundary

`_create_generation_task()` and `_create_continue_generation_task()` remain the
API-side factories for new generation tasks. They continue to:

- validate that the project has no active generation task
- create the task record
- persist `project_id`, `requested_chapters`, `max_chapters`, and
  `run_until_chapter`
- create the root governance event
- return the task id

They stop doing this:

- starting `threading.Thread(..., daemon=True)`
- calling `_run_generation_with_config()` directly
- calling `_run_continue_project_with_config()` directly

New generation task records should be created in `queued` state. `starting`
should no longer be used for newly enqueued generation tasks. Existing restart
recovery may still normalize old non-terminal rows to `queued`.

### Worker Ownership

The worker process calls `run_one_generation_task()`.

For each claim attempt:

1. `claim_generation_task()` selects one claimable generation task with
   `SKIP LOCKED`.
2. The task is marked `running`, with `lease_owner`, `lease_expires_at`, and
   `heartbeat_at` set in the same transaction.
3. The worker computes `resume_from_chapter`.
4. The worker dispatches to the correct executor:
   - project task: continue existing project
   - non-project task: initial generation path
5. The executor updates task progress through DB-backed task updates.
6. The worker refreshes heartbeat at safe points and after execution.

The worker is allowed to reclaim `running` tasks only when their lease has
expired. It must not claim an actively leased task.

### Worker CLI

Add a CLI entry point, preferably under existing `forwin` CLI structure:

```text
forwin generation-worker --worker-id worker-1 --poll-interval 2 --lease-seconds 300
forwin generation-worker --once --worker-id smoke-test-worker
```

Required behavior:

- `--once` claims at most one task and exits with success when no work is
  available.
- polling mode repeatedly calls `run_one_generation_task()` and sleeps when no
  task is claimable.
- `--worker-id` defaults to a stable process-local id containing hostname and
  pid.
- `--lease-seconds` defaults to 300 and clamps to the same minimum as
  `heartbeat_generation_task()`.
- SIGINT/SIGTERM exits the loop cleanly after the current task attempt returns.

Scripts may wrap the CLI later, but the CLI is the canonical operational entry.

### Resume Semantics

The existing worker computes `resume_from_chapter`, but the default executors
currently discard it. This cutover must close that gap.

For continue tasks, execution must honor:

- explicit `task.resume_from_chapter` when set
- failed or paused chapter blockers when present
- otherwise `max(completed_chapters) + 1`
- `task.max_chapters` and `task.run_until_chapter`

The preferred implementation is to pass the resume point into the continue
workset selection path, so the workset starts at or after the lease-derived
resume point and never silently replays earlier accepted chapters.

If the current orchestrator already derives the same next chapter from durable
chapter state, tests must still prove that an explicit `resume_from_chapter`
changes the selected workset. A computed value that has no behavioral effect is
not acceptable for this cutover.

### Auto-Continue

Auto-continue keeps creating the next task through
`_create_continue_generation_task()`. Because that factory becomes enqueue-only,
auto-continue will enqueue the next task and return. A worker will claim the next
task in a later loop iteration.

This keeps auto-continue durable: process death between task creation and
execution leaves a queued task in the DB instead of a lost daemon thread.

### Restart Recovery

The current recovery behavior is mostly aligned with the cutover:

- cancel-requested tasks become `cancelled`
- pause-requested tasks become `paused`
- tasks with failed chapters remain failed blockers
- resumable non-terminal tasks are requeued with cleared lease owner and an
  expired lease timestamp

The cutover should retain this policy. The main change is that requeued tasks
now have a real worker process to claim them.

## Data Flow

```text
API / MCP / automation request
  -> create generation task row with status=queued
  -> return task_id

generation worker loop
  -> claim queued or expired task with DB lease
  -> compute resume_from_chapter
  -> execute generation/continue path
  -> update task progress and heartbeat
  -> completion handler may enqueue next auto-continue task

client/UI/MCP
  -> poll task state as before
```

## Error Handling

- If no worker is running, tasks remain `queued`. This is expected and visible.
- If a worker crashes, the task remains `running` until its lease expires; a new
  worker can reclaim it.
- If a task has failed chapters, restart recovery keeps it failed instead of
  skipping forward.
- If the worker loses the lease, heartbeat refresh fails and the worker must not
  mutate a task it no longer owns.
- Pause and cancel requests remain task flags read by the generation runtime.
- Worker execution exceptions mark the owned task failed with a worker-specific
  error and preserve enough task metadata for later inspection.

## Operational Contract

Production must run at least one generation worker process whenever generation
tasks should execute.

Example deployment shape:

```text
forwin-api.service
  runs FastAPI only

forwin-generation-worker.service
  runs forwin generation-worker --poll-interval 2 --lease-seconds 300
```

Multiple workers may run against the same Postgres database. `SKIP LOCKED` and
lease expiry prevent duplicate claims. The system still should not parallelize
adjacent chapter writing for one project; the existing one-active-generation-task
constraint remains the project-level guard.

## Tests

Focused tests should cover:

- `_create_generation_task()` persists a queued task and does not call
  `threading.Thread.start()`.
- `_create_continue_generation_task()` persists a queued task and does not call
  `threading.Thread.start()`.
- worker CLI `--once` exits cleanly when there is no claimable task.
- worker CLI `--once` claims and executes one queued task using the DB lease.
- expired running task can be reclaimed by a different worker.
- non-expired running task cannot be reclaimed.
- restart recovery requeues resumable running tasks and a worker claims them.
- auto-continue enqueues a next task rather than executing it inline.
- explicit `resume_from_chapter` affects continue workset selection.
- API/MCP-facing task summaries still expose queued/running/completed/failed
  states correctly.

Regression tests should also assert that generation API paths no longer start
daemon generation threads. Other daemon threads, such as automation scheduler or
transport internals, are outside this cutover.

## Completion Definition

This cutover is complete when:

- no generation API path starts `_run_generation_with_config()` or
  `_run_continue_project_with_config()` in a daemon thread
- a documented worker CLI is available and tested
- queued tasks execute through lease claim in tests
- restart recovery plus worker claim is covered end to end
- resume points have behavioral effect
- focused tests pass
- compile checks pass
- strict legacy audit remains clean
