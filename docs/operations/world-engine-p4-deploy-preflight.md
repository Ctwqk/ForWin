# World engine P4 deployment preflight

Carry this check into the combined P0–P4 deployment runbook in P4b. This document
records a release check; it does not authorize a deployment or data repair.

Before enabling chapter generation, the release operator checks the deployment
snapshot for writing projects with a non-superseded arc whose end precedes its
start (including older rows with the default `chapter_end = 0`):

```sql
SELECT p.id AS project_id, a.id AS arc_plan_id, a.status,
       a.chapter_start, a.chapter_end
FROM projects AS p
JOIN arc_plan_versions AS a ON a.project_id = p.id
WHERE p.creation_status = 'writing'
  AND a.status <> 'superseded'
  AND a.chapter_end < a.chapter_start
ORDER BY p.id, a.chapter_start, a.id;
```

Record every affected project and arc in the release checklist. Use ForWin MCP
`project_get`, `task_active_generation_check`, and chapter inspection to review
its current state. Pause active generation through `task_pause` before an
approved recovery. Reconcile arc ranges against the approved Genesis blueprint
through the owning application workflow; do not directly update arc, task, or
chapter tables.

Until it is repaired, such a project has nothing writable: the continue
workset reports it as completed and logs `project <id> has an empty arc chapter
range`. Search the runtime logs for that line as a second check. Books needing
fresh band plans require valid arc ranges before resuming. After recovery, repeat the preflight and verify that the workset skips
accepted chapters and that a historical rewrite leaves the current arc active.
