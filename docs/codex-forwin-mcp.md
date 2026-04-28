# ForWin MCP Operator Runbook For Codex

Codex should operate a running ForWin backend through the `forwin` MCP server. For project, Genesis, task, chapter, and WorldModel workflows, MCP is the authoritative interface. Do not inspect SQLite directly or send ad hoc HTTP requests when an equivalent MCP tool exists.

## Operator Preflight

Install or enable the repo-local plugin from `.agents/plugins/marketplace.json`, then invoke it from Codex as:

```text
[@forwin-operator](plugin://forwin-operator@forwin-local)
```

The plugin contributes the `forwin-operator` skill and a `forwin` MCP server definition pointing at `http://127.0.0.1:8896/mcp`.

On the ForWin server, make sure the backend and MCP endpoint are reachable from the Codex process:

```bash
python3 scripts/check_codex_operator_ready.py
```

Expected required readiness:

- `http://127.0.0.1:8899/health` returns healthy.
- `http://127.0.0.1:8896/health` returns healthy.
- The repo-local plugin declares `forwin` MCP at `http://127.0.0.1:8896/mcp`.

The checker also prints optional diagnostics for Docker Compose visibility, global `codex mcp list` registration, and Python test dependencies. These diagnostics are warnings by default because plugin invocation can provide the MCP definition without global Codex registration.

If you want to require the optional local development checks too, run:

```bash
python3 scripts/check_codex_operator_ready.py --strict
```

If the API or MCP health checks fail, start the backend stack:

```bash
docker compose up -d forwin forwin-mcp
```

Global MCP registration is optional for plugin use. It is only needed when you want to use the `forwin` MCP server outside this plugin:

```bash
codex mcp add forwin --url http://127.0.0.1:8896/mcp
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
4. If there is no active task and the project is not blocked by review or governance gates, call `project_continue_generation`.

### Inspect Output

1. Call `project_get` for project status and counts.
2. Call `chapter_list` to choose a chapter number.
3. Call `chapter_get` for body text, summary, review status, and residual issues.
4. Use `world_model_get`, `world_page_get`, and `world_conflict_list` for canonical world-state inspection.

### Safe Stop

1. Call `task_get` or `task_list` to identify the active generation task.
2. Prefer `task_pause` for a safe checkpointed stop.
3. Continue polling with `task_get` until the task reaches a paused or terminal state.

## Tool Boundaries

- `project_create` creates a Genesis-backed project. It does not make the project writing-ready.
- `project_start_writing` is the only supported handoff from Genesis into chapter production.
- `project_continue_generation` must not be called while `task_active_generation_check` reports active generation.
- `chapter_list` and `chapter_get` are the supported chapter inspection path.
- `world_export_obsidian` is a read-oriented export workflow; canon changes still go through ForWin governance.

## Verification

Run the focused operator and Codex integration tests from the repository virtualenv:

```bash
.venv/bin/python -m pytest \
  tests/test_mcp_server.py \
  tests/test_codex_bridge.py \
  tests/test_llm_router.py \
  tests/test_codex_governance.py \
  tests/test_codex_operator_ready.py \
  -q
```
