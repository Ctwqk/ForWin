# ForWin Schema Ownership Design

## Decision

Document logical schema ownership before any physical database split. Production
ForWin remains on the shared 150-hosted Postgres/Qdrant/MinIO layer.

Ownership is an application contract: which domain is allowed to create,
mutate, or retire a state family. It is not a new database, schema namespace, or
permission system in this phase.

## Owner Domains

- generation task state
- BookState/canon state
- review/governance state
- publisher runtime state
- knowledge/projection state
- observability/artifact state
- outbox event state
- MCP workflow state through API tools only

## Enforcement

This phase adds narrow source-boundary tests where the current code can support
them without rewriting legacy ownership paths. The most important immediate
guardrail is that new outbox state remains owned by outbox modules and approved
producer/handler adapters, not arbitrary API or worker code.

## Physical Split Order

Physical database candidates are evaluated only after logical ownership is
stable:

1. publisher runtime data
2. observability/artifact metadata
3. knowledge index metadata
4. generation task history
5. BookState/canon

BookState/canon is last because it is the correctness core of chapter
acceptance.

## Non-Goals

- Do not create a schema migration for a physical split in this phase.
- Do not add PostgreSQL schemas or cross-database links.
- Do not block legacy read paths that still need shared state.
- Do not remote-split canon, review, Genesis, or MCP.
