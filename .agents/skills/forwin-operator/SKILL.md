---
name: forwin-operator
description: Use when operating the ForWin backend through its MCP tools for project creation, Genesis progression, writing handoff, task control, or chapter inspection. Prefer this skill whenever Codex needs to work with a running ForWin instance instead of reading raw SQLite state or improvising the workflow.
---

# ForWin Operator

## Overview

This skill teaches Codex how to operate ForWin as a workflow system. Use the `forwin` MCP server as the authoritative interface for project, Genesis, task, and chapter actions.

## When To Use The MCP Server

Use `forwin` MCP when you need to:

- create or inspect a project
- inspect or advance Genesis
- start writing or continue generation
- inspect, poll, or safely pause a generation task
- read chapter summaries or full chapter drafts

If a matching MCP tool exists, do not inspect the SQLite database or improvise raw HTTP calls.

## Canonical Flows

### New Book

1. Call `project_create`.
2. Call `genesis_get`.
3. Use `genesis_stage_generate`, `genesis_stage_refine`, and `genesis_stage_lock` until Genesis is ready.
4. Confirm there is no active task with `task_active_generation_check`.
5. Call `project_start_writing`.
6. Poll with `task_get` or `task_list`.

### Resume Project

1. Call `project_get`.
2. Call `task_active_generation_check` for the project.
3. If a task is already active, inspect it with `task_get` instead of starting another.
4. If no task is active and the project is already in writing state, call `project_continue_generation`.

### Inspect Recent Output

1. Call `project_get` or `chapter_list`.
2. Choose a chapter number from `chapter_list`.
3. Call `chapter_get` for the full chapter body and summary.

## Common Mistakes

- Do not treat `project_create` as equivalent to “start writing”.
- Do not call `project_start_writing` before Genesis is actually ready.
- Do not call `project_continue_generation` if `task_active_generation_check` says generation is active.
- Prefer `task_pause` instead of terminate/restart behavior when you need a safe stop.
- Read state first, mutate second.

## Example Prompts

- Use `$forwin-operator` to create a new ForWin project, inspect Genesis, and tell me which stage should be locked next.
- Use `$forwin-operator` to check whether project `abc123` already has an active generation task before continuing generation.
- Use `$forwin-operator` to inspect the newest chapter output for project `abc123` using `chapter_list` and `chapter_get`.
