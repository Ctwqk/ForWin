# V5 Pre-Roadmap Soak Snapshot

Snapshot date: 2026-07-12 PDT

```yaml
classification: pre-roadmap-soak
release_evidence: false
reason: roadmap changes start after this snapshot
```

This document freezes the observable state of the 200-chapter run that started
before the v5 final roadmap. The project is retained as a read-only regression
sample. It must not be resumed, mutated, or counted as V6 release evidence.

## Runtime Identity

| Field | Snapshot value |
|---|---|
| source HEAD | `b38331de337331921f362dc500876e0ce3e71f2a` |
| deployed code SHA | `57241ff0e4e23905f34c20c405e7d2cea89dc28b` |
| deployed application image | `forwin-forwin:deploy-57241ff0e4e2` |
| deployed browser image | `forwin-publisher-browser:deploy-57241ff0e4e2` |
| run-start deploy | `4b6102fc1ef9674454ff012230e0c631b69bf13a` |
| deploy history during run | `4b6102f -> 3b93ac6 -> c839ea7 -> b497c4b -> 57241ff` |
| project | `a06cf00db3ba4cbe8b9862e20e9d6248` (`R12·逆时签名`) |
| initial generation task | `83b149933eb5` |
| active-at-snapshot generation task | `0b9bfd7f0835` |
| RuntimePolicy version | `2` |
| current project quality profile | `standard` |
| frozen task quality profile | `unknown`; Task API exposes policy version but not the frozen execution payload |
| gate delegate | `spark` |
| target chapters | `200` |
| materialized plans | `35` |
| generated / accepted | `16 / 16` |
| needs review | `0` |
| active task failures | `0` |
| historical recovered failure | initial task failed chapter 12; continuation accepted chapters 12-16 |
| overall run start | `2026-07-12 17:01:16 PDT` |
| continuation start | `2026-07-12 23:13:23 PDT` |
| last accepted Canon progress | chapter 16 at `2026-07-12 23:51:42 PDT` |
| last worker heartbeat | `2026-07-12 23:57:28 PDT` |

This run crossed five deployed revisions. It is therefore not a fixed-commit
no-hotfix run even before the final-roadmap changes are considered.

At the pre-pause read, task `0b9bfd7f0835` was writing chapter 17 with lease
owner `forwin-generation-worker-swarm`, heartbeat
`2026-07-12 23:53:28 PDT`, and lease expiry
`2026-07-12 23:58:28 PDT`.

## Safe Stop

The pause was requested through the supported ForWin MCP `task_pause` tool at
`2026-07-12 23:54:47 PDT`.

The task reached a safe checkpoint at `2026-07-12 23:57:29 PDT`:

| Check | Result |
|---|---|
| task status / stage | `paused / paused` |
| current chapter | `17` (not accepted) |
| accepted chapters after stop | `1-16` |
| `task_active_generation_check.active_count` | `0` |
| `task_active_generation_check.safe_to_restart` | `true` |
| stop reason | destructive v5 final-roadmap work starts after this snapshot |

The paused task retains its final lease owner and expiry fields as historical
metadata (`forwin-generation-worker-swarm`,
`2026-07-13 00:02:28 PDT`). The authoritative active-task check confirms that
there is no active lease. Swarm generation-worker logs record
`task_cleanup_finished` at `2026-07-13 06:57:29 UTC`, followed by
`no_claimable_generation_task`.

## Decision Events

At `2026-07-12 23:57:29 PDT` the project had **810 unique DecisionEvent rows
observable through the supported API** (`exact=true`), with zero duplicate IDs
in the snapshot export.

The normal project endpoint is capped at 200 rows. To avoid presenting a capped
window as a total, the export was partitioned across the five scopes actually
present in this project:

- `band`: 5 rows; `character_creation`: 70 rows; `project`: 57 rows.
- `task`: 190 rows for `83b149933eb5` and 98 for `0b9bfd7f0835`, read from
  each task timeline.
- `chapter`: 390 rows, read separately for chapters 1-35; the largest chapter
  partition contained 44 rows, below the endpoint cap.

| Event type | Count | Event type | Count |
|---|---:|---|---:|
| `arc_activation_review_pack_built` | 1 | `auto_continue_decision` | 2 |
| `canon_commit` | 16 | `character_created` | 6 |
| `character_merged_existing` | 54 | `character_roster_materialized` | 2 |
| `context_assembled` | 17 | `context_pruned` | 16 |
| `continue_requested` | 2 | `entity_alias_registered` | 1 |
| `entity_background_generic` | 15 | `entity_registered` | 3 |
| `fallback_profile_switched` | 80 | `future_plan_audit_run` | 33 |
| `generation_audit_checkpoint_reached` | 2 | `generation_worker_claimed` | 2 |
| `generation_worker_heartbeat_failed` | 2 | `genesis_created` | 1 |
| `genesis_stage_generated` | 6 | `genesis_stage_locked` | 6 |
| `llm_request_failed` | 68 | `llm_request_started` | 18 |
| `llm_request_succeeded` | 20 | `map_generation_started` | 1 |
| `map_generation_succeeded` | 1 | `pause_requested` | 1 |
| `personality_loadout_auto_assigned` | 8 | `project_created` | 1 |
| `provisional_gate_evaluated` | 2 | `publisher_canon_available` | 16 |
| `pulp_beat_evaluated` | 16 | `repair_started` | 1 |
| `repair_succeeded` | 1 | `review_verdict_recorded` | 33 |
| `rule_decision_evaluated` | 17 | `run_started` | 2 |
| `runtime_policy_updated` | 1 | `scenario_rehearsal_evaluated` | 5 |
| `stage_duration_summary` | 107 | `stage_entered` | 91 |
| `stage_exited` | 89 | `start_writing_requested` | 1 |
| `task_cleanup_finished` | 2 | `task_cleanup_started` | 2 |
| `task_operation_started` | 2 | `task_operation_succeeded` | 2 |
| `writer_output_artifact_saved` | 17 | `writer_output_built` | 18 |

## Canon, Trace, Outbox, Projection, And Publisher

Observed at `2026-07-12 23:57:29 PDT` unless a row states another timestamp.

| Evidence | Snapshot value | Exact | Basis |
|---|---:|---|---|
| PromptTrace | 42 | yes | unique IDs from chapter observability ledgers; project query cap 500 was not reached |
| CanonCommitRecord total row count | `unknown` | no | the deployed build has no CanonCommitRecord query/count surface |
| observable Canon commit identities | 16 | yes | unique `canon_commit:*` references from accepted chapter review layers |
| expected Canon outbox rows | 32 | no | design-derived expectation: two frozen outbox events per Canon plan |
| pending outbox | `unknown` | no | no read-only outbox status endpoint exists in the deployed build |
| failed outbox | `unknown` | no | no read-only outbox status endpoint exists in the deployed build |
| downstream outbox evidence | complete for chapters 1-16 | yes | 16 publisher availability events plus projection at chapter 16 |
| projection page count | 183 | yes | `GET /api/projects/{id}/projections/status` |
| projection version / kind | `obsidian_v2 / obsidian` | yes | projection page API |
| projection as-of chapter | 16 | yes | all 183 pages report chapter 16 |
| projection lag | 0 chapters | yes | latest accepted Canon is chapter 16 |
| latest projection update | `2026-07-13 06:51:44 UTC` | yes | two seconds after chapter 16 Canon event |
| complete publisher backlog | `unknown` | no | internal reviewed-unpublished backlog has no operator query surface |
| publisher upload-job queue | 0 | yes | upload-job list returned zero rows globally and for this project |

The deployed build cannot expose exact outbox row statuses without bypassing the
supported operational API. Canon record totals and the complete publisher
backlog have the same limitation. This snapshot records them as `unknown`
instead of substituting accepted-chapter counts or upload-job counts. The
complete chapter-16 projection and all 16 publisher notifications are positive
delivery evidence, but are not relabeled as exact queue-depth measurements.

## Log Coverage

The tracked `.forwin-run-logs/current-run-id` points to
`200ch-20260708-205716` / project
`5637d2b7022e4a69a33e00298f2a6f50`. No tracked run-log directory contains this
snapshot's project or task IDs, so those older logs were not mixed into the
evidence. Live Swarm service logs were used only to cross-check deployment
identity and safe cleanup.

## Disposition

- Never call continue/resume for either task in this project.
- Do not delete or clean the project; retain it read-only for regression
  comparison.
- Do not cite this run as R10 or V6 evidence.
- Any final 200-chapter proof must use a new project created after R9 freezes the
  release candidate.
