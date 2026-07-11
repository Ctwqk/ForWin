# ForWin v5 Audit and Project Control Ownership Plan

**Goal:** Eliminate the overloaded `governance` namespace and place audit events, planning controls, review checks, Codex actions, database models, HTTP adapters, and UI controls under their real owners.

## Task 1: Enforce the deletion boundary

- [x] Add architecture guards for every old governance module and import.
- [x] Reject `GovernanceReviewer`, `GovernanceStage`, `GovernanceDeps`, and governance-named audit DTOs.
- [x] Confirm the guards fail before migration.

## Task 2: Split domain contracts

- [x] Move decision event types/contracts to `forwin.audit.events`.
- [x] Move plan task contracts, constraints, and checkpoints to typed `forwin.planning` modules.
- [x] Move issue grouping, constraint keywords, plan checks, and the plan reviewer to `forwin.review`.
- [x] Split ORM ownership into `models.audit` and `models.planning_control` without changing table contracts.
- [x] Delete `forwin/governance.py`, `governance_checks.py`, `governance_keywords.py`, `review/governance.py`, and `models/governance.py`.

## Task 3: Rename runtime and adapter ownership

- [x] Rename pipeline governance stage/state to audit control.
- [x] Move Codex governed actions into the Codex bridge boundary.
- [x] Rename API governance ops/routes/support/schema/dependencies to project control and audit terms.
- [x] Rename the home project-control asset and modal functions.
- [x] Update all production and test imports directly; add no compatibility modules.

## Task 4: Verify and document

- [x] Run audit/planning/review/API/pipeline focused tests.
- [x] Run compileall, Ruff, architecture tests, and full test collection.
- [x] Update D25 and current architecture documentation.
- [x] Commit the completed naming and ownership cutover.

## Verification Evidence

- Production Python/JavaScript/HTML contains no `governance` identifier or filename.
- Architecture/document ownership checks: 34 passed.
- Audit/planning/review/API/pipeline focused checks: 71 passed.
- Updated v5 API/Genesis fixture checks: 29 passed and 30 passed.
- Rendered home JavaScript and `app_task_control.js` pass `node --check`.
- Production Ruff undefined/import checks, `compileall`, and `git diff --check` pass.
- Full suite collection succeeds with 1559 tests and no collection errors.
