# Deterministic Post-Canon MinIO Fault Barrier

## Why A Barrier Is Needed

Manual chapter approval commits Canon first, records the review approval event,
then runs phase 3. Stopping MinIO before the request reaches Canon proves the
pre-Canon failure contract instead. Racing a service stop after observing the
Canon row is not repeatable enough for release evidence.

Canon admission already emits one durable `canon.phase3.requested` outbox
event. The fault harness therefore needs only a deterministic pause between the
Canon transaction and direct phase 3; it does not need a production recovery
API, mode, service, or schema change.

## Isolation

- Run only on the disposable `forwin-v5-recovery` database.
- Use one fresh project and one review-ready chapter.
- Confirm there is no active generation task through the supported API/MCP.
- Stop the isolated outbox worker before approval so the durable event cannot
  race the direct phase 3 path.
- Record the baseline list of non-system triggers/functions before injection.

## Barrier

1. Open a dedicated PostgreSQL session and acquire one session-level advisory
   lock derived from the fault ID.
2. In a separate transaction, create a uniquely named temporary PL/pgSQL
   function and `BEFORE INSERT` trigger on `decision_events`.
3. The trigger calls `pg_advisory_xact_lock` only when all conditions match:
   - exact target `project_id`
   - exact target `chapter_number`
   - `event_type = 'review_approved'`
4. Start the supported chapter review approval request asynchronously.
5. Poll read-only state until both are true:
   - exactly one Canon commit exists for the target chapter
   - the approval backend is waiting on the advisory lock
6. Stop only `forwin-v5-recovery-minio` and record the service transition.
7. Release the session advisory lock. The review event transaction completes,
   then direct phase 3 reaches the real unavailable MinIO backend.
8. Require the approval response to report accepted Canon with deferred or
   pending maintenance, never a rolled-back Canon.

The trigger is a disposable test barrier, not a product feature. It must never
be installed in a shared or production database.

## During-Fault Assertions

- Chapter and candidate are accepted.
- Exactly one `CanonCommitRecord` exists and its ID is unchanged.
- Exactly three deterministic Canon outbox identities exist.
- `canon.phase3.requested` remains retryable, not duplicated.
- The phase 3 world step is failed or pending under the same maintenance row.
- The expected stable trace key is
  `post_canon/{canon_commit_id}/world/llm_trace.json`.
- No second candidate, Canon commit, GraphDelta, outbox event, maintenance row,
  publisher job, or artifact identity is created.

If the world step produces no LLM attempt and therefore performs no MinIO
write, the run does not prove this fault and must be discarded rather than
reported as a pass.

## Recovery

1. Start MinIO and require `/minio/health/ready` from the API container.
2. Start the outbox worker.
3. Wait for the existing phase 3 event to become processed and all expected
   maintenance steps to become succeeded.
4. Verify the world step uses the same run ID, incremented lease epoch, and the
   same deterministic artifact key.
5. Verify accepted Canon identity and all counts are unchanged.
6. Replay the phase 3 event handler once more through its normal idempotent
   path and verify there is no new external or database identity.

## Mandatory Cleanup

In a `finally` path:

1. Release the advisory lock if still held.
2. Drop the uniquely named trigger.
3. Drop the uniquely named function.
4. Restore MinIO and the outbox worker if either is stopped.
5. Compare non-system trigger/function inventories to the baseline and require
   zero test-barrier residue.
6. Hash the barrier script, SQL text, service event log, API bodies, and all
   before/during/after snapshots into the fault report.
