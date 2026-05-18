# ForWin Giant File Decomposition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decompose the largest ForWin modules into focused files while preserving public imports, API behavior, and writing workflow semantics.

**Architecture:** Use compatibility shells for existing public modules, move behavior into domain modules, and add size/import guardrails before each extraction batch. The first batch targets files currently above 2000 lines: `forwin/orchestrator/loop.py`, `forwin/book_genesis.py`, `forwin/api.py`, `forwin/api_project_ops.py`, and `forwin/planning/future_plan_auditor.py`.

**Tech Stack:** Python 3, FastAPI, Pydantic, SQLAlchemy, pytest, existing ForWin runtime modules.

---

## Current Hotspots

Measured in this checkout before planning:

- `forwin/orchestrator/loop.py`: 7694 lines
- `forwin/book_genesis.py`: 3545 lines
- `forwin/api.py`: 2174 lines
- `forwin/api_project_ops.py`: 2075 lines
- `forwin/planning/future_plan_auditor.py`: 2182 lines
- `forwin/canon_quality/countdown_ledger.py`: 1973 lines
- `forwin/api_project_payloads.py`: 1673 lines
- `forwin/api_schemas.py`: 1633 lines
- `forwin/writer/llm_client.py`: 1343 lines
- `forwin/writer/prompts.py`: 1364 lines

## Non-Negotiable Constraints

- Keep `BookState` as the only canon. Obsidian, LLM KB, World Studio, and Qdrant remain projections.
- Preserve existing public import paths:
  - `from forwin.orchestrator.loop import WritingOrchestrator, RunResult`
  - `from forwin.book_genesis import BookGenesisService, GENESIS_STAGE_ORDER, StaleGenesisRevisionError`
  - `import forwin.api_project_ops as api_project_ops`
  - `from forwin.planning.future_plan_auditor import FuturePlanAuditor, FuturePlanAuditRun`
  - `from forwin.canon_quality.countdown_ledger import analyze_countdowns, parse_countdown_minutes`
- Do not change ForWin project/Genesis/task/chapter workflow semantics while refactoring.
- Add line-limit tests for the old giant files and for new modules so this does not regress.
- Prefer mechanical moves plus wrappers first; semantic cleanup comes only after behavior parity tests pass.
- Commit after each task group when executing the plan.

## Target File Structure

### API

- Create `forwin/api_task_store.py`: task row serialization, cache synchronization, DB retry/prune/recovery, task load/persist/update helpers.
- Create `forwin/api_generation_control.py`: active task detection, conflict message construction, task creation, continue-task creation, task status predicates.
- Create `forwin/api_app_lifecycle.py`: startup recovery, shutdown, lifespan wiring, automation scheduler thread wrapper.
- Modify `forwin/api.py`: keep app construction, dependency assembly, middleware, and compatibility wrappers only.

### Project Operations

- Create `forwin/project_ops/lifecycle.py`: list/create/delete/bulk-delete/get project operations.
- Create `forwin/project_ops/genesis.py`: Genesis get/patch/generate/refine/lock/rerun/name/start-writing operations.
- Create `forwin/project_ops/generation.py`: continue/extend generation operations and extension helper builders.
- Create `forwin/project_ops/chapters.py`: chapter list/page/detail/upload operations.
- Create `forwin/project_ops/reviews.py`: chapter review, candidate draft, approve, retry operations.
- Modify `forwin/api_project_ops.py`: re-export public callables from the new modules and keep any tiny compatibility helpers required by tests.

### Genesis

- Create `forwin/genesis_pipeline/constants.py`: `GENESIS_STAGE_ORDER`, prompt labels, world key constants, regex/path constants.
- Create `forwin/genesis_pipeline/json_helpers.py`: JSON load/dump/clone, deep merge/equality, path token get/set helpers.
- Create `forwin/genesis_pipeline/fallbacks.py`: fallback brief/world/map/story-engine/blueprint/bootstrap/name seed builders.
- Create `forwin/genesis_pipeline/prompts.py`: stage generation/refine message builders and support-context rendering.
- Create `forwin/genesis_pipeline/materialization.py`: arc and chapter materialization plus map expansion.
- Create `forwin/genesis_pipeline/normalizers.py`: world, map, blueprint, story-engine, and scope-profile normalizers.
- Create `forwin/genesis_pipeline/tracing.py`: LLM trace payload preparation, event recording, performance span recording.
- Create `forwin/genesis_pipeline/service.py`: `BookGenesisService` composed from helpers.
- Modify `forwin/book_genesis.py`: compatibility shell that re-exports existing public names and legacy-tested fallback helpers.

### Future Plan Audit

- Create `forwin/planning/future_plan_audit/models.py`: `FuturePlanAuditIssue`, `FuturePlanAuditRun`.
- Create `forwin/planning/future_plan_audit/repository.py`: `FuturePlanAuditRepository`.
- Create `forwin/planning/future_plan_audit/countdown.py`: countdown constraints, detection, rewrite, and patch creation.
- Create `forwin/planning/future_plan_audit/custody.py`: character custody/state detection, rewrite, and patch creation.
- Create `forwin/planning/future_plan_audit/obligations.py`: pre-write obligation and band obligation binding checks.
- Create `forwin/planning/future_plan_audit/patches.py`: plan patch application for countdown, custody, obligation, and prompt-json patches.
- Create `forwin/planning/future_plan_audit/prompt_json.py`: prompt payload, issue mapping, and prompt-json plan patch conversion.
- Create `forwin/planning/future_plan_audit/auditor.py`: `FuturePlanAuditor` coordinator.
- Modify `forwin/planning/future_plan_auditor.py`: compatibility shell.

### Orchestrator

- Create `forwin/orchestrator/results.py`: `RunResult`, `ProvisionalGateSnapshot`, `TransientLLMChapterFailure`.
- Create `forwin/orchestrator/helpers.py`: top-level JSON, prompt, issue-priority, checkpoint payload, and positive-int helpers.
- Create `forwin/orchestrator/runtime_hooks.py`: runtime hook binding, progress, pause/cancel helpers.
- Create `forwin/orchestrator/governance_flow.py`: governance runtime, stage spans, audit checkpoints, strict progression, band checkpoints.
- Create `forwin/orchestrator/future_audit_flow.py`: current/future plan audit orchestration and event recording.
- Create `forwin/orchestrator/review_repair_flow.py`: draft persistence, review, repair verification, rewrite, repair patching.
- Create `forwin/orchestrator/chapter_run_flow.py`: project chapter loop and write-with-attention fallback.
- Create `forwin/orchestrator/canon_admission_flow.py`: canon gate, obligation gate, deferred acceptance, candidate application.
- Create `forwin/orchestrator/world_gate_flow.py`: World v4 gate, supported event/state filtering, seed entities, subworld admission, phase 3, world compile.
- Create `forwin/orchestrator/provisional_flow.py`: provisional band preview, fallback, verdict normalization, degradation checks.
- Modify `forwin/orchestrator/loop.py`: define `WritingOrchestrator` as a thin class composed from mixins, keeping constructor and public methods stable.

### Second Batch

- Create `forwin/canon_quality/countdown/` modules for parsing, mention scanning, filters, key resolution, monotonicity, and analyzer coordination; keep `countdown_ledger.py` as a shell.
- Split `forwin/api_project_payloads.py` into summary/detail/genesis/runtime/provisional payload builders; keep public builder imports.
- Split `forwin/api_schemas.py` into schema modules by domain, re-exporting from `api_schemas.py`.
- Split `forwin/writer/prompts.py` by section builders after giant-file guardrails land.
- Leave `forwin/writer/llm_client.py` until after behavior modules are small enough; the class is large but mostly one external adapter responsibility.

## Task 1: Guardrails And Inventory

**Files:**
- Create: `tests/test_large_module_boundaries.py`
- Modify: `tests/test_api_split_modules.py`

- [ ] **Step 1: Add current public import guard**

  Create `tests/test_large_module_boundaries.py` with import compatibility checks:

  ```python
  from pathlib import Path


  REPO_ROOT = Path(__file__).resolve().parents[1]


  def test_giant_module_public_imports_remain_available() -> None:
      from forwin.book_genesis import BookGenesisService, GENESIS_STAGE_ORDER, StaleGenesisRevisionError
      from forwin.orchestrator.loop import RunResult, WritingOrchestrator
      from forwin.planning.future_plan_auditor import FuturePlanAuditor, FuturePlanAuditRun
      from forwin.canon_quality.countdown_ledger import analyze_countdowns, parse_countdown_minutes
      import forwin.api_project_ops as api_project_ops

      assert BookGenesisService is not None
      assert GENESIS_STAGE_ORDER
      assert StaleGenesisRevisionError is not None
      assert RunResult is not None
      assert WritingOrchestrator is not None
      assert FuturePlanAuditor is not None
      assert FuturePlanAuditRun is not None
      assert callable(analyze_countdowns)
      assert callable(parse_countdown_minutes)
      assert callable(api_project_ops.create_project)
      assert callable(api_project_ops.continue_project_generation)
      assert callable(api_project_ops.get_chapter_review)
  ```

- [ ] **Step 2: Add final line-limit guard in skip mode**

  Add this test to `tests/test_large_module_boundaries.py`; keep it skipped until Task 10 lowers all first-batch modules:

  ```python
  import pytest


  FIRST_BATCH_LIMITS = {
      "forwin/orchestrator/loop.py": 900,
      "forwin/book_genesis.py": 250,
      "forwin/api.py": 700,
      "forwin/api_project_ops.py": 300,
      "forwin/planning/future_plan_auditor.py": 250,
  }


  @pytest.mark.skip(reason="enable after first-batch decomposition tasks complete")
  def test_first_batch_giant_files_stay_small() -> None:
      for relative_path, max_lines in FIRST_BATCH_LIMITS.items():
          path = REPO_ROOT / relative_path
          assert path.exists(), f"missing {relative_path}"
          line_count = len(path.read_text(encoding="utf-8").splitlines())
          assert line_count <= max_lines, f"{relative_path} has {line_count} lines; expected <= {max_lines}"
  ```

- [ ] **Step 3: Add new-module line guard**

  Add this active guard to the same file:

  ```python
  NEW_MODULE_MAX_LINES = 1100


  def test_new_decomposition_modules_stay_context_sized() -> None:
      roots = [
          REPO_ROOT / "forwin" / "project_ops",
          REPO_ROOT / "forwin" / "genesis_pipeline",
          REPO_ROOT / "forwin" / "planning" / "future_plan_audit",
      ]
      for root in roots:
          if not root.exists():
              continue
          for path in root.rglob("*.py"):
              line_count = len(path.read_text(encoding="utf-8").splitlines())
              assert line_count <= NEW_MODULE_MAX_LINES, f"{path.relative_to(REPO_ROOT)} has {line_count} lines"
  ```

- [ ] **Step 4: Run guard test**

  Run: `python3 -m pytest tests/test_large_module_boundaries.py -q`

  Expected: pass, because the final size test is skipped and public imports still resolve.

- [ ] **Step 5: Commit**

  ```bash
  git add tests/test_large_module_boundaries.py tests/test_api_split_modules.py
  git commit -m "test: add giant module decomposition guardrails"
  ```

## Task 2: Split API Runtime State From `api.py`

**Files:**
- Create: `forwin/api_task_store.py`
- Create: `forwin/api_generation_control.py`
- Create: `forwin/api_app_lifecycle.py`
- Modify: `forwin/api.py`
- Test: `tests/test_api_split_modules.py`
- Test: `tests/test_api_runtime_progress.py`
- Test: `tests/test_api_task_routes.py`

- [ ] **Step 1: Move task persistence helpers**

  Move these functions and globals from `forwin/api.py` to `forwin/api_task_store.py`: task row serialization, DB retry detection, DB write retry, task cache load/persist/update, prune, recovery, and list operations. Pass dependencies such as session factory, `TaskCenterService`, conflict message callback, and history augmentation as constructor or function arguments instead of importing `app`.

- [ ] **Step 2: Move active-generation control**

  Move active task detection, conflict messages, task creation, continue-task creation, task status predicates, and task serialization wrappers to `forwin/api_generation_control.py`. Keep public call signatures used by `api_route_registry.ApiRouteDeps`.

- [ ] **Step 3: Move lifecycle and automation scheduler**

  Move `_automation_scheduler_thread`, `_automation_scheduler_stop`, scheduler loop/start/stop, startup recovery, shutdown, and `lifespan` construction into `forwin/api_app_lifecycle.py`. Expose `build_lifespan(...)` so `api.py` remains the app assembly shell.

- [ ] **Step 4: Keep compatibility wrappers in `api.py`**

  In `forwin/api.py`, keep wrappers named `_create_generation_task`, `_create_continue_generation_task`, `_load_generation_task`, `_update_task`, `_list_generation_tasks`, `_active_generation_task_ids`, and `_project_has_active_generation_task` if existing route registration still refers to those names.

- [ ] **Step 5: Tighten API file line guard**

  In `tests/test_api_split_modules.py`, lower `forwin/api.py` from `2200` to `700` after the extraction passes.

- [ ] **Step 6: Run API tests**

  Run: `python3 -m pytest tests/test_api_split_modules.py tests/test_api_runtime_progress.py tests/test_api_task_routes.py -q`

  Expected: pass.

- [ ] **Step 7: Commit**

  ```bash
  git add forwin/api.py forwin/api_task_store.py forwin/api_generation_control.py forwin/api_app_lifecycle.py tests/test_api_split_modules.py
  git commit -m "refactor: split api runtime task state"
  ```

## Task 3: Split `api_project_ops.py` By Operation Domain

**Files:**
- Create: `forwin/project_ops/__init__.py`
- Create: `forwin/project_ops/lifecycle.py`
- Create: `forwin/project_ops/genesis.py`
- Create: `forwin/project_ops/generation.py`
- Create: `forwin/project_ops/chapters.py`
- Create: `forwin/project_ops/reviews.py`
- Modify: `forwin/api_project_ops.py`
- Test: `tests/test_api_split_modules.py`
- Test: `tests/test_project_operation_guards.py`
- Test: `tests/test_book_genesis_flow.py`
- Test: `tests/test_continue_project_orphan_review.py`

- [ ] **Step 1: Move lifecycle operations**

  Move `list_projects`, `create_project`, `delete_project`, `bulk_delete_projects`, `get_project`, `_ensure_initial_book_map_from_genesis`, and `_export_project_audit_bundle` into `forwin/project_ops/lifecycle.py`.

- [ ] **Step 2: Move Genesis operations**

  Move `get_project_genesis`, `patch_project_genesis`, `generate_project_genesis_stage`, `lock_project_genesis_stage`, `rerun_project_genesis_stage`, `refine_project_genesis_stage`, `generate_project_genesis_name`, and `start_project_writing` into `forwin/project_ops/genesis.py`.

- [ ] **Step 3: Move generation operations**

  Move `continue_project_generation`, `extend_project_generation`, `_extension_continuity_guard`, `_extension_arc_synopsis`, and `_extension_chapter_blueprint` into `forwin/project_ops/generation.py`.

- [ ] **Step 4: Move chapter operations**

  Move `_normalize_chapter_page`, `_chapter_infos_for_plans`, `list_chapters`, `list_chapter_page`, `get_chapter`, and `create_project_chapter_upload_job` into `forwin/project_ops/chapters.py`.

- [ ] **Step 5: Move review operations**

  Move `get_chapter_review`, `get_candidate_draft`, `approve_chapter_review`, `retry_chapter_review`, and `latest_rewrite_attempts_by_chapter` into `forwin/project_ops/reviews.py`.

- [ ] **Step 6: Re-export public names**

  Make `forwin/api_project_ops.py` import and expose the existing public names. Keep `_overlay_active_generation_task`, `_load_json_object`, and `_load_json_int_list` if tests import them directly.

- [ ] **Step 7: Add size guard**

  Add `forwin/api_project_ops.py: 300` and `forwin/project_ops/*.py: 900` to `tests/test_large_module_boundaries.py`.

- [ ] **Step 8: Run project operation tests**

  Run: `python3 -m pytest tests/test_api_split_modules.py tests/test_project_operation_guards.py tests/test_book_genesis_flow.py tests/test_continue_project_orphan_review.py -q`

  Expected: pass.

- [ ] **Step 9: Commit**

  ```bash
  git add forwin/api_project_ops.py forwin/project_ops tests/test_large_module_boundaries.py
  git commit -m "refactor: split project api operations"
  ```

## Task 4: Split `future_plan_auditor.py`

**Files:**
- Create: `forwin/planning/future_plan_audit/__init__.py`
- Create: `forwin/planning/future_plan_audit/models.py`
- Create: `forwin/planning/future_plan_audit/repository.py`
- Create: `forwin/planning/future_plan_audit/countdown.py`
- Create: `forwin/planning/future_plan_audit/custody.py`
- Create: `forwin/planning/future_plan_audit/obligations.py`
- Create: `forwin/planning/future_plan_audit/patches.py`
- Create: `forwin/planning/future_plan_audit/prompt_json.py`
- Create: `forwin/planning/future_plan_audit/auditor.py`
- Modify: `forwin/planning/future_plan_auditor.py`
- Test: `tests/test_future_plan_auditor.py`
- Test: `tests/test_future_plan_audit_persistence.py`
- Test: `tests/test_obligation_plan_binding_audit.py`

- [ ] **Step 1: Move models and repository**

  Move `FuturePlanAuditIssue`, `FuturePlanAuditRun`, and `FuturePlanAuditRepository` into `models.py` and `repository.py`. Re-export them from `future_plan_auditor.py`.

- [ ] **Step 2: Move countdown audit logic**

  Move `_audit_countdown_plan`, `_countdown_patch`, countdown constraint extraction, countdown text rewrite helpers, duration parsing, instruction stripping, and countdown prompt-item helpers into `countdown.py`.

- [ ] **Step 3: Move custody audit logic**

  Move `_audit_character_state_plan`, `_custody_state_patch`, custody text detection, custody rewrite helpers, and `_is_custody_state_patch` into `custody.py`.

- [ ] **Step 4: Move obligation logic**

  Move pre-write obligation checks, stale signal patching, plan binding checks, and band binding checks into `obligations.py`.

- [ ] **Step 5: Move patch application**

  Move `_apply_countdown_patch`, `_apply_obligation_patch`, `_apply_custody_state_patch`, and `_apply_prompt_plan_patch` into `patches.py`. `FuturePlanAuditor.apply_plan_patch()` should delegate to this module.

- [ ] **Step 6: Move prompt-json mapping**

  Move `_future_plan_prompt_payload`, `_chapter_plan_prompt_item`, `_band_prompt_item`, `_first_prompt_target_plan`, `_prompt_issue_plan_id`, `_prompt_issue_to_future_plan_issue`, and `_prompt_issue_to_plan_patch` into `prompt_json.py`.

- [ ] **Step 7: Keep coordinator thin**

  Keep `FuturePlanAuditor.audit_plans()` and `FuturePlanAuditor.audit_and_apply()` in `auditor.py`, delegating deterministic, prompt-json, obligation, and patch-application work to the new modules.

- [ ] **Step 8: Run future-plan tests**

  Run: `python3 -m pytest tests/test_future_plan_auditor.py tests/test_future_plan_audit_persistence.py tests/test_obligation_plan_binding_audit.py -q`

  Expected: pass.

- [ ] **Step 9: Commit**

  ```bash
  git add forwin/planning/future_plan_auditor.py forwin/planning/future_plan_audit tests/test_large_module_boundaries.py
  git commit -m "refactor: split future plan auditor"
  ```

## Task 5: Split Genesis Service

**Files:**
- Create: `forwin/genesis_pipeline/__init__.py`
- Create: `forwin/genesis_pipeline/constants.py`
- Create: `forwin/genesis_pipeline/json_helpers.py`
- Create: `forwin/genesis_pipeline/fallbacks.py`
- Create: `forwin/genesis_pipeline/prompts.py`
- Create: `forwin/genesis_pipeline/materialization.py`
- Create: `forwin/genesis_pipeline/normalizers.py`
- Create: `forwin/genesis_pipeline/tracing.py`
- Create: `forwin/genesis_pipeline/service.py`
- Modify: `forwin/book_genesis.py`
- Test: `tests/test_book_genesis_flow.py`
- Test: `tests/test_genesis_workspace_service.py`
- Test: `tests/test_genesis_handoff_service.py`
- Test: `tests/test_llm_router.py`

- [ ] **Step 1: Move constants and path helpers**

  Move `GENESIS_STAGE_ORDER`, stage prompt constants, stage labels, world root keys, regex path token parsing, and target-path normalization into `constants.py` and `json_helpers.py`.

- [ ] **Step 2: Move fallback builders**

  Move fallback brief, world bible, world, map, story engine, blueprint, bootstrap, name seed, culture profile, and placeholder rejection helpers into `fallbacks.py`. Keep `_fallback_brief`, `_fallback_blueprint`, `_fallback_map`, and `_fallback_named_entity_seed` re-exported from `book_genesis.py` because tests import them.

- [ ] **Step 3: Move prompt builders**

  Move stage generation/refine message construction, prompt rendering helpers, locked stage context, and refine support context into `prompts.py`.

- [ ] **Step 4: Move materialization**

  Move `materialize_book_arcs`, `materialize_arc_chapter_plans`, `_ensure_arc_map_expansion`, `promote_next_arc_if_needed`, and arc chapter planning into `materialization.py`.

- [ ] **Step 5: Move normalizers**

  Move world, world-root, scope-profile, blueprint, map, and story-engine normalization into `normalizers.py`.

- [ ] **Step 6: Move trace helpers**

  Move `_call_json_with_trace`, trace payload preparation, LLM event recording, and performance span recording into `tracing.py`.

- [ ] **Step 7: Rebuild service shell**

  Define `BookGenesisService` in `service.py`; make `book_genesis.py` a compatibility module that re-exports service, constants, exception, and legacy helper functions.

- [ ] **Step 8: Run Genesis tests**

  Run: `python3 -m pytest tests/test_book_genesis_flow.py tests/test_genesis_workspace_service.py tests/test_genesis_handoff_service.py tests/test_llm_router.py -q`

  Expected: pass.

- [ ] **Step 9: Commit**

  ```bash
  git add forwin/book_genesis.py forwin/genesis_pipeline tests/test_large_module_boundaries.py
  git commit -m "refactor: split book genesis pipeline"
  ```

## Task 6: Split Orchestrator By Phase

**Files:**
- Create: `forwin/orchestrator/results.py`
- Create: `forwin/orchestrator/helpers.py`
- Create: `forwin/orchestrator/runtime_hooks.py`
- Create: `forwin/orchestrator/governance_flow.py`
- Create: `forwin/orchestrator/future_audit_flow.py`
- Create: `forwin/orchestrator/review_repair_flow.py`
- Create: `forwin/orchestrator/chapter_run_flow.py`
- Create: `forwin/orchestrator/canon_admission_flow.py`
- Create: `forwin/orchestrator/world_gate_flow.py`
- Create: `forwin/orchestrator/provisional_flow.py`
- Modify: `forwin/orchestrator/loop.py`
- Test: orchestrator and phase tests listed below.

- [ ] **Step 1: Move public result classes and top-level helpers**

  Move `RunResult`, `ProvisionalGateSnapshot`, `TransientLLMChapterFailure`, `_configured_prompt_json_mode`, `_chapter_plan_prompt_text`, `_loads_json_list`, `_obligation_prompt_item`, `_future_plan_audit_checkpoint_payload`, and related pure helpers into `results.py` and `helpers.py`. Re-export `RunResult` from `loop.py`.

- [ ] **Step 2: Move runtime hook helpers**

  Move `_bind_orchestrator_runtime_hooks`, `_emit_progress`, `_abort_requested`, `_pause_requested`, `_paused_result`, `_cancelled_result`, and runtime service access helpers into `runtime_hooks.py`.

- [ ] **Step 3: Move governance and audit checkpoint flow**

  Move `_bind_governance_runtime`, `_clear_governance_runtime`, stage span helpers, `_record_stage_transition`, `_project_governance`, `_record_decision_event`, audit checkpoint helpers, strict progression, manual boundary checkpoint, and auto band checkpoint logic into `governance_flow.py`.

- [ ] **Step 4: Move future plan audit orchestration**

  Move `_audit_current_plan_before_write`, `_audit_future_plans_after_acceptance`, `_future_plan_audit_plans`, `_future_plan_audit_band_rows`, and `_record_future_plan_audit_events` into `future_audit_flow.py`.

- [ ] **Step 5: Move review and repair flow**

  Move draft persistence, current output review, autofixes, repair verification, rewrite loop, repair patch creation, and repair experience/band schedule patch helpers into `review_repair_flow.py`.

- [ ] **Step 6: Move chapter execution flow**

  Move `run`, `run_existing_project`, `continue_project`, `_pending_chapter_numbers_for_active_arc`, `_materialize_next_genesis_arc_if_needed`, `_run_project_chapters`, `_write_chapter_with_attention_fallback`, transient retry helpers, model identity, failure trace, and fallback payload recording into `chapter_run_flow.py`.

- [ ] **Step 7: Move canon admission and deferred acceptance**

  Move `_apply_canon_quality_gate`, `_run_obligation_prompt_json_gate`, `_prepare_deferred_acceptance_if_needed`, band scope helpers, latest draft/review lookup, and `_apply_canon_candidate` into `canon_admission_flow.py`.

- [ ] **Step 8: Move World v4 and phase 3 flow**

  Move `_apply_world_v4_gate`, resolvable event/state filtering, genesis canon seed entity creation, subworld candidate collection/admission, `_run_phase3_pass`, trace flushing, and world model compilation into `world_gate_flow.py`.

- [ ] **Step 9: Move provisional preview flow**

  Move provisional gate snapshot, scenario/provisional blocking, `_run_provisional_band_preview`, fallback construction, verdict normalization, degradation checks, writer-output loading, review-verdict loading, and state seeding into `provisional_flow.py`.

- [ ] **Step 10: Compose `WritingOrchestrator`**

  Make `WritingOrchestrator` inherit from the new mixins. Keep `__init__` and stable public methods visible through `forwin.orchestrator.loop`.

- [ ] **Step 11: Run focused orchestrator tests**

  Run:

  ```bash
  python3 -m pytest \
    tests/test_arc_execution_scoping.py \
    tests/test_continue_project_orphan_review.py \
    tests/test_full_decoupling_regression.py \
    tests/test_generation_audit_checkpoints.py \
    tests/test_governance_review_and_checkpoint.py \
    tests/test_observability_phase_f_spans.py \
    tests/test_observability_v38.py \
    tests/test_orchestrator_deferred_acceptance.py \
    tests/test_phase05_regressions.py \
    tests/test_placeholder_leakage_gate.py \
    tests/test_repair_progress.py \
    tests/test_scenario_rehearsal.py \
    tests/test_subworld_control.py \
    tests/test_world_v4_orchestrator_gate.py \
    tests/test_writer_attention_fallback.py \
    -q
  ```

  Expected: pass.

- [ ] **Step 12: Commit**

  ```bash
  git add forwin/orchestrator tests/test_large_module_boundaries.py
  git commit -m "refactor: split writing orchestrator phases"
  ```

## Task 7: Enable First-Batch Size Limits

**Files:**
- Modify: `tests/test_large_module_boundaries.py`
- Modify: `tests/test_api_split_modules.py`

- [ ] **Step 1: Enable first-batch line-limit test**

  Remove the skip marker from `test_first_batch_giant_files_stay_small()`.

- [ ] **Step 2: Add recursive new-module guards**

  Ensure `project_ops`, `genesis_pipeline`, `planning/future_plan_audit`, and new orchestrator flow modules are each covered by the `NEW_MODULE_MAX_LINES` guard.

- [ ] **Step 3: Run guard tests**

  Run: `python3 -m pytest tests/test_large_module_boundaries.py tests/test_api_split_modules.py -q`

  Expected: pass.

- [ ] **Step 4: Commit**

  ```bash
  git add tests/test_large_module_boundaries.py tests/test_api_split_modules.py
  git commit -m "test: enforce first batch module size limits"
  ```

## Task 8: Split Near-Threshold Countdown Ledger

**Files:**
- Create: `forwin/canon_quality/countdown/__init__.py`
- Create: `forwin/canon_quality/countdown/parsing.py`
- Create: `forwin/canon_quality/countdown/mentions.py`
- Create: `forwin/canon_quality/countdown/filters.py`
- Create: `forwin/canon_quality/countdown/key_resolution.py`
- Create: `forwin/canon_quality/countdown/monotonicity.py`
- Create: `forwin/canon_quality/countdown/analyzer.py`
- Modify: `forwin/canon_quality/countdown_ledger.py`
- Test: `tests/test_countdown_ledger.py`

- [ ] **Step 1: Move parsing**

  Move `parse_chinese_number` and `parse_countdown_minutes` into `parsing.py`; re-export them from `countdown_ledger.py`.

- [ ] **Step 2: Move mention scanning**

  Move `_iter_countdown_mentions`, context extraction, clause extraction, and overlap helpers into `mentions.py`.

- [ ] **Step 3: Move filters**

  Move all `_is_*_duration_reference`, negation, retrospective, policy threshold, window, ETA, and reset-context helpers into `filters.py`.

- [ ] **Step 4: Move key resolution**

  Move countdown key detection, nearest-key resolution, label binding, ambiguous clock normalization, and explicit label detection into `key_resolution.py`.

- [ ] **Step 5: Move monotonicity helpers**

  Move latest-entry helpers, rounding equivalence, upper-bound prefix, coarse-threshold checks, and non-monotonic repair hints into `monotonicity.py`.

- [ ] **Step 6: Move analyzer**

  Move `analyze_countdowns` into `analyzer.py`, importing parser, scanner, filters, key resolution, and monotonicity helpers.

- [ ] **Step 7: Run countdown tests**

  Run: `python3 -m pytest tests/test_countdown_ledger.py -q`

  Expected: pass.

- [ ] **Step 8: Commit**

  ```bash
  git add forwin/canon_quality/countdown forwin/canon_quality/countdown_ledger.py tests/test_large_module_boundaries.py
  git commit -m "refactor: split countdown ledger analyzer"
  ```

## Task 9: Second-Batch API Payload And Schema Split

**Files:**
- Create: `forwin/api_payloads/summary.py`
- Create: `forwin/api_payloads/detail.py`
- Create: `forwin/api_payloads/genesis.py`
- Create: `forwin/api_payloads/runtime.py`
- Create: `forwin/api_payloads/provisional.py`
- Modify: `forwin/api_project_payloads.py`
- Create: `forwin/schemas/tasks.py`
- Create: `forwin/schemas/project.py`
- Create: `forwin/schemas/genesis.py`
- Create: `forwin/schemas/world_model.py`
- Create: `forwin/schemas/governance.py`
- Create: `forwin/schemas/observability.py`
- Create: `forwin/schemas/publisher.py`
- Modify: `forwin/api_schemas.py`
- Test: `tests/test_generation_control_payload.py`
- Test: `tests/test_project_operation_guards.py`
- Test: `tests/test_world_model.py`
- Test: `tests/test_world_v4_api.py`
- Test: `tests/test_project_publish_bindings.py`

- [ ] **Step 1: Split payload builders**

  Move project summary builders, detail builders, Genesis pack/stage builders, runtime map/upload/scenario helpers, and provisional detail builders into `forwin/api_payloads/` modules. Keep `api_project_payloads.py` as a re-export shell.

- [ ] **Step 2: Split schemas**

  Move Pydantic models into `forwin/schemas/` modules by API domain. Keep `api_schemas.py` as a re-export shell so route modules do not need to change in the same commit.

- [ ] **Step 3: Add import compatibility tests**

  Extend `tests/test_large_module_boundaries.py` to import representative schema and payload names from both old and new paths.

- [ ] **Step 4: Run payload/schema tests**

  Run:

  ```bash
  python3 -m pytest \
    tests/test_generation_control_payload.py \
    tests/test_project_operation_guards.py \
    tests/test_world_model.py \
    tests/test_world_v4_api.py \
    tests/test_project_publish_bindings.py \
    -q
  ```

  Expected: pass.

- [ ] **Step 5: Commit**

  ```bash
  git add forwin/api_project_payloads.py forwin/api_payloads forwin/api_schemas.py forwin/schemas tests/test_large_module_boundaries.py
  git commit -m "refactor: split api payloads and schemas"
  ```

## Task 10: Writer Prompt Follow-Up

**Files:**
- Create: `forwin/writer/prompt_sections/story.py`
- Create: `forwin/writer/prompt_sections/world.py`
- Create: `forwin/writer/prompt_sections/planning.py`
- Create: `forwin/writer/prompt_sections/canon_quality.py`
- Create: `forwin/writer/prompt_sections/retrieval.py`
- Modify: `forwin/writer/prompts.py`
- Test: prompt regression and writer prompt tests.

- [ ] **Step 1: Move section builders by domain**

  Move story basics, chapter plan, experience overlay, world model, map runtime, retrieval, canon quality, and constraint section helpers into `forwin/writer/prompt_sections/`.

- [ ] **Step 2: Keep `prompts.py` as coordinator**

  Keep public prompt assembly functions in `forwin/writer/prompts.py`; import section builders from the new package.

- [ ] **Step 3: Run prompt tests**

  Run: `python3 -m pytest tests/test_writer_prompt_contract.py tests/test_prompt_json_analysis.py tests/test_future_plan_auditor.py -q`

  Expected: pass.

- [ ] **Step 4: Commit**

  ```bash
  git add forwin/writer/prompts.py forwin/writer/prompt_sections tests/test_large_module_boundaries.py
  git commit -m "refactor: split writer prompt sections"
  ```

## Task 11: Final Verification

- [ ] **Step 1: Run all focused suites**

  Run the union of task-level test commands from Tasks 1 through 10.

- [ ] **Step 2: Run full Python test suite**

  Run: `python3 -m pytest -q`

  Expected: pass. If full suite is too slow for the current session, capture the focused suite output and document the skipped full-suite reason in the final handoff.

- [ ] **Step 3: Run diff hygiene**

  Run: `git diff --check`

  Expected: no output.

- [ ] **Step 4: Review public imports**

  Run: `python3 -m pytest tests/test_large_module_boundaries.py -q`

  Expected: pass with no skipped first-batch line-limit tests.

- [ ] **Step 5: Final commit**

  ```bash
  git status --short
  git log --oneline -10
  ```

  Confirm every refactor group is committed independently and the working tree contains no accidental unrelated edits.

## Execution Notes

- Start with API and project operations because existing route split tests and dependency dataclasses make behavior regression cheaper to catch.
- Split `future_plan_auditor.py` before `orchestrator/loop.py`; the orchestrator currently imports the auditor and benefits from a smaller audit interface.
- Split `book_genesis.py` before orchestrator continuation work because the orchestrator calls Genesis arc materialization.
- Split `orchestrator/loop.py` last among first-batch modules because it has the highest blast radius and should consume already-stabilized Genesis and future-audit interfaces.
- Do not convert route modules to a new framework or change endpoint behavior in this update. This is a decomposition update, not a behavior redesign.
