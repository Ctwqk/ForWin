# Publisher Backend Worker Restart Reclaim

Candidate audit source: `80af1c83323b90f8d69321c0cabe4c98f9185f48`

Isolated implementation:

- branch: `codex/v5-r8-publisher-recovery`
- commit: `1bf43f668fdd969d03e103913af93b47c9dc936b`
- base: `80af1c83323b90f8d69321c0cabe4c98f9185f48`
- status: validated but intentionally not merged into the frozen matrix source

## Defect

`PublisherBackendJobRunner.claim_next_cover_generate_job()` changes a
`cover_generate` upload job from `pending` to `running`, but the publisher
worker process does not recover an orphaned backend-owned job on startup. If
the process exits between claim and completion, subsequent worker instances
only search for `pending`; the job remains permanently `running`.

The HTTP runtime's broad `PublisherAttemptService.recover_interrupted()`
currently contains a second cover-job recovery branch. That is the wrong owner:
it recovers cover jobs only when an unrelated API process restarts, while the
publisher worker responsible for those jobs has no startup recovery.

The API automation scheduler also invokes `run_pending_once()` every 30 seconds,
creating a third, unlocked execution owner. Cover assets and the cover job's
terminal state are committed in separate transactions, so a crash between them
can replay generation and leave duplicate DB rows or orphaned files.

This is a generalized post-commit consumer recovery defect. It is unrelated to
any matrix project or chapter.

## Scope

Implemented scope:

- `docker-compose.yml`
- `forwin/api_schema/__init__.py`
- `forwin/api_schema/publisher.py`
- `forwin/application/publisher/operations.py`
- `forwin/application/publisher/service.py`
- `forwin/cli.py`
- `forwin/http/adapters/api_publisher_routes.py`
- `forwin/http/automation.py`
- `forwin/http/routes.py`
- `forwin/publisher_runtime/backend_jobs.py`
- `forwin/publisher_runtime/attempts.py`
- `forwin/publisher_runtime/covers.py`
- `forwin/publishers/manager.py`
- `tests/test_docker_compose_profiles.py`
- `tests/test_http_app_factory.py`
- `tests/test_publisher_runtime_covers.py`
- `tests/test_publisher_worker_cli.py`
- `tests/test_publisher_risk_pause.py` only to remove a fixed-wall-clock
  assumption exposed by the broad publisher regression
- `tests/test_runtime_worker_roles.py`

Do not recover browser-owned upload attempts from publisher-worker startup.
`PublisherAttemptService.recover_interrupted()` intentionally transitions
uncertain browser mutations to reconciliation, and calling it from an
independent backend worker could interrupt a healthy browser attempt.

## Required Behavior

1. `PublisherBackendJobRunner` exposes one narrow startup recovery operation.
2. It locks only non-deleted `cover_generate` jobs with:
   - `status IN ('running', 'terminating')`
   - no browser attempt identity
3. A normal orphan returns to `pending`, clears backend ownership and terminal
   scheduling state, preserves first `claimed_at` / `started_at`, and retains
   the same job ID and idempotency identity.
4. An abort-requested or `terminating` orphan becomes `cancelled`.
5. Browser upload/reconcile jobs and jobs with a current attempt are unchanged.
6. `run_publisher_worker_loop()` invokes recovery exactly once before its first
   poll, not once per idle loop.
7. Recovery is idempotent; a second call returns no additional IDs.
8. `PublisherAttemptService.recover_interrupted()` excludes `cover_generate`,
   leaving one recovery owner.
9. A PostgreSQL advisory lock permits only one publisher backend worker to
   recover and execute jobs at a time. Recovery waits for a concurrently
   locked candidate row instead of skipping it forever.
10. The API automation scheduler never executes publisher backend jobs.
11. Cover assets, job terminal state, and any follow-up upload enqueue commit
    atomically. Rollback deletes staged files, and abort races cannot be
    overwritten by stale success/failure.
12. Every backend claim has a unique `backend:<uuid>` owner token. Completion
    and failure require an exact token match, so a worker surviving advisory
    lock loss cannot overwrite a replacement claim.
13. Cover bytes are written through `.staging` and atomically renamed. Startup
    recovery, while holding the singleton lock, removes files with no committed
    `PublisherCoverAsset` reference.
14. The publisher worker writes under `/app/data/publisher_covers`, which is
    mounted at the same path in the publisher browser container.
15. The API automation fallback and direct
    `POST /api/publishers/covers/generate` owner are physically deleted,
    including their application and schema wrappers.

## Offline Verification

At isolated commit `1bf43f668fdd969d03e103913af93b47c9dc936b`:

- full suite: `2103 passed, 1 skipped, 5 warnings`
- publisher/API/Compose focused suite: `199 passed`
- Ruff: pass
- `compileall`: pass
- `git diff --check`: pass
- final independent review: no remaining P0-P2 in claim, recovery, storage,
  transaction, or scavenger behavior

These results validate the isolated implementation only. They do not promote
the frozen matrix candidate or replace the required real worker kill/restart
evidence.

## TDD Order

RED 1:

Add a database-backed test proving a `running` cover job is reclaimed, a
browser-owned running job is untouched, IDs do not change, and a second
recovery is empty.

RED 2:

Add a CLI-loop fake proving startup recovery is called once before
`run_pending_once`, for both `--once` and polling behavior.

GREEN:

Implement the narrow service method and one startup call. Do not add schema,
mode, compatibility fallback, retry endpoint, or new worker service.

Focused command:

```bash
uv run python -m pytest -q \
  tests/test_publisher_runtime_covers.py \
  tests/test_publisher_worker_cli.py \
  --tb=short
```

## Live Proof

On the isolated recovery stack:

1. Create one `cover_generate` job through the supported publisher API.
2. Wait until the backend worker has claimed it and status is `running`.
3. `SIGKILL` only `forwin-v5-recovery-publisher-worker` with automatic restart
   temporarily disabled by the recovery controller.
4. Restart the same service/image.
5. Prove startup recovery returns the same job to `pending`, then the normal
   worker completes or fails it; it must never remain orphaned `running`.
6. Prove no duplicate job, attempt, or receipt identity was created.
7. Prove the recovered claim has a new owner token and that a delayed result
   carrying the pre-crash token cannot mutate the job.
8. Prove the generated cover path is under the shared `/app/data` mount and is
   readable from the publisher browser container.
9. Leave one unreferenced staging/final file before restart and prove startup
   recovery removes it while preserving every DB-referenced cover.
