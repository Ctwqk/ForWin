# Production Longform Publish-False Experiment Design

Date: 2026-07-01

## Purpose

Finish the remaining production-readiness proof for ForWin after the latest
publisher-browser login path and upload-chain smoke fixes. The system already
has evidence that the production UI, API, MCP, workers, publisher browser,
Fanqie login, Qidian login, endpoint upload smoke, and two-hour supervisor are
working. What remains is a controlled end-to-end experiment that proves the
current production path can generate new long-form content and move one
generated chapter through the publisher upload flow without publishing it.

The experiment must use ForWin's operator/MCP workflow for project, Genesis,
task, and chapter truth. It must not bypass project/task/chapter rules by
reading or writing raw database rows.

## Current Evidence

- Source of truth is GitHub `Ctwqk/ForWin` on `master`.
- The deployed commit is `ed7145e` with images
  `forwin-forwin:deploy-ed7145ec8572` and
  `forwin-publisher-browser:deploy-ed7145ec8572`.
- Production UI/API routes respond on `http://10.0.0.126:8899`:
  `/health`, `/`, `/world-studio`, and `/publishers`.
- Production MCP health responds on `http://10.0.0.126:8896/health`.
- `scripts/check_codex_operator_ready.py` passes against local forwarded
  `127.0.0.1:8899` and `127.0.0.1:8896`.
- Six ForWin Swarm services are `1/1`:
  `forwin-app-swarm`, `forwin-generation-worker-swarm`,
  `forwin-mcp-swarm`, `forwin-publisher-worker-swarm`,
  `forwin-outbox-worker-swarm`, and `forwin-publisher-browser-swarm`.
- The shared publisher browser has one preferred client for both Fanqie and
  Qidian. Both platforms report `connected=true`, `session_connected=true`,
  and dashboard page evidence with `login_visible=false`.
- Shared production has no Discord publisher login webhook env configured.
- Endpoint-only publisher smoke succeeds for `publish=false` job create, list,
  get, terminate, delete, work bindings, and chapter bindings.
- Browser-claimed `publish=false` smoke has already succeeded for both
  platforms using bound works.
- Existing production projects prove long-form capability historically,
  including multiple completed 60-chapter projects and a 240-chapter project
  currently stopped at a review gate.
- WorldModel, BookState, and observability read surfaces return current
  production data for existing projects.
- The 150-hosted `forwin-codex-supervisor.timer` is enabled and active; the
  latest systemd run exited successfully with `blocked_items: []`.

## Goals

1. Create or identify a single explicit ForWin production test project for this
   experiment through the MCP operator path.
2. Complete Genesis through supported MCP stage tools if a new project is used.
3. Start writing only after `task_active_generation_check` proves no active
   generation task exists.
4. Generate at least one long chapter, then continue to multiple chapters when
   runtime, cost, and gates allow.
5. Exercise safe interruption semantics with `task_pause`, `task_get`,
   `task_list`, and `project_continue_generation` when the project is in a
   valid resumable state.
6. Inspect generated chapters through `chapter_list` and `chapter_get`, without
   printing full chapter bodies to logs or final reports.
7. Verify WorldModel and BookState updates through supported read interfaces.
8. Verify observability endpoints for task, project, chapter, LLM, and DB
   performance.
9. Upload one generated chapter through the project publisher upload-job API
   with `publish=false`, first to the safest already-bound platform, then to
   both platforms if preflight and bindings are clean.
10. Record structured evidence: project id, task ids, chapter numbers, safe
    titles, approximate character counts, statuses, elapsed time, LLM failure
    count, retry count when available, review gates, upload job ids, platform
    statuses, and blocked items.

## Non-Goals

- Do not publish public content in this experiment.
- Do not create platform books unless existing work bindings are missing and a
  separate human-approved platform quota plan allows one clearly named test
  book per platform.
- Do not run `publish=true` until platform quotas, audit/review requirements,
  cleanup options, and public-content risk are separately confirmed.
- Do not bypass login, captcha, MFA, scan confirmation, or platform risk
  controls.
- Do not print cookies, session payloads, QR images, tokens, API keys, session
  secrets, passwords, webhook URLs, or full generated chapter text.
- Do not use the legacy Discord QR handoff as part of the experiment.
- Do not treat historical completed projects as sufficient proof of this new
  controlled experiment.

## Approaches Considered

### Recommended: New Controlled Production Project

Create one clearly named test project through MCP, complete Genesis, generate a
small number of long chapters, exercise pause/resume where the task state makes
that safe, then upload one generated chapter as `publish=false` through existing
publisher work bindings.

This best satisfies the remaining objective because it proves the current
deployed code can run the full path now, not only that historical data exists.
It also keeps the blast radius low: one test project, one or two generated
upload jobs, no platform book creation, and no public publication.

### Alternative: Reuse An Existing Writing Project

Continue an existing project such as the 240-chapter production test. This
reduces Genesis cost and avoids creating another project, but it weakens the
proof because the objective explicitly includes creating a test project and
completing Genesis. It also risks disturbing known review gates on existing
long-run projects.

### Alternative: Evidence-Only Audit

Use the already completed 60-chapter projects, existing upload jobs, and
supervisor records as proof. This has the lowest operational risk, but it does
not prove the current deployment can complete a fresh Genesis-to-upload flow.

## Design

### Operator Path

All project, Genesis, task, and chapter operations use the ForWin MCP endpoint
at `http://127.0.0.1:8896/mcp` after
`scripts/check_codex_operator_ready.py` passes. The operator reads state before
each mutation:

1. `project_list` and `task_active_generation_check`.
2. `project_create` for a named test project when no suitable active experiment
   project exists.
3. `genesis_get`, then `genesis_stage_generate`, `genesis_stage_refine` only
   when needed, and `genesis_stage_lock` until Genesis is ready.
4. `task_active_generation_check` for the project.
5. `project_start_writing` with a tight chapter limit.
6. `task_get` or `task_list` polling until terminal, paused, or gated.
7. `chapter_list` and `chapter_get` for inspection.
8. `project_continue_generation` only when the project is already in writing
   state, no active generation task exists, and the next gate allows it.

If a task is active, the operator observes it and does not start another task.
If a task must stop safely, the operator uses `task_pause` and polls until the
pause is visible.

### Test Project

Use a production-safe test title with a timestamp, for example:

`ForWin系统测试-长文发布前验证-20260701`

The premise should be short, fiction-safe, and clearly marked as test content.
The target chapter count should be small enough for a controlled run, such as
3 to 5 chapters. The experiment records the project id and target count.

If a matching unfinished experiment project from the same day exists, prefer
continuing it over creating another project. This prevents project clutter.

### Generation Window

The first generation pass targets one long chapter. If it completes without
active-task conflicts, empty chapters, duplicate body detection, review hard
failures, or stuck states, continue to at least one additional chapter. The
normal cap for this experiment is 2 to 3 generated chapters unless the run is
already clean and the operator has enough time/cost budget to continue.

The generated-content report records:

- generated chapter count
- accepted/drafted/needs-review/planned counts
- approximate body character counts
- summary presence
- residual issue count when available
- failed chapter numbers
- retry and error information when available from task or observability data

The report must not include the full chapter body.

### Pause And Resume

Pause/resume is exercised only when the generation task is running or the
project has a supported resumable state. The safe sequence is:

1. Observe the active task with `task_get`.
2. Call `task_pause`.
3. Poll until the task reaches paused or terminal state.
4. Confirm no active generation task with `task_active_generation_check`.
5. If the project gate allows it, call `project_continue_generation`.
6. Poll the new task to terminal or next gate.

If the first generation task finishes before the pause can be requested, record
that the task completed too quickly for pause and perform resume only if the
project has a valid continuation gate. Do not manufacture a pause by killing a
service or interrupting the worker process.

### WorldModel, BookState, And Observability

For each generated chapter inspected, gather read-only summaries from:

- `world_model_get`
- `world_page_get`
- `world_conflict_list`
- `/api/projects/{project_id}/book-state/nodes`
- `/api/projects/{project_id}/book-state/edges`
- `/api/projects/{project_id}/book-state/deltas`
- `/api/observability/performance/tasks/{task_id}`
- `/api/observability/performance/projects/{project_id}`
- `/api/observability/performance/projects/{project_id}/chapters/{chapter_number}`
- `/api/observability/performance/llm`
- `/api/observability/performance/db`

The report stores status codes, counts, top-level keys, and conflict counts.
It does not dump large payloads.

### Publisher Upload

Publisher upload uses the existing browser login/session path:

1. Run `scripts/check_production_publisher_baseline.py`.
2. Confirm both platforms connected and no Discord env violations.
3. Confirm existing work bindings for Fanqie and Qidian.
4. Use `POST /api/projects/{project_id}/publishers/upload-jobs` with
   `publish=false` and the generated chapter number.
5. Poll `GET /api/publishers/upload-jobs/{job_id}` until terminal.
6. Record job status, platform, safe book name, safe chapter title, timestamps,
   and message.

If a platform reports missing binding, missing book, login-required, captcha,
MFA, risk control, or quota/audit block, stop that platform's upload attempt
and record the smallest human action. Do not create a book automatically and do
not publish.

### Platform Quota Protection

The experiment assumes that `publish=false` uploads to existing bound works are
allowed because this path has already succeeded in production. It does not
assume permission to create new books or publish public content.

Before any future `publish=true` verification, a separate quota and cleanup
check must confirm:

- new-book limits
- draft limits
- upload/publish frequency limits
- chapter length limits
- required cover/category/intro/tag fields
- audit and takedown behavior
- whether test books or test chapters are allowed

That check is outside this experiment.

### Error Handling

- Active generation conflict: observe existing task; do not start another.
- Genesis blocked: report the stage and required operator decision.
- Review gate: report chapter and gate; do not auto-approve unless a separate
  review policy explicitly permits it.
- Worker no-work: record as environment evidence, not generation proof.
- Upload failed after a newer same-platform success: keep historical failure in
  JSON but do not treat it as a current blocker.
- Login expired: report `publisher_login_required`; do not use Discord QR.
- Platform risk control: record platform and safe page state only.
- Missing binding: do not create a platform book automatically.

## Verification

Before running the experiment:

```bash
python scripts/check_codex_operator_ready.py
python scripts/check_production_publisher_baseline.py \
  --api-base http://10.0.0.126:8899 \
  --mcp-health-url http://10.0.0.126:8896/health \
  --docker-context swarm-manager-150 \
  --colima-profile swarmbridged
```

During the experiment, the authoritative evidence is MCP tool output for
project/task/chapter state, read-only API summaries for BookState and
observability, and upload-job API state for publisher flow.

After the experiment:

```bash
python scripts/supervise_forwin_interventions.py \
  --api-base http://127.0.0.1:8899 \
  --mcp-url http://127.0.0.1:8896/mcp \
  --expect-platform-connected fanqie \
  --expect-platform-connected qidian \
  --skip-github
```

The final report is acceptable only if:

- no active generation task is left unintentionally running
- no upload job is left pending or running unintentionally
- both platforms remain connected or a minimal human login action is recorded
- all generated content remains unpublished
- structured evidence covers every goal above

## Testing And Regression Coverage

If the experiment exposes a code bug, add focused regression tests before
fixing it. Candidate test areas include:

- MCP generation gating and active-task checks
- supervisor classification of recovered failures
- publisher upload job state transitions
- project upload-job API validation
- redaction of generated bodies and sensitive browser/session fields
- BookState and observability response summarization helpers if a helper script
  is added

Do not add broad tests for platform-specific browser behavior unless the bug is
reproducible without real platform credentials or can be safely simulated.

## Deliverables

- A structured experiment log in a local ignored directory such as
  `.codex-monitor/`.
- A concise final summary with project id, task ids, chapter numbers, safe
  upload job ids, statuses, and blocked items.
- Code fixes and tests only for issues found during the experiment.
- Documentation updates only if the experiment changes the operating procedure.

