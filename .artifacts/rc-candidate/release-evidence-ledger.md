# ForWin v5 Release Evidence Ledger

Updated: 2026-09-04. Current source successor: `530824e245bdf43226947d532c85289a40a01fd3`, based on pushed `master@0a06cfa`.
The original closure assessment started at `fdaeaa687a0dc4c65550a01d030e7d97d43b0c5f`; R27 runtime and artifacts are preserved.
The three autonomy fixes and their local verification are recorded in `docs/operations/v5-autonomy-fixes-2026-09-04.md`; `docs/superpowers/specs/2026-09-04-v5-autonomy-fixes-design.md` defines their scope. This does not establish live release acceptance.
Authority: `docs/superpowers/specs/2026-09-04-v5-closure-design.md` amends the reviewed final plan. Older roadmap checkboxes do not reopen completed implementation.

## Status rules

- IMPLEMENTED: code exists; this is not a live acceptance result.
- HISTORICAL-PASS: a real passing record exists at the named older SHA.
- PARTIAL: some observations are available; the stated target did not pass.
- MISSING: no sufficient passing evidence has been collected.
- SETUP-BLOCKED: the experiment could not reach its prescribed boundary; neither a product failure nor PASS.
- PASS: the exact stated scope and identity have fresh, sufficient evidence.

## Implementation and design decisions

| Scope | Status | Evidence / decision |
| --- | --- | --- |
| R0 old soak disposition | IMPLEMENTED | `docs/operations/v5-pre-roadmap-soak-snapshot.md`; pre-roadmap-soak, release_evidence=false, supported safe pause |
| Track A, ownership | IMPLEMENTED | Provisional preview removed; extraction and application read models relocated; architecture guards present |
| B0 S1/S3/S2 | IMPLEMENTED | GateLedger, cost and rule provenance services and MCP tools; freeze scope, do not add S4-S8 before release |
| A2 Scenario | IMPLEMENTED | `3ce71d0`: runtime family physically deleted; no second decision is pending |
| A2 generation audit | IMPLEMENTED | `356ab94`: report-only; R27 MCP export contains 13 checkpoints, all evaluated=false/fired=false/blocked=false; 10 are from pulp60 |
| A4 Obsidian | IMPLEMENTED | `c1a4a26`: reverse import removed; export/generic Canon proposals retained |
| Daily automation | RETAINED | Original plan's deletion conditions have not been established |

## Source verification history

| Scope | Status | Evidence |
| --- | --- | --- |
| fdaeaa6 default suite | PARTIAL | 2152 pass, 1 skip, 1 test-environment path override failure; the failed backup test passes separately with only the two contaminating overrides removed. Original full exit remains 1 |
| fdaeaa6 hidden RC harness tests | PASS | 1130 tests pass; explicitly collected outside default tests/ discovery |
| fdaeaa6 Ruff / compileall | PASS | Ruff 0.15.22, forwin/tests; compileall successful |
| b40fbd7 closure successor | PASS (local) | Default 2163 pass / 1 opt-in skip; independently invoked hidden RC 1147 pass; Ruff / compileall pass. Final dead-interface deletion at 6161e4c is verified separately |
| 6161e4c closure runtime source | PASS (local) | Default 2162 pass / 1 opt-in skip, exit 0; Ruff / compileall pass. One removed test exclusively covered a deleted unused interface. Hidden RC Python unchanged since the 1147-pass run; later closure commits only change documentation |
| 530824e autonomy fixes | PASS (local) | Default 2218 pass / 1 opt-in skip, separate RC 1156 pass; Ruff/compileall pass. Contracts, trace delivery and daily dispatch fixes have focused regressions; no live acceptance run |

Raw logs, JUnit, exact commands and isolation details are in the task artifact directory under the source workspace, `.artifacts/v5-closure-2026-09-04/baseline/`. The tracked operations assessment summarizes final closure results. New autonomy-fix evidence is separately retained under `.artifacts/v5-autonomy-fixes-2026-09-04/`; see its operations report for the two initial architecture-check failures and final rerun.

## R6 diagnostic matrix

R27 source/image revision is fdaeaa6. Read-only MCP recheck on 2026-09-04 confirms:

| Cell | Accepted / target | State |
| --- | --- | --- |
| standard / human L30 | 8 / 30 | chapter 9 needs_review |
| standard / spark L60S | 8 / 60 | chapter 9 needs_review |
| pulp / human L60P | 60 / 60 | target reached |
| standard / spark L100 | 9 / 100 | chapter 10 needs_review |

Total 85/250; active generation count=0. This is PARTIAL, not the original matrix PASS. The pulp chapter count is an observed fact, not a complete Canon/projection integrity certificate. Initial task_ids in the runner manifest do not substitute for the full current task inventory.

R27 revealed stale pre-stitch scene text being submitted alongside the final body, and protected-title drift during rewriting. These defects are handled with narrow regressions. Existing Canon-rule/content failures are not silently approved. No original sample or report is rewritten to remove the failures.

The amended pre-RC policy preserves diagnostics across documentation/report-only changes and retests the affected behavior. It does not label old observations as current-source validation. The existing strict matrix collector may still require new complete evidence; this amendment does not make its partial inputs pass.

## V1/V2/V3/V5 live evidence

| Scope | Existing evidence | Remaining proof |
| --- | --- | --- |
| V1 roles, migration cycle, stale-schema fail-fast, LAN embedding | HISTORICAL-PASS at 1a16d25: `.artifacts/v1-release-gate-r6/manifest.json` | Revalidate final runtime and supported lifecycle |
| Generation pre/post-commit crash recovery | HISTORICAL-PASS at 6c1fd50: `.artifacts/v5-recovery-live/20260729T231044Z-6c1fd50-final/01-generation-precommit` and `02-generation-postcommit` | Final-source live proof; retain Canon identity and fencing assertions |
| Spark no-direct-Canon / fail-closed | IMPLEMENTED; focused/default tests exist | Final runtime route/trace/model evidence |
| Qdrant outage | SETUP-BLOCKED in prior live experiments | A real fault reaching the boundary and recovering; do not infer PASS from runner code |
| Consumer / MinIO pre/post Canon | IMPLEMENTED runners, live PASS not found | Complete actual fault experiments and convergence checks |
| Publisher backend/browser/risk pauses | IMPLEMENTED runners, live PASS not found | Actual recovery without duplicate external effects and with explicit operator resume |

No current exact-candidate final recovery set is claimed. Keep existing runners; do not grow a new evidence framework.

## R9/R10/R11

| Requirement | Status / acceptance |
| --- | --- |
| RC identity | Annotated tag OR immutable full Git commit/tree record; supplied tags must still be annotated and match. Images, clean source, policy, routing, prompts/skills/rules and evidence hashes remain bound |
| External signed provenance | Not a release prerequisite. Local hashes assume a trusted operator; they are not external attestation |
| RC final freeze | MISSING: draft manifests are not a completed freeze |
| Fresh L200 no-hotfix | MISSING: all 200 chapters on a new project after final freeze; zero runtime/prompt/model/config/schema/rule/threshold changes; required reports and identity/convergence checks |
| GitHub master integration | User-authorized after current design, local ablation and tests; independent of formal release acceptance |
| 150 deploy | MISSING: preserve previous rollback identity and run production smoke when deployment is requested; a source push is not evidence of deployment |

Do not promote implementation, historical PASS, runner intent, pulp60 or partial matrix evidence into a release PASS. Do not repeat completed architecture work merely because older documents are stale.
