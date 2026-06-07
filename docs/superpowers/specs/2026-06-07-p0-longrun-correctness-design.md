# P0 Long-Run Correctness Repair Design

## Scope

This spec covers only the P0 repair bundle for long-running generation:

- Canon apply failures must never allow a chapter to be accepted.
- Generation workers must refresh task leases while generation is still running.
- Core chapter failures must stop the current run by default.

P1/P2/P3 follow-up items are recorded at the end of this file for later planning. They are not part of this implementation pass.

## Current Risks

### Canon apply failure can look successful

`CanonApplyOutcome.blocked` already treats either `blocked_path` or `block_kind` as a block. The exception path in `_apply_canon_candidate` only returns a blocked outcome when a frozen artifact exists. In pulp mode, `freeze_failed_candidates` is false, so the exception path can return an empty outcome after rollback and `mark_canon_failed`. The chapter loop then proceeds to `accepted`.

### Worker lease can expire during a long task

`claim_generation_task` can reclaim running tasks when `lease_expires_at` is expired. Workers heartbeat at claim and after executor completion, but not during multi-chapter execution. Progress updates from `update_task` do not refresh the lease.

### Core chapter failure can cascade

`LongRunPolicy.stop_on_chapter_failure` defaults to true, but the chapter loop does not read it. Transient LLM-like failures stop the run; other exceptions mark the chapter failed and continue to the next chapter.

## Design

### Canon Apply Failure Handling

Change `_apply_canon_candidate` so every exception returns:

```python
CanonApplyOutcome(blocked_path=frozen_path, block_kind="canon_apply_error")
```

`blocked_path` remains optional. Whether a frozen artifact is produced controls diagnostics only; it does not decide chapter acceptance. The existing chapter loop already routes blocked canon outcomes to `needs_review`, so no new chapter-loop branch is needed.

Tests:

- Replace the existing freeze-disabled exception test that asserts unblocked.
- Assert freeze-disabled canon apply failure does not call artifact freeze, does call `mark_canon_failed`, rolls back, and returns `block_kind="canon_apply_error"`.
- Add or adapt a chapter-loop regression showing a canon apply error without artifact leaves the chapter in `needs_review`, not `accepted`.

### Runtime Worker Heartbeat

Keep the existing claim-time and completion-time heartbeats. Add runtime lease refresh in the worker task update path.

The `_db_task_updater` closure already knows `task_id`, and `_default_continue_executor` / `_default_new_executor` know `worker_id` and `lease_seconds`. Change the updater factory so it can receive `worker_id` and `lease_seconds`, and refresh `heartbeat_at` / `lease_expires_at` when:

- the row exists,
- the row is still owned by this worker,
- the row status is `running` or the incoming change keeps it running.

This makes each stage/chapter progress update extend the lease without creating a separate background thread. If ownership has changed, the updater should not refresh the lease; existing claim and heartbeat semantics remain authoritative.

Tests:

- Add a worker/task test proving a progress update extends the lease.
- Add a reclaim test proving another worker cannot claim the same running task immediately after a progress update refreshes the lease.

### Chapter Failure Stop Policy

In the generic chapter exception path, read:

```python
config.long_run_policy.stop_on_chapter_failure
```

Default behavior is true when the policy is missing or malformed. If true, break after marking the current chapter failed. If false, preserve the current behavior for non-transient failures. Transient LLM-like failures should continue to stop regardless of the opt-out flag.

Tests:

- Default policy: generic chapter failure marks the chapter failed and stops the run.
- Explicit `stop_on_chapter_failure=False`: generic non-transient failure may continue.
- Transient LLM-like failure stops even when the flag is false.

## Error Handling

Canon apply failures are treated as system blocks, not successful commits. They should leave persisted candidate failure metadata intact and route the chapter to review.

Heartbeat refresh is best-effort within the existing task-update transaction. It must not claim ownership or extend a lease for another worker. Failure to refresh due to missing row or changed owner should not mask the original task update.

Chapter failure stop policy applies after rollback, status update, commit, and progress event emission. The current chapter remains recorded as failed.

## Verification

Run focused tests first:

```bash
pytest tests/test_canon_repair_stage.py tests/test_generation_task_lease.py tests/test_generation_worker_observability.py tests/test_long_run_policy.py
```

Run a compile/import sanity check after focused tests:

```bash
python -m compileall forwin
```

If focused tests reveal narrower module-specific failures, add targeted regression tests in the affected test file before changing implementation.

## P1/P2/P3 Backlog For Later

### P1: Context and planning correctness

- Include `retrieved_memories` in total context-budget trimming or give it an explicit protected budget with eviction order.
- Split trope usage into planned and accepted records so replans/retries do not pollute cooldown and repeat-rate metrics.
- Add pressure-report counters for canon failures, lease refreshes, failed-then-stopped runs, and accepted-trope usage.

### P2: Pulp cost and market verifier

- Reduce pulp single-mode required LLM calls by deferring structured extraction or replacing part of it with deterministic extraction.
- Replace the single pulp keyword table with a genre/track-aware verifier.
- Introduce a stricter `PulpChapterContract` covering pressure, protagonist action, public witness, enemy damage, concrete gain, status/wealth shift, and next hook.

### P3: Macro evidence chain

- Derive protagonist macro status from canon facts, DecisionEvents, obligations, and accepted narrative evidence instead of only `ChapterPlan.experience_plan_json`.
- Change arc macro boundary audit from `chapter_end == current_chapter` to missed-boundary-safe auditing with an audited marker or last-audited checkpoint.
- Feed macro progression into band/chapter planning as production guidance, not only post-hoc audit.
