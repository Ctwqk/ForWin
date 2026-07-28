# ForWin v5 Release Evidence Ledger

Updated: 2026-07-28
Matrix candidate source: `80af1c83323b90f8d69321c0cabe4c98f9185f48`
Integration source: resolved from Git by the collector at execution time. This
ledger intentionally does not self-pin the commit that contains it.

Status values:

- `PROVEN`: authoritative evidence exists for the exact candidate.
- `REPEAT`: earlier evidence exists, but the release sequence requires a fresh run.
- `RUNNING`: immutable candidate work is still in progress.
- `MISSING`: required evidence has not been produced.
- `BLOCKED`: an unresolved defect or external dependency prevents progress.

This ledger is evidence bookkeeping only. A row is never promoted from test
intent, memory, or a narrower check than the stated release requirement.

## R6 - Fresh 30/60S/60P/100 Matrix

| Requirement | Status | Authoritative evidence |
| --- | --- | --- |
| Candidate source and image identity frozen | PROVEN | `.artifacts/v4-matrix-candidate/manifest.json`; runtime and browser image revision labels equal the candidate SHA |
| Tracked source unchanged during run | RUNNING | Matrix harness `assert_source_frozen`; final manifest must still report `code_changes_during_run = 0` |
| Matrix-to-RC successor delta is bounded | RUNNING | The tracked integration candidate passes `bounded_successor`: every post-matrix change is confined to the explicit R8 publisher/recovery file allowlist or tracked release-harness directory, while arbitrary HTTP and test files remain rejected; the final matrix audit and immutable RC identity are still pending |
| L30 accepted target reached | RUNNING | Final `L30/project.json`, `chapters.json`, `tasks.json`, and `run-summary.json` |
| L60 standard accepted target reached | RUNNING | Final `L60S/project.json`, `chapters.json`, `tasks.json`, and `run-summary.json` |
| L60 pulp accepted target reached | RUNNING | Final `L60P/project.json`, `chapters.json`, `tasks.json`, and `run-summary.json` |
| L100 accepted target reached with no severe data defect | RUNNING | Final `L100/project.json`, `chapters.json`, `tasks.json`, and `run-summary.json` |
| No active generation task remains | MISSING | Fresh MCP task/project reads after all four targets complete |
| One Canon identity per accepted chapter | MISSING | Final per-cell `integrity.json`; independent final SQL identity audit |
| GraphDelta/outbox identities unique | MISSING | Final per-cell `integrity.json`; independent final SQL identity audit |
| Snapshot coverage and projections converge | MISSING | Final per-cell `integrity.json`; projection status reads after backlog settles |
| GateLedger, cost, and rule provenance reports complete | MISSING | Final per-cell `gate-ledger.json`, `cost-report.json`, `rule-provenance.json` |

## R7 - A2/A4 Decisions

| Requirement | Status | Authoritative evidence |
| --- | --- | --- |
| Generalized defects fixed before RC freeze | PROVEN | `23570ab` fixes repeated zero-range GateLedger defect with focused regression evidence |
| Project-specific content defects do not become production patches | PROVEN | Matrix gate handling used supported retry/override operations; candidate SHA remained unchanged |
| Post-decision fresh smoke | MISSING | Final candidate must run a new 30-chapter project after the R8 fix; the tracked lifecycle runner binds HTTP policy mutation, MCP creation/Genesis/handoff, request hashes, exact stack/images, locked Genesis stages, redacted task-policy snapshots, Canon/projection integrity, and deterministic report content |

## R8 - Live Recovery Proof

| Fault | Status | Existing evidence | Exact RC evidence still required |
| --- | --- | --- | --- |
| Qdrant unavailable | RUNNING | Source-bound `generation_projection_recovery.py` implements the isolated real Qdrant stop/start proof; no exact-candidate live report exists | Execute the documented command and retain Canon identity, pending/replay/converged projection, endpoint, event-chain, and no-duplicate-vector evidence |
| Projection consumer unavailable | RUNNING | Source-bound `generation_projection_recovery.py` implements the outbox-worker stop/start proof; no exact-candidate live report exists | Execute the documented command and retain durable event, lease/replay, lag-zero, endpoint, and event-chain evidence |
| MinIO pre-Canon unavailable | RUNNING | Source-bound `minio_recovery.py` implements the pre-Canon object-store fault; no exact-candidate live report exists | Execute the documented command and prove admission remains non-Canon before supported retry after restore |
| MinIO post-Canon unavailable | RUNNING | Source-bound `minio_recovery.py` implements the scoped advisory-lock barrier and same-event replay proof; no exact-candidate live report exists | Execute the documented command and prove unchanged Canon/artifact/maintenance identity plus zero barrier residue |
| Publisher worker/backend unavailable | RUNNING | Source-bound `publisher_recovery.py` implements deterministic same-job reclaim, stale-token rejection, shared-path readability, and orphan cleanup without external mutation; no exact-candidate live report exists | Build the exact candidate images and execute the documented backend command |
| Publisher browser unavailable | RUNNING | Source-bound `publisher_recovery.py` implements durable browser journal/reconciliation recovery; no exact-candidate live report exists | Execute the documented browser command and prove the existing job/attempt resumes without duplicate mutation |
| CAPTCHA/MFA/account-risk | RUNNING | Source-bound `publisher_recovery.py` implements typed pause, authenticated idempotent resume, no bypass, and no receipt duplication; no exact-candidate live reports exist | Execute all three documented risk commands against the isolated candidate API/browser |
| Recovery runbook matches executable ownership | RUNNING | The tracked runbook now gives all eleven exact runner commands; runners own fresh-up/fault/recovery/destroy, and `setup_blocked` or reused identities cannot finalize | Confirm behavior with eleven new exact-candidate live directories and fault IDs |

## R9 - Release Candidate Freeze

| Requirement | Status | Authoritative evidence |
| --- | --- | --- |
| Full pytest suite green | REPEAT | `2086 passed, 1 skipped` observed at candidate SHA; must rerun after R8 and retain log/JUnit |
| Ruff green | REPEAT | Earlier candidate run green; must rerun and retain output |
| Compileall green | REPEAT | Earlier candidate run green; must rerun and retain output |
| Fresh migration upgrade/check/downgrade/upgrade and stale-role fail-fast | REPEAT | `ba0ad9d` and schema tests exist; strict isolated controller/finalizer require the structured `FORWIN_SCHEMA_REVISION_MISMATCH`, retain the raw log, and prove no task-state mutation, but the exact RC live cycle has not run |
| V1/V2/V3/V5 gates green | MISSING | Final gate manifest must identify tracked `run_rc_gates.py`, exact argv/log headers, nonempty passing JUnit, exact source tree, app images, and shared candidate hash; the runner rejects host control variables, disables ambient plugins, runs uv offline, pins Ruff 0.15.22, and seals a complete PASS against resume |
| V1 deployed all-role health and image identity | MISSING | Strict V1 preflight requires exact IDs for runtime, browser, PostgreSQL, Qdrant, and MinIO plus a passing functional probe for all nine services; no live manifest exists yet |
| V1 LAN embedding integration smoke | MISSING | V1 preflight disables proxy inheritance, records the actual private-LAN TCP peer, and requires configured, metadata, and returned vector dimensions to match; no live manifest exists yet |
| V1 supported HTTP/MCP lifecycle through accepted Canon | MISSING | The exact RC post-decision fresh-30 must carry the tracked HTTP/MCP operation chain and handoff task, then prove locked Genesis, immutable task policy, candidate, Canon, BookState/GraphDelta/Snapshot, and outbox |
| V2 real generation-worker pre/post-commit crash reclaim | RUNNING | The source-bound generation runner and deterministic advisory barriers are code-ready; exact-candidate pre/post-commit live reports do not yet exist |
| V3 fail-closed and no-direct-Canon boundary | REPEAT | Focused tests plus `GateAuditWriter` object-capability boundary; final matrix must retain real Spark model/trace evidence |
| Annotated RC tag or immutable commit record | MISSING | Final collector requires a true annotated tag object pointing at the exact tested SHA |
| Release harness tracked at candidate SHA | RUNNING | The reviewed 42-file promotion inventory and explicit 18-file executable/Compose harness are code-ready; the immutable RC candidate still must be collected from the final commit |
| One candidate identity across all evidence | RUNNING | Final collector code now recomputes source/tree, five image IDs, candidate hash, evaluator and family runner hashes, endpoint/run identity, and one Docker daemon identity; no live final set exists |
| External signed provenance | MISSING | Local hash chains are tamper-evident only under the tracked-candidate/operator-honesty model; no externally signed CI or attestation artifact exists |
| RC manifest complete | RUNNING | Collector v5 and freeze runbook are code-ready; final mode revalidates V1, bounded matrix predecessor/report, exact gate runner/argv/JUnit, eleven source-bound recovery reports/runners/event identities, fresh-30 state/operation chain and candidate-stack endpoints, annotated tag, and exact source/tree/five-image identity; no final live manifest exists |
| SHA and image tags recorded | REPEAT | Earlier draft validates runtime/browser revisions; schema v3 must be recollected after integration with exact PostgreSQL/Qdrant/MinIO IDs and tracked harness |
| Baseline schema hash recorded | PROVEN | Draft RC manifest |
| RuntimePolicy schema version recorded | PROVEN | Draft RC manifest records schema version 2 and source hash |
| Model profile/routing revisions recorded | PROVEN | Draft RC manifest records defaults, whitelisted effective model fields, route policy version, and tree hash |
| Prompt and skill registry revisions recorded | PROVEN | Draft RC manifest records deterministic tree hashes and file inventories |
| Report tool versions recorded | PROVEN | Draft RC manifest records report schema versions, harness hash, package/tool versions, and report tree hash |

## R10 - Fresh L200 No-Hotfix

| Requirement | Status | Authoritative evidence |
| --- | --- | --- |
| Fresh v5 schema and project created after RC freeze | MISSING | L200 manifest and project/Genesis reads |
| All Genesis stages generated and locked through supported API | MISSING | Genesis stage snapshots and DecisionEvents |
| Accepted chapters equal 200 | MISSING | Final project/chapter report |
| No active generation task | MISSING | Final MCP active-generation check and task inventory |
| No unresolved review outside release criteria | MISSING | Final chapter/task inventory and explicit gate decision ledger |
| One CanonCommitRecord per accepted chapter | MISSING | `canon-integrity.json` |
| No duplicate candidate/entity/alias/GraphDelta/outbox identity | MISSING | `canon-integrity.json` |
| BookState snapshots through chapter 200 | MISSING | `canon-integrity.json` and projection checkpoint reads |
| Outbox/projection converged | MISSING | `projection-publisher.json` |
| Publisher state consistent | MISSING | `projection-publisher.json` |
| S1/S3/S2 reports per band | MISSING | Strict collector `.artifacts/rc-candidate/l200_evidence.py` is ready to discover resolved `band_checkpoints` and freeze an S1/S3/S2 snapshot under `bands/`; no live L200 evidence yet |
| Release checkpoint every 25 chapters | MISSING | Collector enforces immutable 25/50/75/100/125/150/175/200 checkpoint entries and refuses final PASS if any are absent; no live L200 evidence yet |
| Task reclaim and recovery evidence recorded | MISSING | Collector is ready to combine authoritative MCP tasks with full DB lease epochs/reclaim events into `task-recovery.json`; no live L200 evidence yet |
| No code/prompt/model/config/schema/rule/threshold change | MISSING | Collector freezes SHA/tree, image IDs, container set, endpoint hashes, RC manifest, project policy, rule state, and its own hash; only a live start/end comparison can promote this row |
| Required `.artifacts/v5-l200/` output set complete | MISSING | Collector emits and hashes the exact manifest, CSV, six JSON reports, and `final-report.md`; finalization is single-shot and refuses completed or partial final output; read-only `verify-final` revalidates checkpoint/band chains, artifact contracts, and deterministic report content; no live L200 evidence exists |

## R11 - V6 Release

| Requirement | Status | Authoritative evidence |
| --- | --- | --- |
| R10 fully proven | MISSING | Completed R10 rows above |
| Architecture/status/state-machine/runbook docs match RC | MISSING | Final tracked documentation diff and review |
| Release commit/tag pushed to GitHub `master` | MISSING | GitHub remote refs; no push is allowed before R10 |
| 150 deploy sync succeeds | MISSING | Deploy command output and `.deploy-sync-*` markers |
| All production roles healthy | MISSING | API, generation worker, MCP, publisher worker/browser, Postgres, Qdrant, and MinIO smoke evidence |
| Fresh production project accepts one chapter | MISSING | Supported MCP project/task/chapter evidence |
| Publisher dry-run or safe binding smoke | MISSING | Publisher job/attempt evidence without duplicate external mutation |
| Deployed SHA/images and rollback point recorded | MISSING | Final release report and production image/service metadata |
