# MinIO Recovery Runner

`minio_recovery.py` is the only MinIO recovery proof path. Each invocation
creates one generic one-chapter fixture through the supported MCP lifecycle and
supports exactly:

- `minio_pre_canon_unavailable`
- `minio_post_canon_unavailable`

## CLI

```bash
export FIXTURE_DATABASE_URL=postgresql://...
export FORWIN_RECOVERY_MINIO_ENDPOINT=minio.example:9000
export FORWIN_RECOVERY_MINIO_ACCESS_KEY=...
export FORWIN_RECOVERY_MINIO_SECRET_KEY=...
export FORWIN_RECOVERY_MINIO_BUCKET=...
export FORWIN_RECOVERY_MINIO_PREFIX=artifacts
export FORWIN_RECOVERY_MINIO_SECURE=false

python .artifacts/rc-candidate/minio_recovery.py run \
  --fault-kind minio_pre_canon_unavailable \
  --fault-id ID \
  --candidate-manifest PATH \
  --mcp-url URL \
  --api-url URL \
  --database-url-env FIXTURE_DATABASE_URL \
  --evidence-dir NEW_EMPTY_PATH
```

Use `minio_post_canon_unavailable` for the post-Canon proof. The database must
be the disposable recovery database. Every run requires a new fault ID and
evidence directory.

The shared `recovery_runner_common.py` owns the Task 4/Task 5 one-chapter MCP
lifecycle, candidate identity, controller client, and atomic evidence writer.
The MinIO runner contains only its own SQL, barrier, MinIO inventory, approval,
and replay behavior. It never calls Docker or writes a business table except
for the exact post-Canon replay operation below.

## Pre-Canon

1. Create and start one chapter through MCP, then read SQL until the candidate
   is `ready_for_canon`.
2. Capture the candidate identity and invoke controller `stop minio`.
3. Send
   `POST /api/projects/{project_id}/chapters/{chapter_number}/review/approve`
   with `continue_generation=false` and the fault/candidate-scoped reason.
4. Require that exact request to fail while SQL still shows zero Canon commits
   and the same candidate identity. A successful request is `setup_blocked`.
5. Invoke controller `start minio`, replay the same immutable request object,
   and wait for one Canon, one processed phase-3 event, four succeeded
   maintenance steps, and the expected world object.

The request identity hashes its method, URL, body, and candidate ID. Both
attempts record the same hash.

## Post-Canon Boundary

The runner executes this order:

1. `setup-hold outbox-worker --hold-id pre-approval`
2. Install the fault-ID-scoped `decision_events` barrier.
3. Send the supported approval asynchronously.
4. Require exactly one advisory holder and one blocked waiter. The waiter must
   be the approval API client backend under the observed database role, blocked
   only by the scoped holder. SQL must already show exactly one committed Canon
   and one fixture-bound pending `canon.phase3.requested` event.
5. `stop minio`, release the database barrier, join the approval request, and
   require the response status `maintenance_pending`.
6. Require the world maintenance row to be `pending` or `failed`, under its
   unchanged natural key, while the held phase-3 event remains pending.
7. Drop the scoped trigger, function, and scope table and require zero object
   and advisory-lock residue.
8. `start minio`, then
   `setup-release outbox-worker --hold-id pre-approval`.
9. Wait for the real worker to process the exact phase-3 event and for all four
   maintenance steps and the MinIO object to converge.

The trigger matches the exact project, chapter, approval reason,
`event_type='review_approved'`, and `actor_type='api'`. Missing or ambiguous
holder, waiter, role, Canon count, event identity, or residue is
`setup_blocked`.

## Same-Event Replay

After initial convergence:

1. `setup-hold outbox-worker --hold-id same-event-replay`.
2. Read the sole processed fixture-bound `canon.phase3.requested` row.
3. Execute one conditional `UPDATE outbox_events SET status='pending', ...`
   whose `WHERE` binds the exact row ID, event ID, processed status, attempts,
   availability, worker and lease fields, processed/error fields, raw payload,
   aggregate type/ID, event type, and payload Canon idempotency key.
4. Require `rowcount = 1`, reread the row, and require event ID, payload hash,
   aggregate identity, and Canon idempotency identity to be unchanged.
5. Write the before hash, predicate hash, rowcount, after hash, and stable event
   identity as `same-event-replay.json`.
6. `setup-release outbox-worker --hold-id same-event-replay` and let the real
   worker replay the event.
7. Require the maintenance natural-key inventory and complete MinIO object
   inventory to equal their pre-replay baselines.

The operation never inserts an event and never changes its ID, payload,
aggregate, idempotency identity, attempts, or lease epoch.

## MinIO Evidence

The expected world key is derived from production composition:

```text
{prefix}/projects/{project_id}/keyed/post_canon/{canon_id}/world/llm_trace.json
```

Inventory uses recursive list, HEAD, and object GET. Each normalized object is
exactly `{key, etag, size, content_type, content_sha256}`. The SHA-256 is
computed independently from downloaded bytes; timestamps and metadata are
excluded. List/HEAD disagreement, byte/HEAD size disagreement, duplicate keys,
or a missing expected key fails closed.

## Evidence And Cleanup

All success snapshots run through Task 1's strict evaluator. The shared writer
atomically creates direct-child artifacts without clobbering, reopens and
rehashes them, rederives assertions, and validates the report with Task 2's
finalizer before and after writing.

Barrier sessions and asynchronous approval are released, joined, and closed in
`finally`; the MinIO client is closed as well. Success invokes controller
`destroy`. Any `SetupBlocked` or `RunnerError` invokes controller `abort` and
writes only a non-PASS `setup_blocked` report.
