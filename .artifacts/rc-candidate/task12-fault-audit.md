**Scope**

Read-only inspection completed at clean commit `80af1c83323b90f8d69321c0cabe4c98f9185f48`. I did not run generation, query live ForWin state, execute tests, alter files, or touch containers.

This is a historical pre-R8 gap analysis, not the final recovery procedure.
The current executable contract is
`.artifacts/rc-candidate/recovery-evidence-runbook.md`. In particular, isolated
R8 commits `1bf43f668fdd969d03e103913af93b47c9dc936b` and `e17ed39`
supersede the backend-cover restart gap recorded below; live proof for the
integrated RC is still outstanding.

| Fault at audited SHA | Honest live stop/start proof |
|---|---|
| Qdrant unavailable | Yes |
| Outbox/projection consumer unavailable | Yes |
| MinIO pre-Canon | Yes |
| MinIO post-Canon | **No: deterministic hook/barrier required** |
| `publisher-worker` unavailable before claim | Yes |
| `publisher-worker` interrupted/failed retry | **No: restart recovery is not wired** |
| `publisher-browser` unavailable before claim | Yes |
| Browser interrupted after remote mutation | Conditional on authoritative platform read-back; exact timing needs a hook |
| CAPTCHA/MFA/account-risk induction | **No supported induction API; fixtures or a real platform challenge only** |

**Shared Harness**

Use one fault per disposable Docker context/host. `docker-compose.yml` hard-codes `container_name`, so `-p` alone cannot isolate a second stack on the same daemon. Define:

```bash
WT="$(git rev-parse --show-toplevel)"
CTX=<dedicated-docker-context>
```

Set `FORWIN_ARTIFACT_BACKEND=minio` for `forwin`, generation, and outbox processes; the repository default is `local`, so stopping MinIO otherwise proves nothing. See [.env.example](../../.env.example#L38) and [docker-compose.yml](../../docker-compose.yml#L2).

Prepare each project only through the supported flow: `POST /api/projects` with any required `publish_bindings`, generate and lock every Genesis stage, then `POST /api/projects/{id}/start-writing`. Stop at one `needs_review` candidate and require `GET /api/tasks/active-generation-check?project_id={id}` to report no active task. There is no supported candidate-seeding API. Routes: [routes.py](../../forwin/http/routes.py#L581), [routes.py](../../forwin/http/routes.py#L623), [routes.py](../../forwin/http/routes.py#L725).

Record commit SHA, UTC fault/start/recovery times, project/chapter/candidate IDs, and all response bodies. Shared cleanup: restore the stopped service, capture evidence, `DELETE /api/projects/{id}`, remove any remote test draft through the platform UI, then `docker --context "$CTX" compose -p forwin-v5-t12 -f "$WT/docker-compose.yml" --profile publisher down -v`.

**1. Qdrant Unavailable**

Stop exactly after the candidate is review-ready and before approval:

```bash
docker --context "$CTX" compose -p forwin-v5-t12 -f "$WT/docker-compose.yml" stop qdrant
```

Trigger `POST /api/projects/{P}/chapters/{N}/review/approve` with `{"continue_generation":false,"reason":"v5-t12 qdrant fault"}`. Canon commits atomically before projection processing; the projection outbox handler fails and returns to `pending`. See [acceptance.py](../../forwin/generation/pipeline_core/acceptance.py#L103), [admission.py](../../forwin/canon/admission.py#L59), and [worker.py](../../forwin/outbox/worker.py#L111).

Before/during/after reads: project, chapter, candidate, decision events, `GET /projections/status`, plus Qdrant native `POST http://127.0.0.1:6335/collections/{llm_kb_vectors|chapter_memories}/points/scroll` filtered by `project_id`. During, Canon must be accepted while Qdrant components are `degraded` with positive lag; use SQL only to prove the outbox row is retryable.

Restart with `... start qdrant`; wait for Compose `healthy`, then until the row’s `available_at + 120s` and projection lag is zero. Replay once through `POST /api/projects/{P}/projections/refresh?projection_kind=all`; point-ID sets and counts must remain unchanged. IDs are deterministic in [vector_index.py](../../forwin/llm_kb/vector_index.py#L92) and [memory_index.py](../../forwin/retrieval/memory_index.py#L228); replay coverage is [test_qdrant_projection_convergence.py](../../tests/test_qdrant_projection_convergence.py#L66).

**2. Outbox/Projection Consumer Unavailable**

Stop only `outbox-worker` before the same approval trigger: `... stop outbox-worker`. Canon/chapter/candidate must become accepted; projection target advances but remains `never` or lagging, and SQL must show the deterministic Canon projection event still `pending`, attempts normally `0`.

Start with `... start outbox-worker`. It has no healthcheck; readiness is container `running`, followed by the row becoming `processed` and `GET /projections/status` reporting all components healthy with zero lag. Deadline is 120 seconds for a never-claimed row; if stopped after claim, use `lease_expires_at + 120s` because the lease is 60 seconds and polling is 2 seconds. Compose timing is defined at [docker-compose.yml](../../docker-compose.yml#L82). Canon ID, candidate ID, outbox `event_id`, and row count must not change. Fixture coverage: [test_projection_outbox.py](../../tests/test_projection_outbox.py#L128).

**3. MinIO Pre-Canon**

Stop `minio` after the review-ready artifact exists, then call review approval. Acceptance rereads the writer metadata from object storage before constructing or committing Canon, so the request should terminate with HTTP 500 and no Canon/outbox identity change. See [finalization.py](../../forwin/generation/pipeline_core/finalization.py#L121) and [artifacts.py](../../forwin/storage/artifacts.py#L157).

During the outage, chapter remains `needs_review`, candidate remains non-Canon, and Canon/outbox SQL counts remain unchanged. Start MinIO and require HTTP 200 from `/minio/health/ready` from the `forwin` container. Retry the identical approval; it must bind the same candidate to exactly one Canon commit and one set of outbox event identities. Treat no HTTP result within 120 seconds as a failed fault run.

**4. MinIO Post-Canon: Gap**

There is no supported API or CLI that pauses after Canon commit but before phase-3 artifact writing, nor one that explicitly reruns post-Canon maintenance. Approval commits Canon and immediately enters phase 3 in the same process at [acceptance.py](../../forwin/generation/pipeline_core/acceptance.py#L167). Polling for the commit and racing `docker stop minio` is not deterministic.

Furthermore, the MinIO write occurs only when LLM attempts exist, using the stable key `post_canon/{canon_commit_id}/{step}/llm_trace.json`; some runs will perform no MinIO write at all. See [post_canon.py](../../forwin/maintenance/post_canon.py#L972). `GET /api/projects/{P}/maintenance/post-canon` is read-only.

The behavior is proven only by an injected artifact-store fixture at [test_post_canon_maintenance.py](../../tests/test_post_canon_maintenance.py#L223). A release-grade live proof needs a test-only “pause after Canon” barrier or “fail next keyed artifact write” hook.

**5. Publisher Worker and Browser**

At the audited SHA, `publisher-worker` handles backend-owned `cover_generate`, not browser uploads; see [cli.py](../../forwin/cli.py#L196). Stop it, then `POST /api/publishers/upload-jobs` with `create_if_missing=true` and cover generation enabled. Find the associated `task_kind=cover_generate` through `GET /api/publishers/upload-jobs`; it must remain `pending`. Start the worker and allow its 2-second poll plus provider timeout. This proves pre-claim outage recovery. At that historical SHA, a mid-claim stop leaves the job `running` because startup recovery is not wired. R8 replaces that behavior with a singleton backend worker, startup reclaim, owner-token fencing, atomic cover persistence, and orphan cleanup; use the current recovery runbook for the required live kill/restart proof.

For browser outage, configure `publish_bindings` before Canon, wait for the scheduled Canon job, then stop `publisher-browser`. Release it through `POST /api/projects/{P}/publishers/upload-jobs` with `publish=false`; `GET /api/publishers/upload-jobs/{J}` must remain `pending`, with unchanged Canon/candidate/job idempotency identity and no receipt. Start with `docker ... --profile publisher start publisher-browser`; require Compose healthy and `GET /api/publishers/extension/heartbeat-status` fresh within 180 seconds. Allow 270 seconds for Fanqie or 540 seconds for Qidian. Repeating the project release must return the same job and must not enqueue another upload. Canon job identity is defined in [canon_jobs.py](../../forwin/publisher_runtime/canon_jobs.py#L28).

Interrupted post-mutation recovery journals before mutation and replays through read-only reconciliation, but a deterministic real stop inside that narrow boundary requires a browser barrier. The fixtures prove it at [controller.test.js](../../browser_extension/forwin-publisher/tests/controller.test.js#L1404).

**6. CAPTCHA/MFA/Account Risk**

No supported API or CLI induces these conditions. The extension detects real platform UI at [platform-agent.js](../../browser_extension/forwin-publisher/platform-agent.js#L3666). The extension-authenticated pause route is a reporting protocol, not a fault-injection endpoint:

`POST /api/publishers/extension/upload-jobs/{J}/attempts/{A}/pause`, with a live fence, `risk_reason` in `captcha|mfa|account_risk`, timestamp, URL, and detector evidence. Schema: [publisher.py](../../forwin/api_schema/publisher.py#L361).

If a real challenge occurs, `GET /upload-jobs/{J}` must show `paused`, reason, token, and no new claim/receipt. After the operator manually completes the challenge, call `POST /api/publishers/upload-jobs/{J}/resume` with the expected reason/token and operator reason. Pre-mutation resumes to `pending`; post-mutation resumes to `reconciling`; repeating resume is idempotent and records one operator action. A pause has no timeout by design. Deterministic evidence for all three reasons exists only in [test_publisher_risk_pause.py](../../tests/test_publisher_risk_pause.py#L37).

**SQL Exceptions**

No supported read surface exposes outbox rows, complete candidate/Canon identity, attempt rows, receipt count, or operator-action rows. Restrict SQL to:

```sql
SELECT c.id,c.candidate_draft_id,c.canon_commit_id,c.idempotency_key,
       cc.id,cc.idempotency_key
FROM candidate_draft_records c LEFT JOIN canon_commit_records cc ON cc.id=c.canon_commit_id
WHERE c.project_id='<P>' AND c.chapter_number=<N>;

SELECT event_id,event_type,status,attempts,available_at,lease_epoch,lease_expires_at
FROM outbox_events WHERE aggregate_id='<P>' AND created_at >= '<FAULT_UTC>' ORDER BY created_at;

SELECT id,attempt_number,attempt_kind,phase,status,lease_epoch FROM publisher_upload_attempts WHERE upload_job_id='<J>';
SELECT receipt_key,remote_book_id,remote_chapter_id,content_sha256 FROM publisher_upload_receipts WHERE upload_job_id='<J>';
SELECT action,pause_token,actor_id,auth_method,reason FROM publisher_operator_actions WHERE upload_job_id='<J>';
```

The main route map is centralized in [routes.py](../../forwin/http/routes.py#L1020) for projections/maintenance, [routes.py](../../forwin/http/routes.py#L1056) for chapter reads, and [routes.py](../../forwin/http/routes.py#L467) for publisher resume/extension attempt protocol.
