# ForWin Service Process Roadmap Design

## Context

ForWin is a long-form Chinese web novel generation and publishing system. Its
runtime is already broader than a single web server: the repository contains a
FastAPI API/UI gateway, a durable generation worker, an MCP gateway, publisher
browser automation, publisher backend jobs, BookState canon admission,
review/governance, retrieval, observability, and local infrastructure profiles.

The current production topology is distributed but not microservice-oriented:

- `10.0.0.246:/home/kikuhiko/ForWin` is the source-of-truth development
  checkout.
- `10.0.0.126:/Users/magi1/ForWin-swarm` is the production deploy output.
- `forwin-app-swarm` and `forwin-mcp-swarm` currently run on 126.
- Postgres, Qdrant, and MinIO are centralized on `10.0.0.150`.

The local Compose stack already has the natural process boundaries needed for
the first step: `forwin`, `generation-worker`, `forwin-mcp`, and the
profile-gated `publisher-browser`. The Dockerfile is still an all-purpose image:
it builds the frontend, installs backend code, includes Chromium/Playwright,
copies the browser extension, and defaults to `uvicorn forwin.api:app`.

The durable generation queue is also already real. `forwin/generation/task_lease.py`
claims queued or expired `GenerationTask` rows with `FOR UPDATE SKIP LOCKED`,
sets `lease_owner`, refreshes heartbeat/lease expiry, and respects
pause/cancel flags. `forwin/generation/worker.py` executes claimed work and
keeps periodic heartbeats. The API daemon-thread cutover has already happened:
`forwin/api_core/generation.py` no longer contains generation `threading.Thread`
starts.

The remaining architecture problem is therefore not "invent a microservice
system." It is to make the existing process boundaries production-grade, reduce
cross-boundary imports and startup coupling, and create a safe path toward later
outbox and image splits.

## Decision

ForWin should continue as:

```text
modular monolith
+ service-process split
+ shared Postgres/Qdrant/MinIO
+ explicit write boundaries
+ Postgres outbox for selected asynchronous side effects
```

ForWin should not be split into full independent microservices now.

The target state keeps one codebase and one shared production data layer while
running a small set of role-specific processes:

- `web-api / ui-gateway`
- `generation-worker`
- `forwin-mcp`
- `publisher-worker`
- `publisher-browser`
- later `knowledge-worker`, `outbox-worker`, and maintenance workers

Genesis/planning, review/governance, BookState/canon, and most read APIs remain
inside the monolith during the roadmap. They get port interfaces, contract
tests, and stricter write ownership before any remote service split is
considered.

## Goals

- Make the existing process split explicit in production and local operations.
- Keep strong chapter-generation and canon correctness paths debuggable.
- Ensure exactly one authority writes each critical state family.
- Stop accidental API-to-worker and worker-to-API dependency growth.
- Add service-boundary tests before adding more deployment boundaries.
- Introduce Postgres outbox only for eventually consistent side effects.
- Defer image and schema/database splits until the contracts are stable.

## Non-Goals

- No full microservice conversion in one pass.
- No split physical database for BookState/canon, generation, or review in the
  early phases.
- No Kafka, Celery, Temporal, Redis queue, or external scheduler dependency for
  generation.
- No HTTP service wrapper around each FastAPI route group.
- No adjacent-chapter parallel writing for the same project.
- No remote BookState/canon service until idempotency, retry, and transaction
  boundaries are proven.
- No UI-only hosting split as a first-priority stability task.

## Current Boundary Map

### Already Process-Shaped

| Boundary | Current evidence | Target treatment |
| --- | --- | --- |
| `web-api / ui-gateway` | `forwin.api:app`, `forwin/api_core/app.py`, static World Studio assets | Keep as API/UI/read/enqueue gateway |
| `generation-worker` | Compose service, CLI subcommand, DB lease worker | Productionize and make it the only generation executor |
| `forwin-mcp` | Compose service, HTTP MCP app, API client | Keep independent and API-only |
| `publisher-browser` | Compose profile, Chromium/extension scripts, heartbeat checks | Keep isolated; remove DB dependency over time |
| `publisher-worker` | CLI subcommand and `PublisherBackendJobRunner` | Add service/process definition and heartbeat/metrics |
| Postgres/Qdrant/MinIO | Local profiles and production 150 infra | Keep centralized for this roadmap |

### Library/Module Boundaries, Not Services Yet

| Domain | Why not split now | First useful boundary |
| --- | --- | --- |
| Genesis/planning | Handoff and project state are tightly coupled to generation readiness | `GenesisPort` / `PlanningPort` |
| Review/governance | Synchronous correctness path for accepted chapter decisions | `ReviewPort.review_chapter()` |
| BookState/canon | Strong transaction-like writer for graph deltas, snapshots, side effects | `CanonPort.compile(ApprovedGraphDeltaSet)` |
| Retrieval/knowledge | Complex read path; rebuild/index work can be async first | `KnowledgeIndexPort` and later `knowledge-worker` |
| Observability/artifacts | Cross-cutting sinks; query API can stay in API | maintenance/outbox consumers |

## Service Responsibilities

### Web API / UI Gateway

The API owns HTTP request handling, UI assets, auth/CORS, project discovery,
Genesis commands, task creation/control, read views, publisher management APIs,
and lightweight enqueue operations.

It must not execute long-running generation work, browser automation, or
maintenance jobs inline. Startup should eventually avoid constructing resources
needed only by workers.

### Generation Worker

The generation worker owns claiming queued or expired generation tasks,
heartbeats, executing initial/continue generation, progress updates, pause and
cancel checks, and auto-continue follow-up task creation.

It continues to share Postgres/Qdrant/MinIO with the API. The current technical
debt is dependency direction: `forwin/generation/worker.py` still imports
`forwin.api_core.generation._create_continue_generation_task` inside the
completion handler. The roadmap must replace that with a generation task port
so `generation/*` does not depend on `api_core/*`.

### MCP Gateway

The MCP gateway exposes ForWin operator tools and talks through the ForWin HTTP
API. It must not inspect the database or bypass workflow APIs for project,
Genesis, task, or chapter truth. It should stay bound to trusted network paths.

### Publisher Worker

The publisher worker owns backend publisher jobs such as cover generation. It
claims jobs with row locking and writes results through publisher runtime
services.

It should become an explicit local and production process, separate from the
API. It does not own browser login state or real browser interactions.

### Publisher Browser Worker

The browser worker owns Chromium/extension/Playwright execution, platform login
state, extension heartbeat, upload/comment job claim/result calls, and browser
profile lifecycle.

Its target contract is API-only. The current Compose environment still injects
`FORWIN_DATABASE_URL` into `publisher-browser`; later phases should remove that
dependency and add tests that browser scripts do not import DB/session modules.

### Knowledge Worker

The knowledge worker is a later process for expensive rebuild/index jobs:
LLM-KB rebuilds, projection refresh jobs that do not need to block acceptance,
and optional context-pack precomputation. Read APIs stay in `web-api` until the
data contracts become stable.

### Outbox Worker

The outbox worker is a later process that consumes `outbox_events` from
Postgres. It handles eventually consistent side effects such as artifact
indexing, knowledge rebuild requests, projection refresh, notification-like
events, and selected publisher follow-ups.

It must not make BookState canon compile eventually consistent. Canon admission
stays synchronous until a separate design proves otherwise.

## Data Ownership

Ownership is about write authority, not physical database ownership.

| Data family | Writer authority | Notes |
| --- | --- | --- |
| `generation_tasks` enqueue/control | API through generation task port | API creates and controls; worker claims/runs |
| `generation_tasks` lease/progress | generation-worker | Worker owns running lease state |
| Chapter drafts/review/canon side effects | generation runtime plus canon/review ports | Keep in the strong generation path |
| BookState graph deltas and snapshots | CanonPort | Must become the only write entry |
| Publisher upload/comment/cover jobs | Publisher runtime / worker / browser APIs | Browser should not write DB directly |
| MCP operations | HTTP API only | MCP has no direct DB ownership |
| LLM-KB/Qdrant index writes | KnowledgeIndexPort / knowledge-worker | Read APIs can remain API-side |
| Observability/artifact records | observability/artifact ports | Maintenance can be async |

## Phased Roadmap

### Phase 0: Boundary Audit and Ownership Markers

Phase 0 is documentation and tests around the current shape. It does not change
deployment or data storage.

Deliverables:

- Write an ownership map for generation, canon, publisher, knowledge, and MCP.
- Add import-boundary tests for the most important direction rules.
- Mark existing cross-boundary dependencies that are allowed temporarily.
- Document that BookState/canon and review are not remote services yet.
- Add role vocabulary to docs: API, generation worker, MCP, publisher worker,
  publisher browser, knowledge worker, outbox worker.

The import tests should start narrow and enforceable:

- `forwin/mcp/*` must not import SQLAlchemy models, DB session factories, or raw
  persistence helpers.
- `forwin/generation/*` should not import `forwin.api_core.*`, except for a
  temporary allowlist entry around the auto-continue completion handler.
- Publisher browser scripts should not create DB sessions.

### Phase 1: Production Process Split

Phase 1 makes already-existing workers real operational units.

Deliverables:

- Add or update production Swarm/operation docs so 126 runs
  `forwin-generation-worker-swarm` when generation should progress.
- Keep `forwin-app-swarm` as API/UI/enqueue/read.
- Keep `forwin-mcp-swarm` as MCP gateway.
- Add `forwin-publisher-worker-swarm` or a documented equivalent for backend
  publisher jobs.
- Keep `publisher-browser` optional and controlled.
- Ensure all production processes point at the 150 Postgres/Qdrant/MinIO layer.
- Add operational checks for queued/running generation tasks, worker lease
  freshness, MCP health, and publisher heartbeat.

The generation worker starts with one replica. Multiple workers are allowed for
multi-project throughput, but the one-active-generation-task constraint remains
the project-level guard against same-project concurrent writing.

### Phase 2: Internal Ports and Dependency Direction

Phase 2 reduces the coupling inside the monolith without changing deployment.

Deliverables:

- Introduce generation task port/client APIs for enqueue, active check,
  pause/cancel, claim/progress, and auto-continue creation.
- Remove the worker's import of `api_core.generation` by injecting a task
  creation port into the auto-continue controller.
- Introduce `CanonPort`, `ReviewPort`, `PublisherJobClient`, and
  `KnowledgeIndexPort` with Pydantic request/response models where useful.
- Make `RuntimeContainer` role-aware enough that API startup does not eagerly
  build browser/worker-only resources.
- Expand import-boundary tests as the temporary allowlist shrinks.
- Add contract tests for ports before any remote boundary is added.

This phase is the main software design phase. It should be completed before
outbox or image splits.

### Phase 3: Postgres Outbox and Async Side Effects

Phase 3 adds an outbox for non-critical asynchronous work.

Deliverables:

- Add `outbox_events` with event id, aggregate type/id, event type, payload,
  status, timestamps, attempts, and error metadata.
- Add an outbox worker command that polls with row locks and bounded retries.
- Move selected side effects to outbox:
  - artifact indexing
  - LLM-KB rebuild/index requests
  - projection refresh that does not need to block acceptance
  - publisher follow-up enqueue where eventual consistency is acceptable
  - observability maintenance or retention tasks
- Keep generation task queue and publisher job tables as their own job tables.
- Keep BookState/canon compile synchronous.

The outbox must be optional for the main writing path: if the outbox worker is
down, core generation and canon acceptance should still finish while outbox lag
becomes visible.

### Phase 4: Image and Runtime Role Split

Phase 4 splits images only after process and dependency contracts are stable.

Recommended order:

1. Extract `publisher-browser` into a browser-focused image with Chromium,
   extension assets, and browser scripts.
2. Slim `web-api` so it no longer carries browser-only dependencies.
3. Keep `generation-worker` with writing, review, retrieval, and artifact
   runtime dependencies.
4. Slim `forwin-mcp` into an API-client-only image if the operational benefit is
   worth it.
5. Add role-specific images for `knowledge-worker` and `outbox-worker` only if
   dependency weight or deployment risk justifies them.

No image split should create a second source package or duplicate business
logic.

### Phase 5: Schema and Database Ownership

Phase 5 is conditional. It should happen only if operational scale, team size,
or failure isolation needs prove it worthwhile.

Deliverables:

- Move from implicit shared-table access to documented schema ownership.
- Split physical databases only after schema ownership is stable.
- Prefer early database split candidates in this order:
  1. publisher runtime data
  2. observability/artifact metadata
  3. knowledge index metadata
  4. generation task history
  5. BookState/canon last

BookState/canon is last because it is the correctness core of chapter
acceptance. A remote canon service would require a separate design for retries,
idempotency keys, transaction boundaries, snapshot materialization, and recovery.

## Error Handling and Operations

- A queued generation task with no active worker is not data corruption. It is
  an operational signal that the worker process is missing or unhealthy.
- A running generation task with an expired lease can be reclaimed by another
  worker.
- A non-expired running lease must not be reclaimed.
- Pause/cancel should remain task flags, not routine container kill/restart.
- Worker health should be proven through lease freshness, worker events, and
  task progress, not by pretending worker processes are HTTP services.
- MCP should be exposed only through trusted LAN or tunnels.
- Publisher remote debugging should stay bound to localhost or trusted network
  paths.
- Outbox lag and failed attempts should be observable before any side effect is
  moved to outbox.

## Testing Strategy

### Contract Tests

- Generation task port:
  - enqueue initial/continue
  - active generation check
  - pause/cancel
  - auto-continue follow-up task creation
  - serialization of execution payloads
- CanonPort:
  - accepted GraphDelta input
  - duplicate/idempotent delta behavior
  - snapshot/materialization result
- ReviewPort:
  - chapter review result schema
  - retry/repair instruction result
  - deterministic/LLM mode compatibility
- PublisherJobClient:
  - create batch
  - backend claim/result
  - browser claim/result
  - comment sync ingestion
- MCP:
  - tool response schema stays API-backed
  - no direct DB imports

### Boundary Tests

- `forwin/mcp/*` has no direct DB/model imports.
- `forwin/generation/*` does not depend on `forwin/api_core/*` after the Phase 2
  allowlist is removed.
- publisher browser scripts do not create DB sessions.
- API startup does not instantiate browser-only resources after role-aware
  runtime is introduced.

### Worker Tests

- queued task claim sets lease fields.
- expired running task can be reclaimed.
- non-expired running task cannot be reclaimed.
- pause/cancel-requested queued tasks are skipped.
- heartbeat requires matching lease owner.
- worker crash leaves reclaimable state after lease expiry.
- auto-continue enqueues next task without inline execution.

### Deployment/Smoke Tests

- Local Compose service definitions include API, generation worker, MCP, and
  publisher-browser profile.
- Production docs describe 126 app processes and 150 data endpoints.
- CLI help exposes `generation-worker` and `publisher-worker`.
- A no-work generation worker `--once` run exits cleanly against the test DB.
- MCP health verifies upstream API connectivity.

## Acceptance Criteria

The roadmap is accepted when:

- The architecture decision explicitly rejects immediate full microservice
  conversion.
- All phases 0-5 are documented with deliverables and stop conditions.
- Current-state facts match the repository: durable worker exists, API
  daemon-thread generation is gone, local Compose has process boundaries, and
  production data stores remain centralized.
- The plan identifies the highest-risk existing coupling:
  `generation.worker -> api_core.generation`.
- Tests are defined for contracts, import boundaries, workers, and deployment
  shape.
- The implementation plan can start with Phase 0/1 without forcing later phases
  into the same code change.
