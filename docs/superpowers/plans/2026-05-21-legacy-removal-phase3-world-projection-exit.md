# Legacy Removal Phase 3 World Projection Exit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove runtime legacy WorldModel/world v4 projection writes so BookState is the only canon commit path.

**Architecture:** Delete projection write callsites first, then remove config/API/module surfaces that could re-enable them. Keep Knowledge Projection refresh and current `forwin/world_model` read/storage helpers that still back World Studio, Obsidian, retrieval, and Genesis bootstrap. Historical migrations remain, while the inventory is split so Phase 3 only claims deletion for runtime projection/v4 compatibility code that is actually removed.

**Tech Stack:** Python 3.12, SQLAlchemy models/migrations, FastAPI route registry, pytest, Docker Compose, ForWin MCP.

---

## Scope

This plan implements Phase 3 from
`docs/superpowers/specs/2026-05-21-legacy-removal-phase3-world-projection-exit-design.md`.

Do not remove:

- Knowledge Projection / LLM KB refresh in `forwin/orchestrator_loop_core/world_projection.py`.
- Obsidian import/export behavior unless it imports deleted world v4 modules.
- Historical migrations under `forwin/migrations/versions`.
- `forwin/world_model` read/storage helpers used by current World Studio/API,
  Obsidian, retrieval, or Genesis bootstrap paths. Keep these inventoried for a
  later rename/delete phase.

## File Structure

- `forwin/orchestrator_loop_core/finalization.py`: replace legacy WorldModel compile with BookState-only success.
- `forwin/orchestrator_loop_core/world_projection.py`: remove world v4 compatibility write branch.
- `forwin/orchestrator_loop_core/common.py`: remove legacy/world v4 compiler imports.
- `forwin/orchestrator/loop.py`: remove architecture-guard marker strings for deleted projection paths.
- `forwin/config.py`: remove projection compatibility config fields and env parsing.
- `forwin/api_route_registry.py`: stop registering world v4 debug handlers.
- Delete: `forwin/api_world_model_v4_routes.py`
- Delete: `forwin/world_v4_compat/`
- Delete: `forwin/world_model_v4/`
- `forwin/review_engine/audit.py`: remove `projection.legacy_world_model_projection` registry entry.
- `docs/designs/legacy-inventory.yaml`: mark deleted projection/v4 runtime symbols deleted, and split retained current `forwin/world_model` helpers into a later owner entry.
- Tests:
  - `tests/test_world_v4_orchestrator_gate.py`
  - `tests/test_world_v4_aliases.py`
  - `tests/test_world_v4_api.py`
  - `tests/test_world_v4_debug_api_deep.py`
  - `tests/test_config_defaults.py`
  - `tests/test_quality_profile.py`
  - `tests/test_architecture_boundaries.py`
  - `tests/review_engine/test_legacy_compatibility_audit.py`

## Task 1: Update Projection Exit Tests

**Files:**
- Modify: `tests/test_world_v4_orchestrator_gate.py`
- Modify: `tests/test_world_v4_aliases.py`
- Modify: `tests/test_world_v4_api.py`
- Modify: `tests/test_world_v4_debug_api_deep.py`
- Modify: `tests/test_config_defaults.py`
- Modify: `tests/test_quality_profile.py`
- Modify: `tests/test_architecture_boundaries.py`
- Modify: `tests/review_engine/test_legacy_compatibility_audit.py`

- [ ] **Step 1: Remove tests for deleted world v4 API and aliases**

Delete tests that import these modules:

```text
forwin.api_world_model_v4_routes
forwin.world_model_v4
forwin.world_v4_compat
```

Keep tests that verify BookState direct commit and Knowledge Projection refresh.

- [ ] **Step 2: Replace config expectations**

In `tests/test_config_defaults.py` and `tests/test_quality_profile.py`, remove
assertions for:

```python
config.world_v4_compat_write_enabled
config.enable_world_v4_debug_api
```

Add a regression that current config has no such attributes:

```python
def test_world_v4_projection_flags_are_removed() -> None:
    config = Config()

    assert not hasattr(config, "world_v4_compat_write_enabled")
    assert not hasattr(config, "enable_world_v4_debug_api")
```

- [ ] **Step 3: Replace orchestrator projection tests**

In `tests/test_world_v4_orchestrator_gate.py`, replace world v4 compile
expectations with BookState-only assertions. The concrete test may reuse the
existing fixture setup in that file, but its final assertions must include:

```python
event_types = {event.event_type for event in events}
payloads = "\n".join(event.payload_json or "" for event in events)

assert "legacy_projection_failed" not in event_types
assert "projection.legacy_world_model_projection" not in payloads
```

- [ ] **Step 4: Update audit registry tests**

Remove assertions that `projection.legacy_world_model_projection` exists in
`LEGACY_COMPATIBILITY_REGISTRY`. Use another still-active registry entry such as
`book_state.state.location_fallback` for generic summary tests.

- [ ] **Step 5: Run tests and confirm failures before implementation**

Run:

```bash
python3 -m pytest tests/test_world_v4_orchestrator_gate.py tests/test_config_defaults.py tests/test_quality_profile.py tests/test_architecture_boundaries.py tests/review_engine/test_legacy_compatibility_audit.py -q
```

Expected before implementation: failures from live imports, config fields, or
projection compatibility events.

## Task 2: Remove Runtime Projection Writes

**Files:**
- Modify: `forwin/orchestrator_loop_core/finalization.py`
- Modify: `forwin/orchestrator_loop_core/world_projection.py`
- Modify: `forwin/orchestrator_loop_core/common.py`
- Modify: `forwin/orchestrator/loop.py`

- [ ] **Step 1: Replace post-acceptance legacy compile**

In `forwin/orchestrator_loop_core/finalization.py`, replace the body of
`_compile_world_model_after_acceptance` with:

```python
def _compile_world_model_after_acceptance(
    self,
    *,
    session: Session,
    updater: StateUpdater,
    project_id: str,
    chapter_number: int,
) -> bool:
    return True
```

Keep the method name in Phase 3 so acceptance/project chapter callers do not
need a rename yet.

- [ ] **Step 2: Remove world v4 branch from BookState commit**

In `forwin/orchestrator_loop_core/world_projection.py`, delete the entire block:

```python
if self.config.world_v4_compat_write_enabled and gate_verdict is not None
```

Do not delete the later `KnowledgeProjectionRefresher` refresh call; only the
compatibility write branch goes away.

- [ ] **Step 3: Remove compiler imports**

In `forwin/orchestrator_loop_core/common.py`, remove:

```python
from forwin.world_v4_compat.compiler import WorldModelCompiler as WorldModelCompilerV4
from forwin.world_model.compiler import WorldModelCompiler as LegacyWorldModelCompiler
```

In `forwin/orchestrator/loop.py`, remove marker lines containing:

```text
WorldModelCompilerV4
LEGACY_PROJECTION_FAILED
LegacyWorldModelCompiler
legacy_projection
```

- [ ] **Step 4: Run focused runtime tests**

Run:

```bash
python3 -m pytest tests/test_world_v4_orchestrator_gate.py tests/test_architecture_boundaries.py -q
```

Expected after implementation: tests pass or fail only on deleted API/module
surfaces handled in Task 3.

## Task 3: Remove Config And API Surfaces

**Files:**
- Modify: `forwin/config.py`
- Modify: `forwin/api_route_registry.py`
- Delete: `forwin/api_world_model_v4_routes.py`

- [ ] **Step 1: Remove config fields and env parsing**

In `forwin/config.py`, remove these entries from env parsing, quality-profile
defaults, and `Config` fields:

```python
world_v4_compat_write_enabled
enable_world_v4_debug_api
FORWIN_WORLD_V4_COMPAT_WRITE
FORWIN_ENABLE_COMPAT_DEBUG_API
```

- [ ] **Step 2: Remove route registration**

In `forwin/api_route_registry.py`, remove:

```python
api_world_model_v4_routes
world_model_v4_handlers = api_world_model_v4_routes.build_handlers
**world_model_v4_handlers,
```

- [ ] **Step 3: Delete debug route module**

Delete:

```text
forwin/api_world_model_v4_routes.py
```

- [ ] **Step 4: Run config and API tests**

Run:

```bash
python3 -m pytest tests/test_config_defaults.py tests/test_quality_profile.py tests/test_api_pages_rendering.py tests/test_architecture_boundaries.py -q
```

Expected: pass after tests are aligned with removed world v4 debug routes.

## Task 4: Remove Compatibility Modules And Registry

**Files:**
- Delete: `forwin/world_v4_compat/`
- Delete: `forwin/world_model_v4/`
- Modify: `forwin/review_engine/audit.py`
- Modify: `docs/designs/legacy-inventory.yaml`
- Modify tests that imported deleted modules

- [ ] **Step 1: Delete world v4 compatibility modules**

Delete the production module trees:

```text
forwin/world_v4_compat/
forwin/world_model_v4/
```

Remove or rewrite tests that import those modules. Current runtime must use
BookState and Knowledge Projection instead.

- [ ] **Step 2: Remove audit registry entry**

In `forwin/review_engine/audit.py`, delete:

```python
"projection.legacy_world_model_projection"
```

- [ ] **Step 3: Update inventory**

In `docs/designs/legacy-inventory.yaml`, split the existing broad
`projection.world_v4_compat` entry:

1. Keep `projection.world_v4_compat` for deleted runtime/v4 compatibility
   paths only:

```yaml
paths:
  - forwin/orchestrator/loop.py
  - forwin/orchestrator_loop_core/common.py
  - forwin/orchestrator_loop_core/finalization.py
  - forwin/orchestrator_loop_core/world_projection.py
  - forwin/world_model_v4
  - forwin/world_v4_compat
```

Mark that entry as:

```yaml
category: deleted
removal_phase: complete
status: deleted
```

Keep narrow deleted-entry patterns that catch reintroduction of projection write
symbols, not broad `legacy_entity_id` strings owned by current helpers or
migration history.

2. Add a retained later-phase entry for current helpers:

```yaml
  - id: projection.world_model_current_helpers
    category: rename_only
    owner_area: world_projection
    paths:
      - forwin/world_model
    allow_patterns:
      - legacy WorldModel projection
      - legacy_entity_id
      - legacy_compatibility
      - legacy_identifier
      - legacy_nested
      - legacy path
    reason: Current World Studio, Obsidian, retrieval, and Genesis bootstrap paths still use this module; Phase 3 removes runtime projection writes but does not rename or delete these helpers.
    removal_phase: phase_7
    verification:
      - world_model_current_path_tests
      - inventory_audit
    delete_when:
      - current callers are migrated to BookState or renamed non-legacy modules
      - old table import/export behavior has an explicit external-compat owner
    status: planned
```

- [ ] **Step 4: Run residual grep**

Run:

```bash
git grep -n -E 'projection\\.legacy_world_model_projection|LegacyWorldModelCompiler|WorldModelCompilerV4|legacy_projection|LEGACY_PROJECTION_FAILED|world_v4_compat_write_enabled|enable_world_v4_debug_api' -- forwin scripts ':!forwin/migrations/versions'
```

Expected: no hits.

## Task 5: Verification And Commit

**Files:**
- All files changed by Tasks 1-4

- [ ] **Step 1: Run full phase verification**

Run:

```bash
python3 scripts/audit_legacy_inventory.py --check --strict-patterns
python3 -m pytest tests/test_architecture_boundaries.py tests/test_legacy_inventory.py tests/review_engine/test_legacy_compatibility_audit.py -q
python3 -m pytest tests/test_world_v4_orchestrator_gate.py tests/test_config_defaults.py tests/test_quality_profile.py tests/test_api_pages_rendering.py -q
python3 -m pytest tests/test_character_creation_helper.py tests/test_subworld_control.py -q
python3 -m compileall -q forwin
git diff --check
```

- [ ] **Step 2: Run residual checks**

Run:

```bash
git grep -n -E 'projection\\.legacy_world_model_projection|LegacyWorldModelCompiler|WorldModelCompilerV4|legacy_projection|LEGACY_PROJECTION_FAILED|world_v4_compat_write_enabled|enable_world_v4_debug_api' -- forwin scripts ':!forwin/migrations/versions'
git grep -n -E 'forwin\\.world_v4_compat|forwin\\.world_model_v4|api_world_model_v4_routes' -- forwin scripts ':!forwin/migrations/versions'
```

Expected: both greps return no hits.

- [ ] **Step 3: Commit**

Commit only Phase 3 files. Do not use `git add -A` because this checkout has
unrelated LLM/minimax work in progress.

```bash
git add docs/superpowers/specs/2026-05-21-legacy-removal-phase3-world-projection-exit-design.md \
  docs/superpowers/plans/2026-05-21-legacy-removal-phase3-world-projection-exit.md \
  forwin/orchestrator_loop_core/finalization.py \
  forwin/orchestrator_loop_core/world_projection.py \
  forwin/orchestrator_loop_core/common.py \
  forwin/orchestrator/loop.py \
  forwin/config.py \
  forwin/api_route_registry.py \
  forwin/api_world_model_v4_routes.py \
  forwin/world_v4_compat \
  forwin/world_model_v4 \
  forwin/review_engine/audit.py \
  docs/designs/legacy-inventory.yaml \
  tests/test_world_v4_orchestrator_gate.py \
  tests/test_config_defaults.py \
  tests/test_quality_profile.py \
  tests/test_architecture_boundaries.py \
  tests/review_engine/test_legacy_compatibility_audit.py
git commit -m "refactor: remove legacy world projection runtime"
```

Do not include unrelated dirty files from other work.

## Task 6: Container 60 Chapter Pilot

**Files:**
- No source changes

- [ ] **Step 1: Deploy from local checkout**

Run from `/home/taiwei/ForWin`:

```bash
FORWIN_ENV_FILE=.env docker compose up -d --build forwin forwin-mcp
```

- [ ] **Step 2: Start or continue a clean 60 chapter project through ForWin MCP**

Use MCP project/task/chapter tools, not raw DB inspection or ad hoc curl, for
project creation, writing handoff, and progress checks.

- [ ] **Step 3: Audit the completed pilot**

Run:

```bash
python3 scripts/audit_review_engine_cutover.py \
  --project-id <project_id> \
  --expected-chapters 60 \
  --include-legacy-compat
```

Expected:

```text
engine_live_chapters: 60
projection.legacy_world_model_projection events: 0
severe_mismatch_chapters: []
```

## Self-Review Checklist

- Finalization no longer invokes `LegacyWorldModelCompiler`.
- BookState commit no longer has a world v4 compatibility write branch.
- Config cannot enable world v4 compatibility writes or debug routes.
- Deleted world v4 modules are not imported by production runtime.
- Knowledge Projection refresh still runs.
- Inventory audit passes without hiding broad projection remnants.
