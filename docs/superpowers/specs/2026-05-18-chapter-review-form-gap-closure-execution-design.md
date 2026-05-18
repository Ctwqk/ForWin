# Chapter Review Form Gap Closure Execution Design

Date: 2026-05-18

Status: approved for implementation planning

Related spec: `docs/superpowers/specs/2026-05-18-chapter-review-form-gap-closure-design.md`

## Purpose

Execute the existing Chapter Review Form gap-closure spec without batching unrelated risk together. The source spec is the functional authority. This document defines execution order, phase gates, live-project validation, deployment rules, and rollback policy.

The update is intentionally strict: implement Phase 1 through Phase 6 in order, validate each phase independently, deploy each phase independently, and run one live chapter after each deployed phase before moving on.

## Confirmed Constraints

- Follow the source spec faithfully. Do not preserve old code paths only because they already exist.
- Do not reintroduce deterministic keyword analyzers, per-analyzer `prompt_json/` modules, or compatibility shells for deleted analyzers.
- Code work may proceed while a generation task is active.
- Container deployment and live one-chapter validation must wait for a safe task boundary. If the selected task is still active at validation time, pause it safely through ForWin MCP.
- Use the current 60-chapter project as the primary validation target.
- If the 60-chapter task has ended by validation time, use the still-running 30-chapter generation task as the fallback target.
- Use ForWin MCP for project, task, and chapter truth. Do not inspect SQLite directly or use ad hoc HTTP calls when an MCP tool covers the workflow.

Current primary validation target at design time:

- Project: `d2338a0e8bfe4e00a068b03ce9e9b0bf`
- Title: `旧城遗档：白塔重置`
- Target length: 60 chapters
- Active task observed during planning: `764c7eec0038`

This snapshot is not a durable requirement. Implementation must re-read live state through MCP before every deployment and one-chapter test.

## Execution Strategy

Use the sequential strategy:

1. Implement one phase only.
2. Run that phase's local tests and affected tests.
3. Self-check the phase against the acceptance criteria in the source spec.
4. Commit the phase.
5. At live-test time, inspect the target project and active task with MCP.
6. If needed, pause the active task safely through MCP.
7. Rebuild and deploy `forwin` and `forwin-mcp` from the current master commit.
8. Run readiness checks.
9. Continue the selected project for one chapter.
10. Inspect task and chapter state through MCP.
11. Record pass/fail evidence before starting the next phase.

Do not combine multiple phases in one deployment test. If a phase fails live validation, fix that phase before continuing.

## Fixed Deployment Gate

Each phase uses the same deployment gate:

1. `git status --short --branch` shows only expected phase changes before commit and a clean tree after commit.
2. `python3 -m compileall -q forwin`
3. Phase-specific tests from the source spec.
4. Affected integration tests for touched modules.
5. Full non-browser suite: `python3 -m pytest --ignore=tests/browser -q`. If the environment blocks this command, record the blocker and compensate with the relevant narrower tests plus live one-chapter validation.
6. MCP `task_active_generation_check` for the selected validation project.
7. Safe MCP pause only if the selected task is active and deployment must proceed.
8. `docker compose up -d --build forwin forwin-mcp`
9. `python3 scripts/check_codex_operator_ready.py`
10. MCP-triggered one-chapter continuation or retry.
11. MCP `task_get`, `project_get`, and `chapter_get` evidence collection.

The phase is not complete until the live chapter confirms there is no system-level regression such as `form_schema_invalid`, `form_llm_unavailable`, unreadable artifacts, unexplained migration counts, or unsafe task interruption. A real narrative-quality block is allowed only if it is evidence-backed and not caused by the phase implementation.

## Phase Design

### Phase 1: Validator Edge-Case Hardening

Implement the validator and projection hardening from the source spec:

- punctuation-equivalent quote normalization
- canonical-name guidance in the LLM prompt
- additive `descriptive_aliases` support
- configurable `FormBlockingPolicy`
- no canon projection from binding answers without evidence
- richer rejection diagnostics containing attempted value and confidence

Local validation:

- `pytest tests/test_form_validators.py tests/test_form_llm_caller.py tests/test_canon_projector.py -q`
- affected service tests if projection or service signatures change

Live one-chapter validation:

- Deploy Phase 1.
- Run one chapter on the selected live project.
- Confirm `chapter_get` does not show quote/subject/schema system misclassification.
- If the chapter blocks, confirm rejection diagnostics name the attempted value, confidence, and evidence reason.

### Phase 2: Legacy Canon Data Migration

Implement supersede semantics, not deletion:

- mark non-form character transition and countdown rows as superseded
- add a dry-run migration script with before/after counts
- make default repository reads exclude superseded rows
- allow audit reads with `include_superseded=True`
- ensure form builder treats entities with only superseded history as `unknown`
- keep optional destructive rebuild behind explicit confirmation

Local validation:

- `pytest tests/test_legacy_canon_supersede.py -q`
- dry-run migration against the configured ForWin database before any write
- real migration only after the dry-run count output shows only non-form-sourced rows are affected

Live one-chapter validation:

- Confirm no active task or safely pause at validation time.
- Deploy Phase 2 after migration logic is committed.
- Run migration dry-run and record counts.
- Run real supersede only when counts match the dry-run output and affected rows are non-form sources.
- Run one chapter.
- Confirm prior state used by the form no longer inherits deterministic analyzer rows, while superseded rows remain audit-readable.

Rollback:

- Clear only this migration's `superseded_by` marker for affected rows.
- Do not delete legacy rows.

### Phase 3: Dry-Run Safety Net

Implement non-blocking diagnostic mode:

- `chapter_review_form_mode="dry_run"`
- full form execution without canon writes
- warning-only signals in dry-run
- persisted form artifacts
- comparison report helper
- inspection endpoint or CLI for saved artifacts
- LLM-unavailable warning in dry-run and error in primary

Local validation:

- `pytest tests/test_chapter_review_form_dry_run.py -q`
- artifact readback test

Live one-chapter validation:

- Switch the deployed config to dry-run for the validation chapter.
- Run one chapter.
- Confirm no blocking and no canon writes from form projection.
- Confirm artifact can be inspected.
- Restore primary mode after the phase validation.

### Phase 4: Pruning Priority And Budget Robustness

Implement priority-aware pruning:

- sort before truncation
- hard-protect active or reopened countdowns
- hard-protect must-resolve obligations
- hard-protect blocker, critical, and error signals
- emit `form_budget_exceeded` warning and proceed if protected items exceed budget
- log pruning counts

Local validation:

- `pytest tests/test_form_builder.py -q`
- targeted tests for protected items and warning fallback

Live one-chapter validation:

- Deploy Phase 4.
- Run one chapter.
- Confirm the form did not silently drop active countdowns, must-resolve obligations, or high-severity signals.
- If `form_budget_exceeded` appears, confirm it is warning-level and the form still proceeds.

### Phase 5: Fixture Suite Completion

Add the 15 generic fixtures required by the source spec:

- no real project mechanism vocabulary
- fake client only
- no real LLM calls
- architecture-boundary scan for banned fixture terms
- fixture notes for every case

Local validation:

- `pytest tests/test_chapter_review_form_regression_suite.py -q`
- existing architecture-boundary tests with fixture scanning
- target runtime under 10 seconds for the fixture suite

Live one-chapter validation:

- Deploy Phase 5.
- Run one chapter to confirm fixture-only additions did not alter production behavior.
- Confirm no new system-level review issue appears.

### Phase 6: Plan-Patcher Loop Closure

Close the loop from form drift to next-chapter planning:

- tag form signals as `plan_patchable`
- add countdown drift pre-audit
- extend signal and obligation pre-audits
- add suppression keys
- shrink writer prompt negative constraints when plan patches cover them
- add observability for consumed patches, suppressed constraints, and remaining constraints

Local validation:

- `pytest tests/test_plan_patcher_loop_closure.py tests/test_writer_prompt_contract.py tests/test_prompt_regression_samples.py -q`
- token-count regression for `_canon_quality_context_section`

Live one-chapter validation:

- Deploy Phase 6.
- Run one chapter.
- Inspect the next chapter's plan and prompt inputs.
- Confirm form-derived drift creates an explicit plan patch.
- Confirm the writer prompt omits the matching negative-list constraint when the plan patch is present.

## Validation Target Selection

Before each live chapter test:

1. Query the 60-chapter project with MCP.
2. If it is still in writing or review and has a safe continuation point, use it.
3. If it is complete, query recent tasks and use the still-running 30-chapter generation task.
4. If both are unavailable, stop and report that no approved live validation target exists.

Do not create a new validation project unless the user explicitly approves it later.

## Evidence Record Per Phase

For every phase, record these facts in the final implementation report:

- phase name
- commit hash deployed
- local test commands and outcomes
- validation target project id
- task id used for the one-chapter test
- chapter number tested
- task result after validation
- `chapter_get` review issue summary
- whether any issue is system-level or narrative-quality-level
- pass/fail conclusion

## Failure Policy

Stop the phase and fix before continuing when any of these occurs:

- schema invalid issue caused by the implementation
- LLM unavailable issue caused by wiring or config
- artifact expected by the phase cannot be read
- migration count is surprising or unexplained
- live task is interrupted outside MCP pause/continue/retry rules
- the selected validation project changes without MCP evidence
- a phase introduces behavior from a later phase without documenting why

If a chapter blocks on real narrative continuity, do not treat that as a phase failure automatically. Inspect the evidence with `chapter_get`. The phase passes only if the block is evidence-backed and unrelated to the phase mechanics.

## Commit And Rollback Discipline

Commit one phase at a time. Suggested commit messages:

- `feat: harden chapter review form validation`
- `feat: supersede legacy canon rows for review form`
- `feat: add chapter review form dry run artifacts`
- `feat: prioritize chapter review form pruning`
- `test: complete chapter review form regression fixtures`
- `feat: feed review form drift into planning`

Rollback is phase-scoped:

- revert only the current failing phase commit
- do not revert already validated prior phases
- for Phase 2, reverse the `superseded_by` marker instead of deleting data
- for Phase 3, restore `primary` mode after dry-run validation
- for live tasks, use only MCP pause, continue, retry, or inspect flows

## Done Criteria

The execution is done when:

- all six source-spec phases are implemented in order
- each phase has local tests, deployment, and one live chapter validation
- master worktree is clean
- deployed containers are healthy
- the selected live validation project has no system-level review-form regression
- all phase evidence is summarized with commit hashes and task/chapter ids
