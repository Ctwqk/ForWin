# Legacy Removal Phase 6 External Compatibility Deletes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete old external compatibility paths that are not validated by chapter generation.

**Architecture:** Remove each compatibility surface at its boundary, update tests to assert current-only behavior, then mark each inventory entry deleted.

**Tech Stack:** Python 3.12, pytest, JavaScript static UI assets, legacy inventory audit.

---

## Scope

Implements `docs/superpowers/specs/2026-05-21-legacy-removal-phase6-external-compatibility-deletes-design.md`.

Do not remove `legacy_compatibility_audit_runtime`, retained migration history,
or `projection.world_model_current_helpers`; those are Phase 7/8 or retained
entries.

## Task 1: Route Dependency External Compatibility

**Files:**
- Modify: `forwin/api_route_registry.py`
- Modify: `forwin/api_task_routes.py`
- Modify: `tests/test_architecture_boundaries.py`
- Modify: `tests/test_api_task_routes.py`

- [ ] **Step 1: Write failing tests**

Update tests so `ApiRouteDeps(**flat_kwargs)` and
`api_task_routes.build_handlers(get_task_status=...)` raise `TypeError`.

- [ ] **Step 2: Remove old kwargs support**

Remove `**legacy` from `ApiRouteDeps.__init__()` and remove `**legacy_deps`
from `api_task_routes.build_handlers()`.

- [ ] **Step 3: Run route tests**

```bash
python3 -m pytest tests/test_architecture_boundaries.py tests/test_api_task_routes.py -q
```

## Task 2: Config And Planning Alias Deletes

**Files:**
- Modify: `forwin/config.py`
- Modify: `forwin/runtime/container.py`
- Modify: `forwin/orchestrator_loop_core/run_control.py`
- Modify: `forwin/planning/provisional_preview_service.py`
- Modify: `forwin/orchestrator/phase24.py`
- Modify: `forwin/production/policy.py`
- Modify: `forwin/runtime_settings.py`
- Modify tests covering those modules.

- [ ] **Step 1: Write failing tests**

Assert old env aliases are ignored/rejected and current fields are required:

```python
Config.from_env({"FORWIN_DB_PATH": "tmp/old.db"})
```

must not set `database_url` from the old alias.

`PlanningServices.from_legacy_args` must not exist.

- [ ] **Step 2: Remove aliases**

Remove old env constants, old config fields, old planning constructors, and old
production quota variable names. Use `provisional_preview_enabled` for current
preview gating.

- [ ] **Step 3: Run config/planning tests**

```bash
python3 -m pytest tests/test_config_env_resolution.py tests/test_postgres_engine.py tests/test_phase05_regressions.py tests/test_scenario_rehearsal.py -q
```

## Task 3: Publisher Session Compatibility

**Files:**
- Modify: `forwin/api_publisher_ops.py`
- Modify: `forwin/publisher_runtime/browser_sessions.py`
- Modify: `forwin/publishers/manager.py`
- Modify: `tests/test_publisher_routes_security.py`

- [ ] **Step 1: Write failing tests**

Assert `PublisherManager.get_browser_session()` has no `upgrade_legacy`
argument and plaintext cookie JSON is not upgraded.

- [ ] **Step 2: Remove plaintext upgrade**

Remove `upgrade_legacy`, old metadata flags, and legacy cookie name helpers.

- [ ] **Step 3: Run publisher tests**

```bash
python3 -m pytest tests/test_publisher_routes_security.py -q
```

## Task 4: Protocol And Writer Compatibility

**Files:**
- Modify: `forwin/personality/models.py`
- Modify: `forwin/protocol/review.py`
- Modify: `forwin/reviewer/repair_scope_router.py`
- Modify: `forwin/writer/chapter_writer.py`
- Modify tests covering personality, repair instructions, and writer parsing.

- [ ] **Step 1: Write failing tests**

Assert old schema aliases and repair-scope aliases are rejected. Assert writer
does not accept old JSON preview shape.

- [ ] **Step 2: Delete adapters**

Remove `_accept_legacy_keys`, `_LEGACY_REPAIR_SCOPE_MAP`, old repair-scope
normalization, and `_legacy_json_preview_is_accepted`.

- [ ] **Step 3: Run protocol/writer tests**

```bash
python3 -m pytest tests/test_personality_runtime.py tests/test_repair_instruction.py tests/test_chapter_writer.py -q
```

## Task 5: UI Legacy Labels

**Files:**
- Modify: `forwin/ui_assets/home/app_library.js`
- Modify: `forwin/ui_assets/home/app_state.js`
- Modify: `forwin/ui_assets/home/app_task_governance.js`
- Modify: `forwin/ui_assets/home/app_task_progress.js`
- Modify: `forwin/ui_assets/home/body.html`

- [ ] **Step 1: Replace labels and defaults**

Remove `legacy_relaxed`, `setLegacyTabState`, `Legacy Preview`,
`legacy_preview`, and `legacy preview` labels.

- [ ] **Step 2: Run static audit**

```bash
python3 scripts/audit_legacy_inventory.py --check --strict-patterns
```

## Task 6: Migration Script Retirement

**Files:**
- Delete: `scripts/migrate_legacy_canon_to_form.py`
- Delete: `scripts/migrate_sqlite_to_postgres.py`
- Modify/delete tests importing those scripts.

- [ ] **Step 1: Remove old scripts**

Delete both one-shot old migration scripts.

- [ ] **Step 2: Update tests**

Remove tests that only validate deleted scripts, or repoint them to current
chapter-review form/replay behavior.

## Task 7: Inventory And Verification

- [ ] **Step 1: Mark Phase 6 entries deleted**

Set these entries to `category: deleted`, `removal_phase: complete`, and
`status: deleted`:

```text
external.legacy_migration_scripts
external.route_dependency_legacy_kwargs
external.config_legacy_aliases
external.publisher_plaintext_session_upgrade
external.model_and_protocol_aliases
ui.legacy_project_and_governance_labels
```

- [ ] **Step 2: Run final verification**

```bash
python3 scripts/audit_legacy_inventory.py --check --strict-patterns
python3 -m pytest tests/test_architecture_boundaries.py tests/test_api_task_routes.py -q
python3 -m pytest tests/test_config_env_resolution.py tests/test_postgres_engine.py tests/test_phase05_regressions.py tests/test_scenario_rehearsal.py -q
python3 -m pytest tests/test_publisher_routes_security.py -q
python3 -m compileall -q forwin
git diff --check
```

- [ ] **Step 3: Commit Phase 6 only**

Do not stage unrelated dirty LLM/minimax files.

```bash
git commit -m "refactor: remove external legacy compatibility"
```
