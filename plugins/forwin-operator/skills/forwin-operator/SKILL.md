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
2. If API or MCP health fails, inspect `docker --context swarm-manager-150 service ls --filter name=forwin` and recover through the approved 150 deploy sync path.
3. Global `codex mcp list` registration is optional when this plugin supplies the MCP definition.

If readiness says the ForWin API or MCP endpoint is unavailable, report the setup problem and stop. Do not compensate by bypassing MCP.

The repository defaults to the Swarm manager endpoints at `ccttww-lap.local`. Use the readiness command's explicit URL flags only when intentionally operating a separate local development stack.

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
4. Use `world_model_get`, `world_page_get`, and `world_model_signal_list` for canonical world-state and legacy CanonQualitySignal inspection.
   Use `world_conflict_list(project_id, status="open")` and `world_conflict_get(project_id, conflict_id)` to inspect world–plan conflicts. After the user chooses an offered option, call `world_conflict_resolve(project_id, conflict_id, option, rationale, reason)`; reason is required. The decision is recorded as human/manual_ui. Closed or stale conflicts return 409, unavailable options return 400, and missing or cross-project conflicts return 404.
   World–plan conflicts use `/api/projects/{project_id}/world-conflicts`; legacy signals remain at `/api/projects/{project_id}/world-model/conflicts`.

### P4a Planning And Conflict Recovery

- Source schema head is `0006_planning_v6` (parent `0005_world_plan_conflicts`); inspect the running service before assuming it matches. Handoff creates `arc_beat_sheets` and protections, with fixed Genesis arc ranges; `band_plans` and ChapterPlans are prepared just in time. `plan.arc_beats` / `plan.band` produce PromptTrace and decision evidence.
- Post-Canon order is `("world", "plan", "feedback")`, then order controls. Plan separately commits arc activation, beat-sheet work, audit/replanning and next-band preparation before committing step completion and trace outbox; feedback analyzes comments before aggregation. Synchronous raw-artifact store errors can fail the current unit/step; retries retain and conditionally reuse earlier committed planning work. Post-commit outbox retries only upload.
- Read `world_conflict_list` / `world_conflict_get` for `plan_feasibility` or `band_planning` issues. Both require human `amend_plan` with rationale; PlanAmender revises the beat sheet for milestone/exit-state problems, otherwise forces replanning. World-tick amendments commit the change, release protections with `payload.status = "released"`, then replan remaining chapters; a failed replan leaves a follow-up conflict. Use only returned options.
- Missing future plans leave plot_responds changes pending and accept_with_obligation obligations open for the next band; this alone does not require escalation. Saved responses attach plan_patch: result refs.
- `chapter_extraction` conflicts come from a chapter's text that still breaks a protection or milestone after the draft repair loop. The chapter is paused in `needs_review` with its draft kept, and `blocking_reason.conflict_source` reads `chapter_extraction`. Options: `rewrite_chapter` sends the saved draft back to repair with the boundary as must_fix; `amend_plan` (human only) accepts the text and releases the protection, or revises the beat sheet for a milestone. Approving that chapter's review returns 409 until the conflict is resolved, and resolving returns 409 while a generation task is active. Resolve, reread `project_get` and `task_active_generation_check`, then call `project_continue_generation`; the chapter resumes from the saved draft without a new writer pass.
- Replanning preserves accepted/drafted/needs_review rows and legacy rows (null band_plan_id) with draft/candidate history. Never-drafted legacy planned/failed rows are planned in place before writing. Pre-write `ensure_plan_for_chapter` reuses existing band-owned rows, but band-owned planned/failed rows remain eligible for later explicit or automatic replanning even with draft history. Run control directly reuses existing rows before the active arc, including never-drafted legacy rows; completed arcs never reactivate. Empty/degenerate arc ranges have no writable chapters and log a warning. Publication checks the caller's observed band version.
- `ChapterWorldView` provides POV-safe writer knowledge, recorded reader facts, visible routes, onstage state, clocks and reveal/hint directives (standard 6000/pulp 4000 chars). Author-view bindings/how/ref never render to the writer; goals carry their actions and payoff layers are projected by chapter. Feasibility rejects writer secret propositions (`writer_text_states_secret`). Extraction reuse requires both body hash and plan fingerprint. Independent access rules/team speed/edge multipliers are replaced by per-book travel modes.
- P4b remains planned: residual chapter_extraction conflicts/rewrite_chapter/resume, tick retirement/re-run and vanished refs, world_variable/world.drift, checkable predicates, new offstage actors, task_contract_json removal, world/news/cost fixes, extraction narrowing/dead-signal cleanup, held-change cards, multi-genre tests and closing docs. Its production runbook is separate. See [current architecture](../../../../Design-docs/CURRENT_ARCHITECTURE.md) for the exact deletion list and full boundary.

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
