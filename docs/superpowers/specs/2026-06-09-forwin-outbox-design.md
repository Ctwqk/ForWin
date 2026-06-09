# ForWin Outbox Design

## Decision

Add a small Postgres-backed outbox for eventually consistent side effects. The
outbox is not a replacement for `generation_tasks`, publisher job tables, or the
synchronous BookState/canon compile path.

## Scope

The first outbox slice adds:

- `outbox_events` table and ORM model.
- Store helpers for enqueue, claim, complete, and fail/retry.
- A worker loop with event-type handlers.
- `forwin outbox-worker --once` CLI entrypoint.

## Non-Goals

- Do not move BookState/canon compile to outbox.
- Do not replace generation task leasing.
- Do not add Kafka, Celery, Temporal, or Redis.
- Do not move existing side effects in this first slice.

## Data Model

`outbox_events` stores:

- `id`
- `event_id`
- `aggregate_type`
- `aggregate_id`
- `event_type`
- `payload_json`
- `status`
- `attempts`
- `available_at`
- `locked_by`
- `locked_at`
- `processed_at`
- `error_message`
- `created_at`
- `updated_at`

Pending events are claimed with row locks. Processed events are terminal. Failed
events are terminal after retry budget is exhausted.

## Error Handling

If no handler exists for an event type, the worker treats it as a handler error
and applies the same retry/failure policy. If the outbox worker is not running,
pending events remain visible and do not block core generation/canon paths.

## Tests

- enqueue stores JSON payloads.
- claim skips unavailable events.
- worker processes handled events.
- worker retries failed events until retry budget is exhausted.
- CLI exposes `outbox-worker --once`.
