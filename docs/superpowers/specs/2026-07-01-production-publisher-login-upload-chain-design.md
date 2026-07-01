# Production Publisher Login And Upload Chain Design

Date: 2026-07-01

## Purpose

Advance the ForWin production readiness goal through the next hard blocker:
the shared production publisher browser must regain verified Fanqie and Qidian
login state, then prove the publisher API, backend workers, extension claim
path, and browser automation can run a safe `publish=false` upload-chain smoke.

This spec follows the already-deployed production baseline work. Production is
currently up on GitHub commit `6724fb0`, service health is OK, Discord login QR
forwarding is disabled, and the publisher extension defaults to not calling the
login-QR endpoint. The remaining blocker is platform authentication: both
Fanqie and Qidian redirect the production publisher browser profile to login
pages.

## Current Evidence

- Source of truth is GitHub `Ctwqk/ForWin` `master`; `10.0.0.246` is retired
  and must not be used as a source workspace.
- Production deploy target is `10.0.0.126:/Users/magi1/ForWin-swarm`, managed
  by the 150 deploy sync.
- Production services are expected to run:
  `forwin-app-swarm`, `forwin-mcp-swarm`,
  `forwin-generation-worker-swarm`, `forwin-publisher-worker-swarm`,
  `forwin-outbox-worker-swarm`, and `forwin-publisher-browser-swarm`.
- The shared publisher browser loads `browser_extension/forwin-publisher`.
- Baseline verification classifies both `fanqie` and `qidian` as
  `human_login_required` because their real pages land on login URLs.
- Existing cookie names inside the browser profile are not enough evidence.
  Page-level login state is authoritative.

## Existing Surfaces To Reuse

- Runtime baseline: `scripts/check_production_publisher_baseline.py`.
- Supervisor snapshots: `scripts/monitor_forwin_runtime.py` and
  `scripts/supervise_forwin_interventions.py`.
- Operator-facing publisher API:
  `/api/publishers/platforms`,
  `/api/publishers/browser-sessions/{platform}`,
  `/api/publishers/preflight`,
  `/api/publishers/upload-jobs`,
  `/api/publishers/upload-jobs/{job_id}`,
  `/api/publishers/upload-jobs/{job_id}/terminate`,
  `/api/publishers/work-bindings`, and
  `/api/publishers/chapter-bindings`.
- Project upload API:
  `/api/projects/{project_id}/chapters/{chapter_number}` and
  `/api/projects/{project_id}/publishers/upload-jobs`.
- Extension-only API:
  `/api/publishers/extension/heartbeat-status`,
  `/api/publishers/extension/upload-jobs/claim`,
  `/api/publishers/upload-jobs/{job_id}/result`, and
  `/api/publishers/extension/browser-sessions/{platform}`. These are used by
  the extension flow and tests, but production reports must not print raw
  extension keys or session payloads from them.

## Goals

1. Preserve one shared persistent production publisher browser profile for
   Fanqie and Qidian.
2. Provide a safe operator handoff for platform login that does not use Discord
   QR forwarding, does not log QR images, and does not expose cookies or tokens.
3. Verify login state through all required surfaces:
   `/api/publishers/platforms`,
   `/api/publishers/browser-sessions/{platform}`,
   key-protected `/api/publishers/extension/heartbeat-status` when the key is
   available, and real browser page state.
4. Run publisher API smoke coverage without publishing content:
   - `POST /api/publishers/preflight`
   - `POST /api/publishers/upload-jobs`
   - `GET /api/publishers/upload-jobs`
   - `GET /api/publishers/upload-jobs/{job_id}`
   - `POST /api/publishers/upload-jobs/{job_id}/terminate`
   - `DELETE /api/publishers/upload-jobs/{job_id}`
   - `GET /api/publishers/work-bindings`
   - `GET /api/publishers/chapter-bindings`
   - `POST /api/projects/{project_id}/publishers/upload-jobs`
   - `GET /api/projects/{project_id}/chapters/{chapter_number}`
5. Prove the extension can claim at least one safe upload job and return a
   terminal result without consuming publish quota.
6. Emit redacted structured evidence for every check and every human-blocked
   item.

## Non-Goals

- Do not run long-form generation in this slice. That is the next independent
  spec.
- Do not create books, upload chapters, or publish with `publish=true` until
  platform quota and account rules are confirmed.
- Do not split Fanqie and Qidian into separate browser profiles unless a later
  verified platform limitation makes the shared profile impossible.
- Do not replay, print, copy, or store raw cookies, passwords, tokens, QR image
  data, API keys, Discord webhooks, or `FORWIN_PUBLISHER_SESSION_SECRET`.
- Do not bypass captcha, MFA, platform risk control, or account verification.
- Do not mutate project, Genesis, task, chapter, or WorldModel state outside
  MCP/operator-approved workflows when an MCP tool exists.

## Approaches Considered

### Recommended: Staged Login Handoff Then Publish-False Chain

Use the existing production publisher browser as the single source of platform
state. First open or reuse Fanqie and Qidian login pages in that profile and
record a minimal human handoff. After the human completes login, run the
baseline verifier until API state and page evidence agree. Only then run
publisher API and upload-job smoke tests with `publish=false`.

This is the safest path because it separates authentication from publishing,
keeps one browser profile, and produces clear evidence at each boundary.

### Alternative: API-Only Publisher Smoke

Exercise API endpoints with synthetic jobs and never wait for the browser to
claim them. This is faster, but it does not prove the extension, browser, and
platform page automation are actually working. It should be used only for unit
or local API regression checks, not production readiness.

### Alternative: Full End-To-End Publish

Log in, create or reuse a test book, upload a sample chapter, and set
`publish=true`. This proves the most, but it consumes real platform quota and
can leave public or reviewable artifacts. It belongs after quota/risk preflight
has been completed and after `publish=false` is stable.

## Design

### Phase 1: Production Runtime Reconfirmation

Before platform work, rerun the production baseline:

```bash
python scripts/check_production_publisher_baseline.py \
  --api-base http://10.0.0.126:8899 \
  --mcp-health-url http://10.0.0.126:8896/health \
  --docker-context swarm-manager-150 \
  --colima-profile swarmbridged
```

Expected acceptable state before login is `degraded` with only
`publisher_login_required` blocked items. Any failed service, unhealthy app/MCP
endpoint, stale publisher-browser container, or Discord webhook violation must
be fixed before continuing.

### Phase 2: Human Login Handoff

The operator opens Fanqie and Qidian in the production publisher browser
profile, not in an unrelated local Chrome profile:

- Fanqie: `https://fanqienovel.com/main/writer/`
- Qidian: `https://write.qq.com/portal/dashboard`

If either platform shows QR scan, captcha, MFA, login confirmation, or risk
control, the automation stops at a handoff record containing only platform,
safe URL, page-state label, and the command to rerun after completion. No QR
image or login token is copied into logs or Discord.

When the human reports that login is complete, rerun the baseline. Login is
accepted only when both page evidence and `/api/publishers/platforms` converge
to connected after at least one heartbeat interval.

### Phase 3: Quota And Safety Preflight

Before creating any publisher work, record platform limits that affect testing:

- whether a reusable safe test book already exists
- whether draft-only chapter upload is allowed
- whether test books can be deleted or hidden
- per-day or per-hour upload/publish limits, if the platform shows them
- chapter word-count boundaries and required metadata
- whether the account is currently under risk control

If limits cannot be confirmed from visible UI/API without side effects, the
smoke stays at API/preflight and `publish=false` draft save. No batch uploads or
`create_if_missing=true` happen in this slice unless the operator explicitly
chooses a single marked test book.

### Phase 4: API-Only Endpoint Smoke

Run a redacted smoke that exercises endpoint availability and validation paths
without requiring a live publisher job to succeed. It should use safe temporary
payloads and clean up created upload-job records when possible.

Required outcomes:

- `GET /api/publishers/platforms` returns both supported platforms and recent
  extension heartbeat.
- `POST /api/publishers/preflight` returns clear pass/fail reasons without
  creating real publisher artifacts.
- Upload-job create/list/get/terminate/delete endpoints return expected state
  transitions.
- Work/chapter binding reads succeed and omit sensitive browser/session data.

### Phase 5: Browser-Claimed Publish-False Upload Smoke

After both platforms are connected, create one minimal upload job per platform
with:

- `publish=false`
- `create_if_missing=false`
- a reusable safe test work binding, if available
- short sample content
- no public publish action

The extension must claim the job from the shared production publisher browser.
The extension claim path uses `/api/publishers/extension/upload-jobs/claim`,
then records completion through `/api/publishers/upload-jobs/{job_id}/result`.
The smoke records job state transitions, final URL, redacted result payload,
and whether draft-save evidence was observed. If a platform returns login
required, quota/risk control, missing book binding, or editor validation error,
the result becomes a blocked item or regression candidate depending on whether
it is operator-actionable.

### Phase 6: Project-Chapter Upload Path

Only after endpoint and direct upload-job smoke pass, use MCP/operator project
truth to select or create a safe test project/chapter path. The project/chapter
path verifies:

- `GET /api/projects/{project_id}/chapters/{chapter_number}`
- `POST /api/projects/{project_id}/publishers/upload-jobs`
- resulting work/chapter bindings

This phase still uses `publish=false` and does not create public content.

## Data Flow

1. Baseline verifier checks services, health endpoints, Discord policy, browser
   container, page state, and publisher API state.
2. Human completes platform login only inside the production publisher browser
   profile.
3. Extension heartbeat syncs page evidence and browser session summary to the
   backend.
4. API smoke creates safe upload-job records and verifies endpoint behavior.
5. Publisher worker and browser extension claim eligible jobs.
6. Extension performs platform draft-save automation with `publish=false`.
7. Backend records redacted terminal job result and bindings.
8. Smoke emits one structured report with actions taken, blocked items, and
   next commands.

## Report Schema

Every production smoke report should be a single JSON object with stable,
redacted fields:

- `checked_at`, `phase`, and `status`
- `git`: local commit, deployed commit, and whether the local tree had
  unrelated changes
- `services`: expected service names, image tags, replicas, and health summary
- `health`: app and MCP health payload summaries
- `publisher_browser`: container identity, extension version, page evidence,
  and whether browser inspection ran
- `platforms`: per-platform API/page status, final safe URL, title, and blocked
  item if any
- `publisher_api`: endpoint smoke status and heartbeat freshness
- `upload_jobs`: created job IDs, platform, publish flag, state transitions,
  terminal state, and cleanup result
- `project_chapter_path`: project ID, chapter number, and API status when this
  phase runs
- `actions_taken`: safe operator actions only, with no QR images or secrets
- `blocked_items`: actionable human/platform blockers with rerun commands
- `redactions`: names of redaction rules applied, not the sensitive values

The report must be useful enough to debug the run without requiring raw browser
storage, cookie values, tokens, QR images, webhook URLs, or screenshots of
login credentials.

## Error Handling

- Login, captcha, MFA, risk control, or scan confirmation becomes
  `human_login_required`; automation does not work around it.
- API/page mismatch after heartbeat wait becomes `state_sync_mismatch`.
- Missing reusable safe work binding becomes `publisher_test_work_missing`.
- Platform quota uncertainty becomes `quota_unconfirmed`; no create/publish
  actions run until resolved.
- Upload-job failure with a reproducible extension/backend error becomes
  `regression_candidate` with safe logs and a recommended test target.
- All reports must redact sensitive values and avoid raw browser/session dumps.

## Testing And Verification

Automated tests should cover:

- Smoke report redaction for cookies, secrets, API keys, QR image data, and
  webhooks.
- Endpoint smoke classifications for success, validation failure, cleanup, and
  blocked login.
- Upload-job state transition handling for pending, running, succeeded, failed,
  cancelled, terminated, and deleted.
- Baseline integration when API state is connected but page evidence shows
  login, and when page evidence is connected but API heartbeat is stale.

Manual/production verification should record:

- deployed commit and service images
- app and MCP health payloads
- publisher extension version
- `/api/publishers/platforms` connected status for both platforms
- browser final URL/title/dashboard-vs-login evidence for both platforms
- upload-job IDs and redacted terminal states
- whether any test drafts or test books were left behind

## Acceptance Criteria

This slice is complete when:

1. Production services remain healthy on one deployed commit.
2. Discord/webhook and login-QR notification policy remains disabled in shared
   production.
3. Fanqie and Qidian are both connected in the same production publisher
   browser profile by API and page evidence.
4. Required publisher API endpoints have been exercised with redacted evidence.
5. At least one `publish=false` browser-claimed upload smoke has reached a
   clear terminal state, or each platform has a minimal human/platform blocked
   item explaining why it cannot proceed.
6. No secret, cookie value, token, password, QR image, or webhook URL appears in
   repository files, command logs, or the final report.

## Follow-Up Specs

- Long-form generation stability experiment.
- Quota-confirmed `publish=true` minimal real publish verification.
- Two-hour recurring intervention supervisor hardening and alert workflow.
