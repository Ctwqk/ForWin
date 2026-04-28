---
name: forwin-operator
description: Use when operating the ForWin backend through its MCP tools for project creation, Genesis progression, writing handoff, task control, chapter inspection, or WorldModel reads. Prefer this skill whenever Codex needs to work with a running ForWin instance instead of reading raw SQLite state or improvising HTTP calls.
---

# ForWin Operator

## Overview

Use the `forwin` MCP server as the authoritative interface for project, Genesis, task, chapter, and WorldModel workflows.

The plugin provides the operator policy. The MCP server provides the live tools. Do not inspect SQLite directly or send raw HTTP requests when an equivalent `forwin` MCP tool exists.

## Preflight

Before operating a running backend from the ForWin server, confirm the operator path is available:

1. `python3 scripts/check_codex_operator_ready.py`
2. If API or MCP health fails, run `docker compose up -d forwin forwin-mcp`.
3. Global `codex mcp list` registration is optional when this plugin supplies the MCP definition.

If readiness says the ForWin API or MCP endpoint is unavailable, report the setup problem and stop. Do not compensate by bypassing MCP.

## Canonical Flows

### New Book

1. Call `project_create`.
2. Call `genesis_get`.
3. Use `genesis_stage_generate`, `genesis_stage_refine`, and `genesis_stage_lock` until Genesis is ready.
4. Call `task_active_generation_check`.
5. Call `project_start_writing` only when Genesis is ready and no active generation task exists.
6. Poll with `task_get` or `task_list`.

### Resume Project

1. Call `project_get`.
2. Call `task_active_generation_check` for the project.
3. If a task is active, inspect it with `task_get` instead of starting another.
4. If no task is active and the project is in writing state, call `project_continue_generation`.

### Inspect Output

1. Call `project_get` or `chapter_list`.
2. Choose a chapter number from `chapter_list`.
3. Call `chapter_get` for body text, summary, and review state.
4. Use `world_model_get`, `world_page_get`, and `world_conflict_list` for canonical world-state reads.

### Safe Stop

1. Call `task_get` or `task_list` to identify the active generation task.
2. Prefer `task_pause` for a safe checkpointed stop.
3. Poll with `task_get` until paused or terminal.

## Tool Boundaries

- `project_create` creates a Genesis-backed project. It does not create a writing-ready project.
- `project_start_writing` is the only supported handoff from Genesis into chapter production.
- `project_continue_generation` must not be called while `task_active_generation_check` reports active generation.
- `chapter_list` and `chapter_get` are the supported chapter inspection path.
- Treat Codex Bridge as ForWin's background model route, not as the operator path. Operator actions still go through MCP.

## Common Mistakes

- Do not assume `creation_status="creating"` or `creation_status="genesis_ready"` can write chapters immediately.
- Do not invent alternate Genesis stage transitions.
- Do not terminate or restart a generation task when `task_pause` can stop safely.
- Read state first, mutate second.
