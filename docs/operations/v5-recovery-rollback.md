# V5 Recovery And Rollback Runbook

## Scope

This runbook applies to the fresh-schema v5 recovery stack. Canon, BookState,
candidate admission, outbox recovery, post-Canon maintenance, projections, and
publisher attempts must be treated as one release unit.

Never repair a recovery incident by editing project, task, chapter, Canon,
outbox, maintenance, or publisher rows directly. Use ForWin MCP/API operations
for project and task truth, and let the owning worker replay durable work.

## Invariants

- Accepted Canon does not depend on Qdrant, Obsidian, post-Canon MinIO writes,
  publisher availability, or browser availability.
- One Canon transaction creates exactly the projection, phase 3, and publisher
  recovery events. Their IDs are deterministic under the Canon key.
- Outbox and maintenance claims are fenced by worker and lease epoch. Failure
  returns work to a retryable state; attempt count is diagnostic, not terminal.
- Projection replay rebuilds from authoritative Canon and cannot move a
  checkpoint backwards.
- Publisher work keeps one Canon/platform identity. An uncertain mutation is
  reconciled read-only and is never blindly executed again.
- CAPTCHA, MFA, and account-risk signals pause work. Operators resolve the
  platform challenge; ForWin never automates a bypass.

## Incident Entry

1. Record the deployed source SHA, image tags, UTC incident time, affected
   project/chapter, and current worker state.
2. If chapter ordering may be affected, pause the active generation task through
   the supported ForWin task operation. Do not kill and recreate the task.
3. Preserve Postgres, MinIO, and publisher-browser profile evidence before any
   rollback.
4. Restore the failed dependency or owning worker. Do not create replacement
   Canon records, outbox events, or publisher jobs.
5. Confirm recovery through the owning read surface, then resume generation.

Useful read surfaces include:

- `GET /api/projects/{project_id}/projections/status`
- `GET /api/projects/{project_id}/maintenance/post-canon`
- publisher upload-job and extension heartbeat views
- ForWin MCP project, task, and chapter inspection tools

## Recovery Procedures

### Qdrant Unavailable

Leave Canon and the projection outbox event unchanged. Restore Qdrant, then let
the outbox worker retry. Confirm all three projection checkpoints are healthy,
their lag is zero, and projected chapter never regressed. Do not re-admit the
candidate or enqueue a second projection event.

### Projection Consumer Unavailable

Restore `forwin-outbox-worker-swarm`. A failed or unclaimed event remains
durable and is reclaimed under a new lease epoch. Confirm the event converges
through projection status and that accepted Canon identity is unchanged.

### MinIO Unavailable

For a pre-Canon required artifact failure, the candidate must remain
non-accepted. Restore MinIO and retry the supported generation/admission flow.

For a post-Canon trace failure, keep Canon accepted. Restore MinIO and let the
phase 3 owner retry the failed `world` step. The retry must use the same
deterministic artifact key and must not duplicate maintenance rows.

### Publisher Or Browser Unavailable

Keep the existing scheduled Canon/platform job. Restore the publisher worker or
browser session and release/claim that same job through the normal scheduler or
operator flow. Never create a replacement job for the same Canon/platform key.
If an attempt may have crossed the remote mutation boundary, leave it
`reconciling` until read-only platform evidence proves a match or remains
indeterminate.

### CAPTCHA, MFA, Or Account Risk

Keep the job paused and resolve the visible challenge manually in the bound
browser profile. Resume only through the authenticated operator endpoint with
the expected pause reason and an audit reason. A pre-mutation resume returns the
same job to `pending`; a post-mutation resume enters `reconciling`. A stale
attempt cannot unpause or overwrite the resumed state.

## Rollback

V5 has one fresh baseline and no in-place compatibility migration. Do not run a
schema downgrade against production data and do not deploy only one worker from
another protocol version.

Rollback is an atomic release operation:

1. Pause generation and publisher mutation claims.
2. Preserve the current database, object store, and browser journal/profile.
3. Select the previous complete release SHA and its matching database snapshot.
4. Roll back app, generation worker, MCP, outbox worker, publisher worker, and
   publisher browser together.
5. Restore only the database snapshot that belongs to that release. If no such
   snapshot exists, stop and repair forward on the current schema.
6. Run role health checks and the read-only publisher baseline verifier before
   resuming tasks.

Production deploys continue to use the 150 GitHub sync path after the approved
release has been pushed to `master`. A worktree commit is not deployable proof.

## Pinned Fault Evidence

Candidate:
`8f75133b069c594639d0078b3ec1b853e1225dc2`

Evidence below is isolated deterministic fault injection. It did not stop or
mutate shared production Qdrant, MinIO, browser, or Swarm services.

| Fault | UTC start | Command | Expected and actual replay result |
| --- | --- | --- | --- |
| Qdrant initialization unavailable | 2026-07-22T03:32:36Z | `uv run pytest -q tests/test_lazy_external_indexes.py::test_memory_index_failed_initialization_can_retry_and_success_is_cached --tb=short` | First initialization fails visibly; retry succeeds and only the successful resource is cached. PASS. |
| Projection consumer/backend unavailable | 2026-07-22T03:33:03Z | `uv run pytest -q tests/test_projection_outbox.py::test_canon_projection_failure_preserves_acceptance_and_retries --tb=short` | Canon stays accepted, failed event is retryable, replay processes once and converges. PASS. |
| Post-Canon MinIO unavailable | 2026-07-22T03:33:15Z | `uv run pytest -q tests/test_post_canon_maintenance.py::test_world_trace_failure_keeps_canon_and_retries_same_artifact_key --tb=short` | Canon stays committed; world step retries under the same artifact key. PASS. |
| Publisher backend/preflight unavailable | 2026-07-22T03:33:24Z | `uv run pytest -q tests/test_canon_publisher_jobs.py::test_blocked_preflight_keeps_durable_scheduled_job --tb=short` | The one scheduled Canon job remains durable and is not replaced. PASS. |
| CAPTCHA, MFA, and account risk | 2026-07-22T03:33:38Z | `uv run pytest -q tests/test_publisher_risk_pause.py::test_each_pre_mutation_risk_pauses_and_operator_resume_returns_pending tests/test_publisher_risk_pause.py::test_post_mutation_resume_enters_reconciling_and_wrong_reason_is_rejected --tb=short` | All three pre-mutation risks pause and resume the same job; post-mutation resume reconciles read-only; wrong expected reason is rejected. 4 PASS. |

## Remaining RC Evidence

After the RC SHA is frozen, repeat each fault against isolated deployed services
with real process stop/restart evidence. Record service/image versions, fault
and recovery timestamps, before/after API snapshots, and replay identity counts.
Do not perform those service outages against shared production dependencies.
