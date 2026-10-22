# ForWin MCP Operator Runbook For Codex

Codex should operate a running ForWin backend through the `forwin` MCP server. For project, Genesis, task, chapter, and WorldModel workflows, MCP is the authoritative interface. Do not inspect PostgreSQL directly or send ad hoc HTTP requests when an equivalent MCP tool exists.

## Operator Preflight

Install or enable the repo-local plugin from `.agents/plugins/marketplace.json`, then invoke it from Codex as:

```text
[@forwin-operator](plugin://forwin-operator@forwin-local)
```

The plugin contributes the `forwin-operator` skill and a `forwin` MCP server definition pointing at the Swarm manager LAN endpoint, `http://ccttww-lap.local:8896/mcp`.

On the ForWin server, make sure the backend and MCP endpoint are reachable from the Codex process:

```bash
python3 scripts/check_codex_operator_ready.py
```

Expected required readiness:

- `http://ccttww-lap.local:8899/health` returns healthy.
- `http://ccttww-lap.local:8896/health` returns healthy.
- The repo-local plugin declares `forwin` MCP at `http://ccttww-lap.local:8896/mcp`.

The checker also prints optional diagnostics for Docker Compose visibility, global `codex mcp list` registration, and Python test dependencies. These diagnostics are warnings by default because plugin invocation can provide the MCP definition without global Codex registration.

If you want to require the optional local development checks too, run:

```bash
python3 scripts/check_codex_operator_ready.py --strict
```

If the API or MCP health checks fail, inspect the deployed Swarm services:

```bash
docker --context swarm-manager-150 service ls --filter name=forwin
```

Recover the deployment through the approved 150 deploy sync path. Use local Docker Compose only when intentionally operating a separate development stack and pass all three endpoint overrides to the readiness script.

Global MCP registration is optional for plugin use. It is only needed when you want to use the `forwin` MCP server outside this plugin:

```bash
codex mcp add forwin --url http://ccttww-lap.local:8896/mcp
codex mcp list
```

If the MCP server is missing or unhealthy, stop and fix the operator environment. Do not fall back to raw database inspection for project/task/chapter truth.

## Canonical Workflows

### New Book

1. Call `project_create` with title, premise, genre, and target chapter count.
2. Call `genesis_get` and inspect `creation_status`, `can_start_writing`, and stage states.
3. Use `genesis_stage_generate`, `genesis_stage_refine`, and `genesis_stage_lock` for each Genesis stage.
4. Before handoff, call `task_active_generation_check` for the project.
5. Call `project_start_writing` only when Genesis is complete and no active generation task exists.
6. Poll the returned task with `task_get` or recent work with `task_list`.

### Resume Writing

1. Call `project_get` to confirm the project is already in writing state.
2. Call `task_active_generation_check` for the project.
3. If an active task exists, inspect it with `task_get`; do not start another generation task.
4. If there is no active task and the project is not blocked by review or project-control gates, call `project_continue_generation`.

### Inspect Output

1. Call `project_get` for project status and counts.
2. Call `chapter_list` to choose a chapter number.
3. Call `chapter_get` for body text, summary, review status, and residual issues.
4. Use `world_model_get`, `world_page_get`, and `world_model_signal_list` for canonical world-state and legacy CanonQualitySignal inspection.
   Use `world_conflict_list(project_id, status="open")` and `world_conflict_get(project_id, conflict_id)` to inspect world–plan conflicts. After the user chooses an offered option, call `world_conflict_resolve(project_id, conflict_id, option, rationale, reason)`; reason is required. The decision is recorded as human/manual_ui. Closed or stale conflicts return 409, unavailable options return 400, and missing or cross-project conflicts return 404.
   World–plan conflicts use `/api/projects/{project_id}/world-conflicts`; legacy signals remain at `/api/projects/{project_id}/world-model/conflicts`.

### Safe Stop

1. Call `task_get` or `task_list` to identify the active generation task.
2. Prefer `task_pause` for a safe checkpointed stop.
3. Continue polling with `task_get` until the task reaches a paused or terminal state.

### Recover Platform Uploads

1. Record the deployed source/image identity and inspect the running endpoint contract before diagnosing a missing upload job. A newer checkout's Canon-job release prerequisite does not prove that an older deployed project upload endpoint cannot create a job. Use the project's supported endpoint for its actual runtime; never substitute a raw database write or skip that runtime's gates.
2. Inspect accepted chapter content through MCP, then inspect jobs, bindings, and the existing remote work before enqueueing. Older bindings can lack chapter numbers and remote IDs. Failed or cancelled jobs can still have saved remote drafts; reconcile remote identity and the complete normalized body before retrying.
3. Wait for the platform's chapter list to finish loading. An initial empty list or zero word count during page initialization is not evidence of missing content. Reopen the saved chapter, compare its complete body with the accepted source, and record the remote book/chapter IDs and normalized SHA-256.
4. Keep draft upload, submission for review, and public availability as separate outcomes. `publish=false` cannot meet a request for public release. With publication authorized, advance the verified chapter through the platform's release flow and record the actual audit state. An expired inspection schedule or repeated unchanged observations do not count as recovery.
5. ForWin-generated chapters must disclose AI use truthfully. The extension selects “是” on Fanqie's recognized AI disclosure form and stops if that selection cannot be verified. Draft jobs never confirm publication. Unknown dialogs require inspection, not generic “next” clicks.
6. Keep generation and publication blockers separate. A later chapter's Canon repair does not itself prevent uploading an earlier accepted chapter. A login problem on another platform does not block Fanqie. Preserve genuine missing-provenance gates and published content.

These instructions do not authorize an incompatible deployment. Source fixes must follow the approved complete-image and forward-migration release process; do not patch production source strings or claim a local test changed the running extension.

## Tool Boundaries

- `project_create` creates a Genesis-backed project. It does not make the project writing-ready.
- `project_start_writing` is the only supported handoff from Genesis into chapter production.
- `project_continue_generation` must not be called while `task_active_generation_check` reports active generation.
- The removed `/api/generate` shortcut is not a valid workflow. MCP, CLI, and the web console all use project creation, Genesis handoff, and durable continuation tasks.
- `chapter_list` and `chapter_get` are the supported chapter inspection path.
- `world_export_obsidian` is a read-oriented export workflow; canon changes still go through candidate review and Canon admission.

The HTTP Review detail used by the operator console reports five independent
layers: draft review, repair, residual eligibility, gate delegation, and Canon.
An approval event is not proof of Canon admission; Canon truth requires the
candidate's committed status and commit identity.

## Verification

Run the focused operator and Codex integration tests from the repository virtualenv:

```bash
.venv/bin/python -m pytest \
  tests/test_mcp_server.py \
  tests/test_codex_bridge.py \
  tests/test_llm_router.py \
  tests/test_codex_governed_actions.py \
  tests/test_codex_operator_ready.py \
  -q
```
