# P1 Long-Run Correctness Design

## Scope

This spec covers the P1 correctness bundle after the P0 long-run fixes:

- Include `retrieved_memories` in context-budget trimming.
- Split trope usage into planned and accepted usage so planning retries do not pollute cooldown.
- Add lightweight pressure-report counters for P0/P1 long-run risks.

P2 pulp cost/verifier work and P3 macro evidence-chain work are explicitly out of scope for this pass.

## Context Budget Design

`RetrievalBroker._pick_memories` should continue to use the typed retrieval budget. That budget controls what enters the pack. The missing behavior is eviction when the assembled pack exceeds `context_budget_chars`.

`RetrievalBroker._trim_pack` will include `retrieved_memories` in the eviction loop. Eviction order:

1. `active_relations`
2. low-priority `retrieved_memories`
3. `active_entities`, keeping at least 3
4. `active_threads`, keeping at least 1
5. `previous_chapter_summaries`, keeping at least 1
6. `world_context.relevant_world_pages`, keeping at least 1

Memory priority is type-aware. `recent` memories are removed first, then `relationship` and `world`, then `enemy`, `wealth_status`, and `promise`. This preserves obligation-like memory longer than ordinary recency recall while still allowing memory to shrink under the total context budget.

The broker observability summary will include:

- `memories_count_before`
- `memories_count_after`
- `pruned_memories`

## Trope Usage Design

Use the existing `trope_usage_records` table with a new `usage_stage` field instead of introducing a second table. Valid stages:

- `planned`: written when a band plan schedules a reward template.
- `accepted`: written after a chapter containing the trope is accepted.

Historical rows default to `accepted` so existing cooldown behavior remains stable after migration.

`recent_trope_usage()` will default to `usage_stage="accepted"`. Band planning may still write planned records through `save_trope_usage_records`, but planned rows must not count as accepted cooldown facts. This prevents replan/retry schedules from suppressing templates that never reached accepted story text.

Accepted usage should be recorded from accepted chapter experience overlays. The accepted writer path already has the accepted `ChapterPlan.experience_plan_json`; it should save usage for that chapter's selected reward/template IDs when the chapter is marked accepted. The write must be idempotent by project, chapter, template, and stage so retries do not duplicate accepted usage.

## Pressure Report Design

Pressure reporting remains a read-only telemetry script. It will add summary counters that can be computed from existing rows:

- `planned_trope_usage_count`
- `accepted_trope_usage_count`
- `canon_commit_failed_count`
- `generation_worker_heartbeat_failed_count`
- `failed_chapter_stop_count`
- `context_memory_pruned_count`

`context_memory_pruned_count` comes from `CONTEXT_PRUNED` decision-event payloads when available. The P1 implementation should not add high-frequency success events just to count normal worker heartbeats; heartbeat failures already have a DecisionEvent and successful lease refresh remains visible in task lease fields.

## Data Model

Add `TropeUsageRecord.usage_stage` with default `"accepted"`.

Add a migration after the current latest migration. It should:

- add `usage_stage` to `trope_usage_records`,
- backfill existing rows to `"accepted"`,
- add an index that supports recent accepted lookup by project/stage/created_at,
- add a uniqueness guard for idempotent usage writes if compatible with existing rows.

If a strict unique index risks breaking existing historical duplicates, implement idempotency in `save_trope_usage()` by checking for an existing project/chapter/template/stage row before insert, and add only a non-unique lookup index.

## Error Handling

Memory trimming must never empty all context categories just to satisfy an impossible tiny budget. It should stop once each protected category reaches its floor.

Trope usage writes are planning/observability metadata. A planned usage write failure should fail the band planning transaction as it does today. Accepted usage write failures should be treated like post-acceptance metadata failures: they should be visible in tests and logs, but must not undo a canon-accepted chapter unless the caller already runs it in the same transaction.

Pressure report counters must default to `0` when rows or event payloads are absent.

## Tests

Add focused tests for:

- context trimming removes low-priority memories when memories push the pack over budget,
- promise/enemy/wealth memories survive longer than recent memories,
- broker summary reports memory pruning,
- planned trope usage does not appear in default `recent_trope_usage`,
- accepted trope usage appears in default `recent_trope_usage`,
- saving the same accepted trope usage twice is idempotent,
- pressure report includes planned/accepted trope counters and P0/P1 risk counters.

## Out Of Scope

P2 remains responsible for reducing pulp single-mode LLM calls and replacing the single keyword verifier with track-aware pulp contracts.

P3 remains responsible for deriving macro status from canon evidence and making missed arc boundary audits safe.
