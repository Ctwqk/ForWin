# Generation Worker Observability Design

## Context

The durable generation worker cutover made DB lease claim the only execution
authority for generation tasks. The generated chapters still pass through the
existing task runtime, so chapter execution already emits `task_operation_*`
DecisionEvents and performance spans. The worker layer itself is only partially
observable:

- `forwin.generation.worker` logs failures with stdlib logging.
- `forwin.generation.worker_cli` logs successful execution with stdlib logging.
- `api_runtime.run_orchestrator_task()` still records spans with
  `component="api"` even when called by a worker.
- Lease claim, expired lease reclaim, heartbeat failure, and worker loop state
  are not first-class task observations.

This design adds structured worker observability without introducing another
task runner or a separate logging backend.

## Goals

- Make generation worker claim/reclaim/execution ownership visible in the
  existing DecisionEvent and PerformanceSpan systems.
- Mark worker-executed task spans as `component="worker"` instead of `api`.
- Preserve current task execution behavior and failure semantics.
- Avoid noisy DB writes for every empty poll or successful heartbeat.
- Keep observability failures non-fatal.
- Avoid logging secrets from execution payloads or runtime config.

## Non-Goals

- No dashboard, UI redesign, alerting system, metrics exporter, or queue-depth
  graph in this pass.
- No new external observability dependency.
- No per-loop DecisionEvent for empty polling.
- No DecisionEvent for non-project tasks until a project id exists.
- No change to the external-worker-only execution contract.

## Architecture

### Worker Observability Helper

Add a focused helper module, `forwin/generation/worker_observability.py`.

Responsibilities:

- Record worker-scoped DecisionEvents for task rows that have a project id.
- Create lightweight PerformanceSpans around lease claim and worker execution
  boundaries.
- Emit stdlib logs for process-level lifecycle events that are not project
  scoped, such as empty polls and worker loop stop.
- Swallow and debug-log observability failures so task execution is not blocked.

The helper should accept `session_factory`, `task_id`, `project_id`,
`worker_id`, and a small payload. It must not receive or log full runtime config
or secret-bearing payload fields.

### DecisionEvent Types

Add the following `DecisionEventType` values:

- `GENERATION_WORKER_CLAIMED = "generation_worker_claimed"`
- `GENERATION_WORKER_RECLAIMED = "generation_worker_reclaimed"`
- `GENERATION_WORKER_HEARTBEAT_FAILED = "generation_worker_heartbeat_failed"`
- `GENERATION_WORKER_EXECUTION_FAILED = "generation_worker_execution_failed"`

Successful task execution continues to use existing `TASK_OPERATION_SUCCEEDED`.
Worker completion should not duplicate that event.

Payload fields:

```json
{
  "worker_id": "host:pid",
  "lease_expires_at": "...",
  "resume_from_chapter": 12,
  "previous_lease_owner": "old-worker",
  "previous_lease_expires_at": "...",
  "claim_kind": "queued|expired_running",
  "execution_mode": "initial|continue"
}
```

Only include previous lease fields when reclaiming an expired running task.

### Performance Spans

Change `api_runtime.run_orchestrator_task()` to accept:

```python
component: str = "api"
```

Use that component for existing `task.operation` and `task.cleanup` spans.
API callers keep the default. Worker callers pass `component="worker"`.

Add worker-level spans:

- `generation_worker.claim`
- `generation_worker.execute`

These spans should include tags or metrics for:

- `worker_id`
- `task_id`
- `project_id`
- `claimed`
- `claim_kind`
- `resume_from_chapter`
- `lease_seconds`

If the existing observability service is unavailable, the helper falls back to
`NullObservability`.

### Lease Claim Data

`claim_generation_task()` currently returns only the mutated `GenerationTask`.
To distinguish a normal queued claim from an expired running reclaim, add a
small return model or attach transient metadata before mutation:

```python
class GenerationTaskClaimResult(BaseModel):
    task: GenerationTask
    claim_kind: Literal["queued", "expired_running"]
    previous_lease_owner: str = ""
    previous_lease_expires_at: datetime | None = None
```

`run_one_generation_task()` should consume this result, record the worker claim
event, then proceed as it does today.

### Heartbeat Observability

Do not record a DecisionEvent for every successful heartbeat. It is too noisy
for long-running generation.

Record `GENERATION_WORKER_HEARTBEAT_FAILED` when:

- a worker tries to heartbeat a task it no longer owns
- the task is no longer `running`
- the task row is missing

This is a task-scoped warning event when project id is available, and a stdlib
warning otherwise.

### Worker CLI Logs

`run_generation_worker_loop()` should log:

- startup: worker id, lease seconds, poll interval, once mode
- no task claimed: debug-level only
- task claimed/executed: info-level with task id, project id, resume chapter
- graceful stop: info-level
- loop exception: exception-level before propagating or exiting

These logs are process logs, not DecisionEvents.

## Data Flow

```text
worker loop
  -> logs startup
  -> claim_generation_task()
  -> record generation_worker.claim span
  -> if no task: debug log only
  -> if task claimed:
       record generation_worker_claimed or generation_worker_reclaimed
       run worker executor
         -> run_orchestrator_task(component="worker")
         -> existing task_operation_* DecisionEvents
         -> existing task.operation/task.cleanup spans with component=worker
       heartbeat
       if heartbeat fails:
         record generation_worker_heartbeat_failed
```

## Error Handling

- Observability write failures are ignored after debug logging.
- Worker execution exceptions still mark the owned task failed as they do now.
- The worker additionally records `generation_worker_execution_failed` when
  project id is available.
- Missing project id does not block execution or logging; stdlib logging is used
  until task/project linkage exists.
- No full execution payload, API key, publisher secret, or bridge token is
  written into logs or DecisionEvents.

## Tests

Focused tests should cover:

- queued project task claim records `generation_worker_claimed`.
- expired running task reclaim records `generation_worker_reclaimed` with
  previous lease owner metadata.
- worker runtime calls produce `PerformanceSpan` rows with `component="worker"`.
- API/default runtime calls still produce `component="api"`.
- heartbeat ownership failure records `generation_worker_heartbeat_failed`.
- worker observability helper swallows logging failures and does not fail task
  execution.
- worker CLI logs startup/no-claim/stop through stdlib logging without writing
  DB events for no-claim loops.

## Completion Definition

This pass is complete when worker claim/reclaim/failure events are visible in
DecisionEvents for project-backed tasks, worker task spans are marked
`component="worker"`, focused tests pass, compile checks pass, and the legacy
inventory audit remains clean.
