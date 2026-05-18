# ForWin Python 1024-Line Module Decomposition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the clearly structured production Python modules still above 1024 lines, while preserving public imports and API/writing behavior.

**Architecture:** Use thin compatibility shells for existing public modules and move behavior into focused packages. Add guardrails first, then split by domain in commits that each pass targeted regression tests.

**Tech Stack:** Python 3, FastAPI, Pydantic, SQLAlchemy, pytest, existing ForWin service/repository modules.

---

## Source Spec

Implement: `docs/superpowers/specs/2026-05-18-forwin-python-1024-module-decomposition-design.md`

## Non-Negotiable Constraints

- Do not split test files.
- Keep old public import paths working.
- Keep behavior stable; mechanical moves first.
- Keep new production modules below 1024 lines, preferably below 900.
- Keep thin compatibility shells for old modules.
- Preserve BookState as the only canon. Obsidian, LLM KB, World Studio, Qdrant, and legacy world modules remain projections or compatibility surfaces.
- Commit after each task group once its focused tests pass.

## Target File Structure

### Countdown Ledger

- Create `forwin/canon_quality/countdown/parsing.py`: number parsing, minute parsing, latest-entry helpers.
- Create `forwin/canon_quality/countdown/mentions.py`: mention iteration and mention context helpers.
- Create `forwin/canon_quality/countdown/filters.py`: ignored duration, wall clock, local tactical, resolution, and other predicate filters.
- Create `forwin/canon_quality/countdown/retrospective.py`: stale retrospective reference analysis.
- Create `forwin/canon_quality/countdown/keys.py`: countdown key resolution, explicit labels, nearest-key logic.
- Create `forwin/canon_quality/countdown/analysis.py`: `analyze_countdowns` coordinator.
- Create `forwin/canon_quality/countdown/__init__.py`: aggregate public exports.
- Modify `forwin/canon_quality/countdown_ledger.py`: compatibility shell.

### API Schemas And Project Payloads

- Create `forwin/api_schema/` with domain schema modules and aggregate exports.
- Modify `forwin/api_schemas.py`: compatibility shell that re-exports all prior model names.
- Create `forwin/project_payloads/` with payload-builder modules and aggregate exports.
- Modify `forwin/api_project_payloads.py`: compatibility shell that re-exports all prior public builders and helpers required by tests.

### Writer

- Create `forwin/writer/prompt_core/` for prompt section helpers and public prompt builders.
- Modify `forwin/writer/prompts.py`: compatibility shell.
- Create `forwin/writer/llm/` for LLM client adapter implementation and helpers.
- Modify `forwin/writer/llm_client.py`: compatibility shell.

### Context And Retrieval

- Create `forwin/context/assembler_core/` for map context, BookState overlay, canon quality context, personality integrity, and assembler coordination.
- Modify `forwin/context/assembler.py`: compatibility shell.
- Create `forwin/retrieval/broker_core/` for broker coordination, context payload helpers, visibility, budgeting, and database-url helpers.
- Modify `forwin/retrieval/broker.py`: compatibility shell.

## Task 1: Strengthen Guardrails

**Files:**
- Modify: `tests/test_large_module_boundaries.py`

- [ ] **Step 1: Add second-batch shell import coverage**

  Extend `test_giant_module_public_imports_remain_available()` with imports for:

  ```python
  from forwin.api_project_payloads import build_project_detail, build_project_summaries
  from forwin.api_schemas import GenerateRequest, ProjectDetail, ProjectSummary
  from forwin.writer.prompts import build_single_chapter_draft_prompt
  from forwin.writer.llm_client import LLMClient
  from forwin.context.assembler import ChapterContextAssembler, assemble_context
  from forwin.retrieval.broker import RetrievalBroker
  ```

  Assert each imported object is present or callable.

- [ ] **Step 2: Add second-batch new-module roots**

  Extend the `roots` list in `test_new_decomposition_modules_stay_context_sized()` with:

  ```python
  REPO_ROOT / "forwin" / "canon_quality" / "countdown",
  REPO_ROOT / "forwin" / "api_schema",
  REPO_ROOT / "forwin" / "project_payloads",
  REPO_ROOT / "forwin" / "writer" / "prompt_core",
  REPO_ROOT / "forwin" / "writer" / "llm",
  REPO_ROOT / "forwin" / "context" / "assembler_core",
  REPO_ROOT / "forwin" / "retrieval" / "broker_core",
  ```

- [ ] **Step 3: Add selected-shell line limits**

  Add a `SECOND_BATCH_SHELL_LIMITS` mapping and a test:

  ```python
  SECOND_BATCH_SHELL_LIMITS = {
      "forwin/canon_quality/countdown_ledger.py": 300,
      "forwin/api_project_payloads.py": 250,
      "forwin/api_schemas.py": 250,
      "forwin/writer/prompts.py": 250,
      "forwin/writer/llm_client.py": 250,
      "forwin/context/assembler.py": 250,
      "forwin/retrieval/broker.py": 250,
  }
  ```

  Mark this test skipped until the last split task enables it, then unskip it in Task 7.

- [ ] **Step 4: Run guardrail test**

  Run:

  ```bash
  python3 -m pytest tests/test_large_module_boundaries.py -q
  ```

  Expected: pass with the new shell-size test skipped.

- [ ] **Step 5: Commit guardrails**

  ```bash
  git add tests/test_large_module_boundaries.py
  git commit -m "test: add second batch module decomposition guardrails"
  ```

## Task 2: Split Countdown Ledger

**Files:**
- Create: `forwin/canon_quality/countdown/parsing.py`
- Create: `forwin/canon_quality/countdown/mentions.py`
- Create: `forwin/canon_quality/countdown/filters.py`
- Create: `forwin/canon_quality/countdown/retrospective.py`
- Create: `forwin/canon_quality/countdown/keys.py`
- Create: `forwin/canon_quality/countdown/analysis.py`
- Create: `forwin/canon_quality/countdown/__init__.py`
- Modify: `forwin/canon_quality/countdown_ledger.py`
- Test: `tests/test_countdown_ledger.py`
- Test: `tests/test_large_module_boundaries.py`

- [ ] **Step 1: Move parsing helpers**

  Move these top-level functions to `parsing.py`: `parse_chinese_number`, `parse_countdown_minutes`, `_latest_entry`, `_latest_entries_by_key`, `_latest_unresolved_entries_by_key`.

- [ ] **Step 2: Move mention helpers**

  Move `_iter_countdown_mentions`, `_mention_context`, and `_overlaps` to `mentions.py`.

- [ ] **Step 3: Move retrospective helpers**

  Move `_analyze_stale_retrospective_references`, `_looks_like_stale_retrospective_clause`, `_looks_like_public_decoy_clause`, and `_looks_like_retrospective_day_reference` to `retrospective.py`.

- [ ] **Step 4: Move key-resolution helpers**

  Move `_prefer_active_memory_reset_key`, `_prefer_short_clock_continuation_key`, `_short_clock_has_explicit_key_label`, `_normalize_ambiguous_clock_minutes`, `_explicit_minute_second_countdown_context`, `_non_monotonic_repair_hint`, `_countdown_key_for_mention`, `_looks_like_forced_memory_calibration_context`, `_explicit_countdown_label_key`, `_after_label_binds_to_mention`, and `_nearest_countdown_key` to `keys.py`.

- [ ] **Step 5: Move predicate filters**

  Move remaining `_looks_like_*`, `_is_*`, `_text_has_countdown_resolution`, `_has_upper_bound_prefix`, `_is_rounding_equivalent`, `_is_coarse_threshold_reference`, `_current_clause`, and `_clause_start_index` helpers to `filters.py`, except helpers already moved in earlier steps.

- [ ] **Step 6: Move analyzer coordinator**

  Move `analyze_countdowns` to `analysis.py`. Import moved helpers from sibling modules and keep the same public return shape.

- [ ] **Step 7: Build public exports and shell**

  Export `analyze_countdowns`, `parse_chinese_number`, and `parse_countdown_minutes` from `countdown/__init__.py`. Replace `countdown_ledger.py` with a shell that imports these names and sets `__all__`.

- [ ] **Step 8: Run countdown tests**

  Run:

  ```bash
  python3 -m pytest tests/test_countdown_ledger.py tests/test_large_module_boundaries.py -q
  ```

  Expected: pass.

- [ ] **Step 9: Commit countdown split**

  ```bash
  git add forwin/canon_quality/countdown forwin/canon_quality/countdown_ledger.py tests/test_large_module_boundaries.py
  git commit -m "refactor: split countdown ledger"
  ```

## Task 3: Split API Schemas And Project Payloads

**Files:**
- Create: `forwin/api_schema/*.py`
- Modify: `forwin/api_schemas.py`
- Create: `forwin/project_payloads/*.py`
- Modify: `forwin/api_project_payloads.py`
- Test: `tests/test_api_split_modules.py`
- Test: `tests/test_project_operation_guards.py`
- Test: `tests/test_generation_control_payload.py`
- Test: `tests/test_book_genesis_flow.py`
- Test: `tests/test_mcp_server.py`
- Test: `tests/test_large_module_boundaries.py`

- [ ] **Step 1: Split schemas by class ranges**

  Move the Pydantic classes from `api_schemas.py` into these modules:

  - `api_schema/llm.py`: `GenerateRequest` through `LLMSettingsResponse`
  - `api_schema/tasks.py`: `CodexBridgeStatusResponse` through `TaskBulkDeleteRequest`
  - `api_schema/world.py`: `WorldModelV4DebugResponse` through `WorldModelImportResponse`, plus map and BookState schemas
  - `api_schema/genesis.py`: `BookGenesisStageState` through `StartWritingResponse`
  - `api_schema/project.py`: `ProjectArcSnapshotFields`, automation settings, `ProjectSummary` through generation/governance/project detail schemas
  - `api_schema/observability.py`: decision events, timelines, artifact, performance, replay, and insight schemas
  - `api_schema/governance.py`: narrative constraint and task contract schemas
  - `api_schema/review.py`: chapter detail/review, trope, provisional, scenario, candidate draft schemas
  - `api_schema/publisher.py`: publisher, extension, upload, and comment-sync schemas

- [ ] **Step 2: Build schema exports**

  Add `api_schema/__init__.py` that imports and re-exports all schema classes. Replace `api_schemas.py` with a shell that imports `*` from `forwin.api_schema` and preserves `__all__`.

- [ ] **Step 3: Split project payload helpers**

  Move payload functions from `api_project_payloads.py` into:

  - `project_payloads/common.py`: `_deep_merge_dict`, `_normalized_project_ids`, `_recent_rows_by_project`, `_latest_rows_by_project`, JSON helpers
  - `project_payloads/generation.py`: `_derive_blocking_reason`, `_derive_next_gate`, `effective_target_total_chapters`, `build_generation_control`
  - `project_payloads/arc_snapshot.py`: `_latest_band_checkpoint_by_project`, `_decision_timeline_by_project`, `_narrative_constraints_by_project`, `_band_checkpoint_detail`, `project_arc_snapshot_payload`
  - `project_payloads/runtime_maps.py`: latest arc/band loaders, project automation normalization, upload stats, runtime map loaders
  - `project_payloads/genesis.py`: Genesis pack/revision/stage/prompt trace helpers
  - `project_payloads/project_summary.py`: `build_project_summaries`
  - `project_payloads/project_detail.py`: `build_project_detail`
  - `project_payloads/provisional.py`: `latest_provisional_band_execution`, `build_provisional_band_detail`
  - `project_payloads/scenario.py`: `latest_scenario_rehearsal_run`, `build_scenario_rehearsal_detail`

- [ ] **Step 4: Build payload exports and shell**

  Add `project_payloads/__init__.py` that re-exports prior public functions. Replace `api_project_payloads.py` with a shell that re-exports those names and any private helpers that existing tests import directly.

- [ ] **Step 5: Run API payload/schema tests**

  Run:

  ```bash
  python3 -m pytest tests/test_api_split_modules.py tests/test_project_operation_guards.py tests/test_generation_control_payload.py tests/test_book_genesis_flow.py tests/test_mcp_server.py tests/test_large_module_boundaries.py -q
  ```

  Expected: pass.

- [ ] **Step 6: Commit API split**

  ```bash
  git add forwin/api_schema forwin/api_schemas.py forwin/project_payloads forwin/api_project_payloads.py tests/test_large_module_boundaries.py
  git commit -m "refactor: split api schemas and project payloads"
  ```

## Task 4: Split Writer Prompts

**Files:**
- Create: `forwin/writer/prompt_core/sections.py`
- Create: `forwin/writer/prompt_core/constraints.py`
- Create: `forwin/writer/prompt_core/builders.py`
- Create: `forwin/writer/prompt_core/extraction.py`
- Create: `forwin/writer/prompt_core/__init__.py`
- Modify: `forwin/writer/prompts.py`
- Test: `tests/test_writer_prompt_contract.py`
- Test: `tests/test_prompt_revision.py`
- Test: `tests/test_phase05_regressions.py`
- Test: `tests/test_large_module_boundaries.py`

- [ ] **Step 1: Move prompt section helpers**

  Move section helpers from `_apply_skill_layers` through `_timeline_section` to `prompt_core/sections.py`.

- [ ] **Step 2: Move constraint section helpers**

  Move `ConstraintSection`, `_canon_quality_context_section`, `_render_constraint_sections`, final/countdown/character/open-signal/future-audit/obligation helpers, suppression helpers, and display helpers to `prompt_core/constraints.py`.

- [ ] **Step 3: Move chapter prompt builders**

  Move `_chapter_hook_requirement`, `_join_sections`, `_scene_task_section`, `_scene_prompt_sections`, `build_single_chapter_draft_prompt`, `build_preview_chapter_prompt`, `build_scene_breakdown_prompt`, `build_scene_generation_prompt`, and `build_scene_stitch_prompt` to `prompt_core/builders.py`.

- [ ] **Step 4: Move extraction prompt builders**

  Move `build_state_event_extraction_prompt`, `build_thread_time_extraction_prompt`, `build_lore_timeline_notes_extraction_prompt`, and `build_structured_extraction_prompt` to `prompt_core/extraction.py`.

- [ ] **Step 5: Build exports and shell**

  Export all public prompt builders from `prompt_core/__init__.py`. Replace `writer/prompts.py` with a shell that re-exports public builders and any private helpers imported by tests.

- [ ] **Step 6: Run prompt tests**

  Run:

  ```bash
  python3 -m pytest tests/test_writer_prompt_contract.py tests/test_prompt_revision.py tests/test_phase05_regressions.py tests/test_large_module_boundaries.py -q
  ```

  Expected: pass.

- [ ] **Step 7: Commit prompt split**

  ```bash
  git add forwin/writer/prompt_core forwin/writer/prompts.py tests/test_large_module_boundaries.py
  git commit -m "refactor: split writer prompt builders"
  ```

## Task 5: Split LLM Client

**Files:**
- Create: `forwin/writer/llm/adapter.py`
- Create: `forwin/writer/llm/__init__.py`
- Modify: `forwin/writer/llm_client.py`
- Test: `tests/test_llm_client_retry.py`
- Test: `tests/test_writer_prompt_contract.py`
- Test: `tests/test_large_module_boundaries.py`

- [ ] **Step 1: Move adapter class intact**

  Move `OpenAICompatibleAdapter` and `LLMClient` from `writer/llm_client.py` to `writer/llm/adapter.py` without changing method bodies.

- [ ] **Step 2: Build exports and shell**

  Export `OpenAICompatibleAdapter` and `LLMClient` from `writer/llm/__init__.py`. Replace `writer/llm_client.py` with a shell that re-exports both names.

- [ ] **Step 3: Run LLM tests**

  Run:

  ```bash
  python3 -m pytest tests/test_llm_client_retry.py tests/test_writer_prompt_contract.py tests/test_large_module_boundaries.py -q
  ```

  Expected: pass.

- [ ] **Step 4: Commit LLM split**

  ```bash
  git add forwin/writer/llm forwin/writer/llm_client.py tests/test_large_module_boundaries.py
  git commit -m "refactor: split writer llm client"
  ```

## Task 6: Split Context Assembler And Retrieval Broker

**Files:**
- Create: `forwin/context/assembler_core/*.py`
- Modify: `forwin/context/assembler.py`
- Create: `forwin/retrieval/broker_core/*.py`
- Modify: `forwin/retrieval/broker.py`
- Test: `tests/test_context_provider_chain.py`
- Test: `tests/test_world_model.py`
- Test: `tests/test_world_v4_context_pack.py`
- Test: `tests/test_world_v4_retrieval_packs.py`
- Test: `tests/test_map_world_integration.py`
- Test: `tests/test_large_module_boundaries.py`

- [ ] **Step 1: Split context assembler helpers**

  Move helpers into:

  - `assembler_core/map_context.py`: `_build_genesis_map_overview` through `_map_edge_payload`
  - `assembler_core/book_state_overlay.py`: `_book_state_context_overlay` through `_merge_book_state_map_overlay`
  - `assembler_core/personality_integrity.py`: `_project_personality_integrity_strict`, `_personality_integrity_issues`, `_save_personality_integrity_failure`
  - `assembler_core/canon_quality_context.py`: `_build_canon_quality_context` through `_looks_like_final_chapter_label`
  - `assembler_core/assembler.py`: `ChapterContextAssembler` and `assemble_context`

- [ ] **Step 2: Build assembler exports and shell**

  Add `assembler_core/__init__.py` with public exports. Replace `context/assembler.py` with a shell that re-exports `ChapterContextAssembler`, `assemble_context`, and any private helpers imported by tests.

- [ ] **Step 3: Split retrieval broker helpers**

  Move `RetrievalBroker` to `broker_core/broker.py`. Move `_book_state_*_hidden`, `_map_*_hidden`, and `_frontmatter_hidden` to `broker_core/visibility.py`. Move `_node_context`, `_edge_context`, `_fact_context`, `_map_node_context`, `_map_edge_context`, `_active_personality_contexts`, `_truncate`, `_extract_source_digest`, and `_database_url_from_repo` to `broker_core/helpers.py`.

- [ ] **Step 4: Build broker exports and shell**

  Add `broker_core/__init__.py` with public exports. Replace `retrieval/broker.py` with a shell that re-exports `RetrievalBroker` and helper names required by tests.

- [ ] **Step 5: Run context/retrieval tests**

  Run:

  ```bash
  python3 -m pytest tests/test_context_provider_chain.py tests/test_world_model.py tests/test_world_v4_context_pack.py tests/test_world_v4_retrieval_packs.py tests/test_map_world_integration.py tests/test_large_module_boundaries.py -q
  ```

  Expected: pass.

- [ ] **Step 6: Commit context/retrieval split**

  ```bash
  git add forwin/context/assembler_core forwin/context/assembler.py forwin/retrieval/broker_core forwin/retrieval/broker.py tests/test_large_module_boundaries.py
  git commit -m "refactor: split context and retrieval modules"
  ```

## Task 7: Enable Shell Size Guard And Run Broad Verification

**Files:**
- Modify: `tests/test_large_module_boundaries.py`

- [ ] **Step 1: Unskip second-batch shell-size guard**

  Remove the skip marker from the `SECOND_BATCH_SHELL_LIMITS` test.

- [ ] **Step 2: Run final verification**

  Run:

  ```bash
  python3 -m compileall -q forwin
  python3 -m pytest tests/test_large_module_boundaries.py -q
  python3 -m pytest -q
  git diff --check
  ```

  Expected: pass. If the full test suite is blocked by an environment-only dependency, record the exact blocker and keep the focused matrix results as the acceptance evidence.

- [ ] **Step 3: Commit final guard activation**

  ```bash
  git add tests/test_large_module_boundaries.py
  git commit -m "test: enforce second batch module size limits"
  ```

## Done Criteria

- All seven selected production modules are compatibility shells below their configured line limits.
- All new production modules under the split packages are below `NEW_MODULE_MAX_LINES`.
- No test file has been split.
- Old imports continue to work.
- Focused tests pass after each group.
- Final compile, guardrail, broad pytest, and whitespace checks pass or any environment-only blocker is documented exactly.
