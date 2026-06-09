# ForWin Service Process Roadmap

This document is the operator-facing summary of the service-process roadmap in
`docs/superpowers/specs/2026-06-08-forwin-service-process-roadmap-design.md`.

The target architecture is a modular monolith with explicit service processes,
shared production infrastructure, and clear write boundaries. It is not an
immediate full microservice conversion.

## Target Shape

ForWin keeps one codebase and the shared production Postgres/Qdrant/MinIO layer
while running role-specific processes:

- `web-api / ui-gateway`
- `generation-worker`
- `forwin-mcp`
- `publisher-worker`
- `publisher-browser`
- future `knowledge-worker`
- future `outbox-worker`

Genesis/planning, review/governance, and BookState/canon stay in-process until
their port contracts, idempotency behavior, and recovery behavior are proven.

## Logical Write Ownership

Ownership means logical write authority, not physical database ownership. The
roadmap keeps a shared database while documenting which process or port is
allowed to mutate each state family.

| State family | Logical writer | Notes |
| --- | --- | --- |
| generation task enqueue/control | web API through generation task port | Creates, pauses, cancels, and reports task state |
| generation task lease/progress | generation-worker | Claims queued or expired tasks and refreshes heartbeat |
| BookState/canon writes | CanonPort | The only intended entry for accepted graph deltas and snapshots |
| review/governance results | ReviewPort and generation runtime | Review stays synchronous in the generation correctness path |
| publisher upload/comment/cover jobs | Publisher runtime, publisher-worker, publisher-browser API calls | Browser automation should not write the database directly |
| MCP operations | forwin-mcp through HTTP API | MCP must not bypass workflow APIs or inspect raw database state |
| knowledge index writes | KnowledgeIndexPort, later knowledge-worker | Expensive rebuild/index work can become async before read APIs split |
| observability/artifacts | Observability and artifact ports, later maintenance/outbox workers | Retention and indexing can become async side effects |

## Phase Summary

### Phase 0: Boundary Audit

Document ownership, add boundary tests, and keep an explicit temporary allowlist
for the current `generation.worker -> api_core.generation` auto-continue
dependency.

### Phase 1: Production Process Split

Make existing process boundaries operational: API, generation worker, MCP,
publisher worker, and optional publisher browser. All production processes use
the 150 data layer.

### Phase 2: Internal Ports

Add generation, canon, review, publisher, and knowledge ports. Remove the
generation worker's dependency on `forwin.api_core.*` and make runtime creation
more role-aware.

### Phase 3: Postgres Outbox

Add an outbox for eventually consistent side effects such as artifact indexing,
knowledge rebuild requests, and non-blocking projection refresh. BookState/canon
compile remains synchronous.

### Phase 4: Image Role Split

Split images only after role contracts are stable. Start with
`publisher-browser`, then slim `web-api`, and only split MCP or knowledge images
when operational value is clear.

### Phase 5: Schema Ownership

Document schema ownership before considering physical database splits. Candidate
physical splits come later, with BookState/canon last. See
`docs/operations/forwin-schema-ownership.md` for the owner domains, enforcement
guardrails, and split-order constraints.

## Guardrails

- Do not add a remote HTTP service for every FastAPI route group.
- Do not move BookState/canon compile to outbox without a separate design.
- Do not add Kafka, Celery, Temporal, Redis queue, or another scheduler for the
  generation queue in this roadmap.
- Do not run routine generation control by killing containers; use task
  pause/cancel flags.
- Do not expose MCP or publisher remote debugging outside trusted operator
  paths.
