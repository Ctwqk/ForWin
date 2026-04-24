# ForWin Operator Rules

Use the `forwin` MCP server for project, Genesis, task, and chapter truth whenever it is configured. Do not inspect the SQLite database or send ad hoc `curl` requests for those workflows when an equivalent `forwin` MCP tool exists.

## Workflow Rules

- `project_create` creates a Genesis-backed project. It does not create a writing-ready project.
- `project_start_writing` is the only handoff from Genesis into chapter production.
- Before `project_start_writing` or `project_continue_generation`, call `task_active_generation_check` or read the project/task state to confirm there is no active generation task.
- Prefer `task_pause` over kill/restart behavior when generation must stop safely.
- Read state first, mutate second. Typical order is `project_get` or `genesis_get` or `task_get` before any write tool.

## Canonical Tool Choices

- Use `project_list` and `project_get` for project discovery and authoritative status.
- Use `genesis_get`, `genesis_stage_generate`, `genesis_stage_refine`, and `genesis_stage_lock` for Genesis work. Do not invent alternate stage transitions.
- Use `task_list`, `task_get`, and `task_active_generation_check` for generation monitoring.
- Use `chapter_list` and `chapter_get` to inspect chapter output instead of reading raw DB rows.

## Do Not

- Do not assume a project with `creation_status="creating"` or `creation_status="genesis_ready"` can write chapters immediately.
- Do not call `project_continue_generation` against a project that still has an active task.
- Do not bypass ForWin workflow rules by writing directly to project, task, or chapter tables.
