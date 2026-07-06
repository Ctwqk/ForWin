# ForWin Phase 0 Roadmap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the Phase 0 final gate and subworld admission repair path, test it, deploy it, and verify it with a 100-chapter real-machine run.

**Architecture:** Extend review-engine routing with a dedicated `subworld_admission_patch` outcome, add a subworld admission policy/executor pair, and keep final acceptance conservative but always structured. The orchestrator repair loop handles metadata admission patches locally before rebuilding context and rerunning review.

**Tech Stack:** Python, Pydantic protocol models, SQLAlchemy models, pytest, ForWin MCP operator tools.

---

### Task 1: Final Acceptance Always Matches

**Files:**
- Modify: `forwin/review_engine/rules/final_acceptance.py`
- Create: `tests/review_engine/test_final_acceptance.py`

- [ ] Write tests for missing verification, hard residual issue, soft residual-only force accept, and unsupported residual issue.
- [ ] Run `pytest tests/review_engine/test_final_acceptance.py -q` and verify the missing-verification engine case fails with `no_rule_matched`.
- [ ] Change the final acceptance rule `matches` predicate to always return `True`.
- [ ] Run `pytest tests/review_engine/test_final_acceptance.py -q` and verify all tests pass.

### Task 2: Subworld Repair Routing

**Files:**
- Modify: `forwin/review_engine/types.py`
- Modify: `forwin/review_engine/issue_taxonomy.py`
- Modify: `forwin/review_engine/rules/repair_v2.py`
- Modify: `tests/review_engine/test_repair_v2.py`

- [ ] Add failing repair-v2 tests for `subworld_admission_missing_canon_entity`, `subworld_admission_unauthorized_new_entity`, and `sub_world_unknown_named_entity`.
- [ ] Run `pytest tests/review_engine/test_repair_v2.py -q` and verify the new tests fail because subworld routes to `chapter_patch` or `draft`.
- [ ] Add `subworld_admission_patch` to `DecisionOutcome`.
- [ ] Map subworld admission issue kinds to `scope="subworld"` and `outcome="subworld_admission_patch"`.
- [ ] Run `pytest tests/review_engine/test_repair_v2.py -q` and verify all tests pass.

### Task 3: Admission Policy

**Files:**
- Create: `forwin/subworld/admission_policy.py`
- Create: `forwin/subworld/__init__.py`
- Create: `tests/test_subworld_admission_policy.py`

- [ ] Write tests for register, genericize, and manual decisions using real `ReviewVerdict`, `WriterOutput`, and `ChapterExperiencePlan` values.
- [ ] Run `pytest tests/test_subworld_admission_policy.py -q` and verify import/classification failures.
- [ ] Implement `SubworldAdmissionPolicy`, `SubworldAdmissionDecision`, and `SubworldAdmissionAction`.
- [ ] Run `pytest tests/test_subworld_admission_policy.py -q` and verify all tests pass.

### Task 4: Admission Patch Executor

**Files:**
- Create: `forwin/subworld/admission_patch.py`
- Modify: `forwin/orchestrator_loop_core/repair_loop.py`
- Create: `tests/test_subworld_admission_patch.py`

- [ ] Write tests proving register adds a chapter entry target and genericize returns replacement metadata.
- [ ] Run `pytest tests/test_subworld_admission_patch.py -q` and verify failures.
- [ ] Implement the patch executor and call it from `_apply_repair_patch` when `repair_scope == "subworld"`.
- [ ] Treat `subworld_admission_patch` as locally executable in the repair loop.
- [ ] Run `pytest tests/test_subworld_admission_patch.py -q` and verify all tests pass.

### Task 5: Repair Word Budget Guard

**Files:**
- Modify: `forwin/orchestrator_loop_core/repair_loop.py`
- Create: `tests/test_repair_word_budget.py`

- [ ] Write a test proving default repair instructions include `target_chapter_chars`, `min_chapter_chars`, `max_chapter_chars`, `repair_max_growth_ratio`, and `must_replace_not_append`.
- [ ] Run `pytest tests/test_repair_word_budget.py -q` and verify it fails.
- [ ] Add budget metadata to `_default_repair_instruction`.
- [ ] Run `pytest tests/test_repair_word_budget.py -q` and verify it passes.

### Task 6: Verification, Deploy, Real Run

**Files:**
- No planned source changes after tests unless verification finds regressions.

- [ ] Run the focused pytest set from Tasks 1-5.
- [ ] Run broader impacted tests: `pytest tests/review_engine/test_repair_v2.py tests/test_subworld_control.py tests/test_subworld_admission_auto_population.py tests/test_repair_verification.py -q`.
- [ ] Run `python3 scripts/check_codex_operator_ready.py`.
- [ ] Commit and push from `/Users/magi1/ForWin-source-github`.
- [ ] Deploy with `ssh 10.0.0.150 '/home/taiwei/deploy-github-sync/bin/deploy-github-sync.sh --apply --project forwin'`.
- [ ] Use ForWin MCP tools to create or select a 100-chapter test project, confirm no active generation task, start or continue generation to chapter 100, and inspect chapter/task/project results through MCP.
