# Deterministic Generation Worker Crash Barriers

Both V2 crash proofs run on a fresh isolated recovery stack and use a temporary
PostgreSQL advisory-lock trigger. The trigger is test-only and must never be
installed on a shared or production database.

## Shared Isolation

- Use a new database volume, project, fault ID, and evidence directory.
- Set generation-worker lease to 30 seconds and poll interval to 1 second.
- Stop the outbox worker before arming either barrier.
- Record the baseline non-system trigger/function inventory.
- Derive a signed 64-bit advisory key from the unique fault ID.
- A dedicated PostgreSQL session holds the session advisory lock.
- The trigger calls `pg_advisory_xact_lock` only for the exact project and
  chapter under test.

## Pre-Commit Crash

Arm a `BEFORE INSERT` trigger on `canon_commit_records`, filtered by exact
`NEW.project_id` and `NEW.chapter_number`.

1. Start one supported generation task for exactly one chapter.
2. Wait until PostgreSQL shows the worker transaction blocked in the trigger.
3. Require zero Canon rows and a `ready_for_canon` candidate.
4. Disable automatic restart and SIGKILL only the generation worker.
5. Confirm the blocked transaction rolled back and Canon remains absent.
6. Release/drop the barrier, restore automatic restart, and start the worker.
7. After lease expiry, require the same task ID to be reclaimed at a higher
   lease epoch.
8. Require exactly one accepted chapter/candidate/Canon/GraphDelta/outbox
   identity and a completed task.

## Post-Commit Crash

Arm a `BEFORE INSERT` trigger on `post_canon_maintenance_runs`, filtered by
exact `NEW.project_id`, `NEW.chapter_number`, and `NEW.step_name = 'planning'`.
This point is after `CanonAdmissionService.commit_plan` has committed and before
the generation worker can finish post-acceptance work.

1. Start one supported generation task for exactly one chapter.
2. Wait until exactly one committed Canon row exists and PostgreSQL shows the
   worker blocked in the maintenance trigger.
3. Freeze the accepted chapter, candidate, Canon, GraphDelta, snapshot, and
   three deterministic outbox IDs.
4. Disable automatic restart and SIGKILL only the generation worker.
5. Release/drop the barrier and start the same image/service.
6. After lease expiry, require the same task ID at a higher lease epoch.
7. Require Canon replay to be idempotent, task completion to record the same
   chapter, and all frozen identities/counts to remain unchanged.
8. Start the outbox worker and require post-Canon consumers to converge.

## Mandatory Cleanup

Run in a `finally` path:

1. Release the advisory lock.
2. Drop the uniquely named trigger and function.
3. Restore generation-worker restart policy and start it if stopped.
4. Restore the outbox worker.
5. Compare trigger/function inventory to baseline and require zero residue.
6. Hash SQL, controller events, task snapshots, and before/during/after
   identity snapshots into the corresponding independent fault report.
