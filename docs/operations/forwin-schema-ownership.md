# ForWin Schema Ownership

This document defines logical schema ownership for the ForWin modular monolith.
ForWin still uses the shared production Postgres/Qdrant/MinIO layer on 150.
Ownership here means application write authority, not a physical database,
PostgreSQL schema namespace, or separate service.

## Logical Schema Ownership

| State family | Owner domain | Write rule |
| --- | --- | --- |
| generation task state | Generation task port, web API enqueue/control, generation worker lease/progress | API creates and controls tasks; generation worker claims, heartbeats, and completes tasks |
| BookState/canon state | CanonPort and BookState repository/compiler | Accepted graph deltas, canon snapshots, and admission results stay synchronous in the correctness path |
| review/governance state | ReviewPort, governance APIs, generation runtime | Review verdicts and governance decisions remain part of the controlled writing workflow |
| publisher runtime state | Publisher runtime and publisher worker | Upload, comment sync, cover, connection, and browser-session state is mutated through publisher runtime APIs |
| knowledge/projection state | KnowledgeIndexPort, projection jobs, and future knowledge worker | Rebuild/index/projection refresh work may use outbox when eventual consistency is acceptable |
| observability/artifact state | Observability and artifact ports, plus maintenance/outbox workers | Retention, indexing, and artifact maintenance are side effects, not generation correctness gates |
| outbox event state | Outbox store and outbox worker | Producers enqueue through outbox store helpers; handlers process events idempotently where possible |
| MCP workflow state | MCP workflow state through API tools only | MCP must not write project, task, chapter, or canon tables directly |

## Enforcement Guardrails

- Boundary tests should prefer source scans around stable ownership seams over
  broad bans that would freeze known legacy read paths.
- New outbox event state is owned by `forwin/outbox/*`, `forwin/models/outbox.py`,
  and approved producer/handler adapters such as
  `forwin/knowledge_system/projection_jobs.py`.
- Publisher browser automation should use backend API calls and heartbeat
  checks; it should not own direct database writes.
- MCP should call API/MCP workflow tools and should not import SQLAlchemy model
  or session helpers.
- BookState/canon and chapter acceptance must not move to outbox or a remote
  service without a separate design.

## Physical Database Split Candidates

Physical database splits are future work. A separate design is required before any physical database split, including migration, rollback, idempotency, failure behavior, backup/restore, and operator checks.

Evaluate candidates only after logical ownership has held under production
operation, in this order:

1. publisher runtime data
2. observability/artifact metadata
3. knowledge index metadata
4. generation task history
5. BookState/canon

BookState/canon is last because it is the correctness core for accepted chapter
truth.

## Non-Goals

- Do not create physical split migrations in this phase.
- Do not add PostgreSQL schemas or physical database links in this phase.
- Do not split shared production Postgres away from the 150 data layer in this
  phase.
- Do not treat this document as permission to bypass ForWin API, MCP, task, or
  chapter workflow tools.
