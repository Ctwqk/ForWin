# Legacy Removal Phase 4 Location And Project Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove `state.location`, `creation_status="legacy"`, and old Genesis pack-shape runtime compatibility.

**Architecture:** Delete compatibility behavior at the runtime boundary, then remove registry/inventory entries. Tests assert current-only behavior: `location_id` for locations, explicit Genesis lifecycle states for projects, and current `world` pack shape for Genesis.

**Tech Stack:** Python 3.12, SQLAlchemy, Pydantic models, pytest, inventory audit.

---

## Scope

Implements `docs/superpowers/specs/2026-05-21-legacy-removal-phase4-location-project-lifecycle-design.md`.

Do not modify publisher plaintext session upgrade, flat API dependency kwargs,
review-engine telemetry naming, or `forwin/world_model` helper names in this
phase.

## File Structure

- `forwin/book_state/runtime.py`: remove `state.location` read fallback and observer payload.
- `forwin/book_state/reviewer.py`: reject `state.location` patches by only accepting `state.location_id`.
- `forwin/map/service.py`: remove `state.location` location resolution.
- `forwin/models/project.py`: default `creation_status` to `creating`.
- `forwin/state/updater.py`: default project creation to `creating`.
- `forwin/api_schema/project.py`: default response schema lifecycle to `creating`.
- `forwin/mcp/models.py`: default project lifecycle to `creating`.
- `forwin/mcp/client.py`: parse missing lifecycle as `creating`.
- `forwin/api_core/runtime.py`: remove legacy Genesis guard branch.
- `forwin/context/assembler_core/personality_integrity.py`: strict mode defaults to current lifecycle.
- `forwin/project_ops/genesis.py`: delete legacy creation-status audit recorder.
- `forwin/project_payloads/project_detail.py`: serialize missing lifecycle as `creating`.
- `forwin/project_payloads/project_summary.py`: serialize missing lifecycle as `creating`.
- `forwin/book_genesis_core/helpers.py`: delete `_legacy_world_root_from_pack`.
- `forwin/book_genesis_core/names_paths.py`: stop promoting old top-level world sections.
- `forwin/genesis_workspace/normalizer.py`: remove legacy payload wording.
- `forwin/review_engine/audit.py`: remove registry entries for the deleted runtime features.
- `docs/designs/legacy-inventory.yaml`: mark Phase 4 entries deleted with narrow residual patterns.
- Tests:
  - `tests/test_book_state_runtime.py`
  - `tests/test_book_state_final.py`
  - `tests/test_project_creation_status.py`
  - `tests/review_engine/test_legacy_compatibility_audit.py`
  - `tests/test_architecture_boundaries.py`

## Task 1: Update Failing Tests First

- [ ] **Step 1: Add location-id-only assertions**

In `tests/test_book_state_runtime.py`, update or add a test that builds a
BookState node with only `state["location"]` and asserts `_resolve_location`
returns `""`.

In BookState reviewer tests, assert a `NodePatch(field_path="state.location")`
is not treated as movement compatibility. The accepted movement field is
`state.location_id`.

- [ ] **Step 2: Add project lifecycle assertions**

Add or update tests so new `Project()` and `StateUpdater.create_project`
produce `creation_status == "creating"` unless a current explicit value is
passed.

Update API/MCP payload tests to assert missing status serializes as `creating`,
not `legacy`.

- [ ] **Step 3: Add Genesis pack-shape assertion**

Add or update a Genesis helper test so top-level `world_bible`, `map_atlas`,
and `story_engine` are not promoted into `world`. Current payloads must pass
their world content under `world`.

- [ ] **Step 4: Run focused tests and observe failures**

Run:

```bash
python3 -m pytest tests/test_book_state_runtime.py tests/test_book_state_final.py tests/review_engine/test_legacy_compatibility_audit.py -q
```

Expected before implementation: failures for `state.location` fallback and
registry entries.

## Task 2: Remove Location Compatibility

- [ ] **Step 1: Update `forwin/book_state/runtime.py`**

Delete the `legacy_location = state.get("location")` branch from
`_resolve_location`. Keep activity and faction fallbacks.

- [ ] **Step 2: Update `forwin/book_state/reviewer.py`**

Change `_is_location_patch()` to:

```python
return str(patch.op) in {"set", "replace"} and patch.field_path == "state.location_id"
```

Remove `legacy_compatibility` payload construction from movement issues.

- [ ] **Step 3: Update `forwin/map/service.py`**

Delete the `state.get("location")` branch from `resolve_world_node_location_id`.

- [ ] **Step 4: Run location tests**

Run:

```bash
python3 -m pytest tests/test_book_state_runtime.py tests/test_book_state_final.py tests/test_map_runtime.py -q
```

Expected: pass after implementation.

## Task 3: Remove Project Lifecycle Legacy Default

- [ ] **Step 1: Change model and helper defaults**

Set defaults to `creating` in:

```text
forwin/models/project.py
forwin/state/updater.py
forwin/api_schema/project.py
forwin/mcp/models.py
```

In `StateUpdater.create_project()`, normalize empty input to `creating`.

- [ ] **Step 2: Remove legacy lifecycle audit recorder**

In `forwin/project_ops/genesis.py`, delete:

```text
_PROJECT_CREATION_STATUS_LEGACY_SUMMARY
_record_project_creation_status_legacy_compatibility
_require_genesis_project_with_audit
```

Call the normal `require_genesis_project` guard directly.

- [ ] **Step 3: Update serializers and clients**

Replace missing-status fallback with `creating` in:

```text
forwin/api_core/runtime.py
forwin/book_genesis_core/workflow.py
forwin/genesis_workspace/service.py
forwin/mcp/client.py
forwin/project_payloads/project_detail.py
forwin/project_payloads/project_summary.py
forwin/context/assembler_core/personality_integrity.py
```

- [ ] **Step 4: Run lifecycle tests**

Run:

```bash
python3 -m pytest tests/test_project_creation_status.py tests/test_project_api.py tests/test_mcp_models.py tests/test_character_personality_integration.py -q
```

If one of these named test files does not exist, run the closest existing
project/schema test that covers the same file changed in this task and record
the exact replacement command in the commit notes.

## Task 4: Remove Genesis Pack Payload Upgrade

- [ ] **Step 1: Delete old pack promotion helper**

Remove `_legacy_world_root_from_pack()` from `forwin/book_genesis_core/helpers.py`.

- [ ] **Step 2: Update initial pack merge**

In `forwin/book_genesis_core/names_paths.py`, set:

```python
upgraded_payload["world"] = _deep_merge(_empty_stage_world(), upgraded_payload.get("world") or {})
```

Do not read top-level `world_bible`, `map_atlas`, or `story_engine`.

- [ ] **Step 3: Update normalizer wording**

Change `GenesisNormalizer` docstring to current pack normalization wording.

- [ ] **Step 4: Run Genesis tests**

Run:

```bash
python3 -m pytest tests/test_book_genesis_core.py tests/test_genesis_workspace.py -q
```

Expected: pass after tests are aligned to current pack shape.

## Task 5: Remove Registry And Inventory Entries

- [ ] **Step 1: Update runtime registry**

Remove these entries from `forwin/review_engine/audit.py`:

```text
book_state.state.location_fallback
book_state.state.location_patch_warning
project.creation_status_legacy
```

- [ ] **Step 2: Update inventory**

In `docs/designs/legacy-inventory.yaml`, mark these entries as deleted:

```text
book_state.state.location_fallback
project.creation_status_legacy
genesis.legacy_pack_payload_upgrade
```

Keep only narrow residual patterns that catch reintroduction.

- [ ] **Step 3: Update audit tests**

Use a synthetic registry entry in `tests/review_engine/test_legacy_compatibility_audit.py` where tests need a candidate feature; do not depend on deleted Phase 4 features.

## Task 6: Verification And Commit

- [ ] **Step 1: Run phase verification**

Run:

```bash
python3 scripts/audit_legacy_inventory.py --check --strict-patterns
python3 -m pytest tests/test_book_state_runtime.py tests/test_book_state_final.py tests/test_architecture_boundaries.py tests/review_engine/test_legacy_compatibility_audit.py -q
python3 -m pytest tests/test_project_creation_status.py tests/test_character_personality_integration.py tests/test_design_status_docs.py -q
python3 -m compileall -q forwin
git diff --check
```

- [ ] **Step 2: Run residual grep**

Run:

```bash
git grep -n -E 'book_state\.state\.location_fallback|book_state\.state\.location_patch_warning|project\.creation_status_legacy|_record_project_creation_status_legacy_compatibility|_legacy_world_root_from_pack|legacy payload compatibility|state\.location([^_a-zA-Z0-9]|$)' -- forwin scripts ':!forwin/migrations/versions'
```

Expected: no hits. `state.location_id` is not matched because the pattern
excludes underscore suffixes.

- [ ] **Step 3: Commit only Phase 4 files**

Do not stage unrelated dirty LLM/minimax files.

```bash
git commit -m "refactor: remove legacy project lifecycle fallbacks"
```
