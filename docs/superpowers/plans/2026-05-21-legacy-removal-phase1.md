# Legacy Removal Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove Phase 1 low-risk legacy compatibility: BookState import, relaxed governance fallback, and old checkpoint status normalization.

**Architecture:** Delete old runtime/API compatibility rather than preserving deprecated shells. Update tests to assert current strict behavior and use `docs/designs/legacy-inventory.yaml` as the removal ledger. Keep Phase 2+ runtime compatibility intact until their own phases and 60 chapter signals.

**Tech Stack:** Python 3.12, FastAPI route registry, Pydantic models, SQLAlchemy models, pytest, ForWin inventory audit.

---

## Scope

This plan implements Phase 1 from `docs/superpowers/specs/2026-05-21-legacy-removal-phase1-design.md`.

Do not remove:

- `legacy_entity_id`, `create_legacy_entity`, or subworld identity bridges.
- World v4 projection compatibility.
- `state.location` fallback.
- `creation_status="legacy"` fallback.
- UI labels listed under `ui.legacy_project_and_governance_labels`.

## File Structure

- `forwin/api_book_state_routes.py`: remove the legacy import handler and imports.
- `forwin/api_route_registry.py`: unregister `/api/projects/{project_id}/book-state/legacy-import` and schema import.
- `forwin/api_schema/world.py`: remove `BookStateLegacyImportResponse`.
- `forwin/api_schema/__init__.py`: remove `BookStateLegacyImportResponse` export.
- `forwin/book_state/__init__.py`: remove `LegacyBookStateImporter` export.
- `forwin/book_state/legacy_import.py`: delete the importer file.
- `forwin/governance.py`: remove `legacy_relaxed`, `legacy_project_governance`, `treat_empty_as_legacy`, and old checkpoint status marker helper.
- `forwin/api_governance_support.py`: stop recording checkpoint-status compatibility events and serialize current statuses only.
- `forwin/orchestrator_loop_core/governance.py`: remove relaxed fallback instrumentation and strict-mode bypass.
- `forwin/api_runtime.py`, `forwin/runtime_settings.py`, and any callsites: default invalid/empty progression modes to `serial_canon_band_guard`.
- `forwin/review_engine/audit.py`: remove Phase 1 runtime registry entries.
- `docs/designs/legacy-inventory.yaml`: mark Phase 1 entries deleted while keeping residual patterns.
- Tests:
  - `tests/test_governance_decision_api.py`
  - `tests/test_generation_audit_checkpoints.py`
  - `tests/test_book_state_final.py`
  - delete or rewrite `tests/test_book_state_legacy_import.py`
  - `tests/review_engine/test_legacy_compatibility_audit.py`

## Task 1: Add Current-Behavior Tests

**Files:**
- Modify: `tests/test_governance_decision_api.py`
- Modify: `tests/test_generation_audit_checkpoints.py`
- Modify: `tests/test_book_state_final.py`
- Delete or rewrite: `tests/test_book_state_legacy_import.py`

- [ ] **Step 1: Replace old checkpoint compatibility test**

In `tests/test_governance_decision_api.py`, replace
`test_legacy_approved_band_checkpoint_serializes_without_validation_error` with
a current-behavior test:

```python
def test_unknown_band_checkpoint_status_serializes_as_error_without_compat_event(self) -> None:
    db_path = postgres_test_url("unknown_checkpoint_status")
    self._prime_api(db_path)
    project_id = new_id()
    arc_id = new_id()
    created = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    with api_module._get_session() as session:
        session.add(Project(id=project_id, title="current checkpoint", premise="premise", genre="玄幻", setting_summary=""))
        session.flush()
        session.add(ArcPlanVersion(id=arc_id, project_id=project_id, version=1, arc_synopsis="治理弧线", status="active"))
        session.flush()
        session.add(
            BandCheckpoint(
                id="checkpoint-invalid-status",
                project_id=project_id,
                arc_id=arc_id,
                band_id="band-current",
                chapter_start=1,
                chapter_end=3,
                boundary_kind="band_end",
                boundary_chapter=3,
                status="approved",
                summary="old status should not be accepted",
                reason="operator used removed status",
                created_at=created,
                updated_at=created,
            )
        )
        session.commit()

    checkpoint = api_module.get_band_checkpoint(project_id, "band-current")

    self.assertEqual(checkpoint.status, "error")
    self.assertEqual(checkpoint.reason, "operator used removed status")
    self.assertFalse(checkpoint.resolved_at)
    with api_module._get_session() as session:
        count = (
            session.query(DecisionEvent)
            .filter(
                DecisionEvent.project_id == project_id,
                DecisionEvent.event_type == DecisionEventType.LEGACY_COMPATIBILITY_USED,
                DecisionEvent.related_object_type == "band_checkpoint",
                DecisionEvent.related_object_id == "checkpoint-invalid-status",
            )
            .count()
        )
    self.assertEqual(count, 0)
```

- [ ] **Step 2: Update generation audit fixture governance**

In `tests/test_generation_audit_checkpoints.py`, update `_governance_json` so it
uses current strict governance:

```python
"progression_mode": "serial_canon_band_guard",
"auto_band_checkpoint": True,
```

Keep `generation_audit_interval_chapters` and `generation_audit_pause_enabled`
unchanged.

- [ ] **Step 3: Remove direct importer tests**

Delete `tests/test_book_state_legacy_import.py`. In `tests/test_book_state_final.py`,
remove `LegacyBookStateImporter` imports and replace
`test_v4_adapter_and_legacy_import_create_book_state_rows` with an adapter-only
test:

```python
def test_v4_adapter_create_book_state_rows_without_import() -> None:
    Session = _session()
    with Session.begin() as session:
        project_id = _project(session)
        entity = Entity(project_id=project_id, kind="character", name="陆沉", created_at_chapter=1)
        session.add(entity)
        session.flush()
        source = DeltaSource(source_type=DeltaSourceType.CHARACTER_ACTION, actor_id="char_mc")
        changes = ExtractedWorldChangeSet(
            project_id=project_id,
            chapter_number=2,
            world_deltas=[
                WorldDelta(
                    delta_id="wd1",
                    project_id=project_id,
                    world_line_id="line_main",
                    delta_kind=DeltaKind.VISIBLE,
                    summary="主角进入黑石城",
                    narrative_chapter=2,
                    source=source,
                    affected_entities=[entity.id],
                )
            ],
        )
        approved = BookStateDeltaAdapter().from_world_change_set(changes)
        result = BookStateCompiler(session).compile(approved)
        node_count = session.scalar(select(func.count()).select_from(WorldNodeRow))
        narrative_count = session.scalar(select(func.count()).select_from(NarrativeNodeRow))

    assert result.committed is True
    assert node_count == 0
    assert narrative_count >= 1
    assert NarrativeControlGraph(nodes=[]).open_gap_ids() == []
```

- [ ] **Step 4: Run tests and confirm expected failures**

Run:

```bash
python3 -m pytest tests/test_governance_decision_api.py::GovernanceDecisionApiTests::test_unknown_band_checkpoint_status_serializes_as_error_without_compat_event tests/test_generation_audit_checkpoints.py tests/test_book_state_final.py -q
```

Expected before implementation: failures for status serialization and missing
deleted importer cleanup.

## Task 2: Delete BookState Legacy Import API

**Files:**
- Modify: `forwin/api_book_state_routes.py`
- Modify: `forwin/api_route_registry.py`
- Modify: `forwin/api_schema/world.py`
- Modify: `forwin/api_schema/__init__.py`
- Modify: `forwin/book_state/__init__.py`
- Delete: `forwin/book_state/legacy_import.py`
- Modify: `forwin/governance.py`

- [ ] **Step 1: Remove handler and route**

In `forwin/api_book_state_routes.py`, remove:

```python
LegacyBookStateImporter
DecisionEventInfo
DecisionEventType
audit_payload
event_error_payload
def import_book_state_legacy(...)
"import_book_state_legacy": import_book_state_legacy
```

In `forwin/api_route_registry.py`, remove `BookStateLegacyImportResponse` from
imports and delete this route tuple:

```python
("/api/projects/{project_id}/book-state/legacy-import", ["POST"], handlers["import_book_state_legacy"], {"response_model": BookStateLegacyImportResponse})
```

- [ ] **Step 2: Remove schema and package export**

Delete `BookStateLegacyImportResponse` from `forwin/api_schema/world.py` and
from `__all__`. Remove its import and `__all__` entry in
`forwin/api_schema/__init__.py`.

In `forwin/book_state/__init__.py`, remove:

```python
from .legacy_import import LegacyBookStateImporter
"LegacyBookStateImporter",
```

Delete `forwin/book_state/legacy_import.py`.

- [ ] **Step 3: Remove region promotion event types**

In `forwin/governance.py`, delete:

```python
LEGACY_REGION_PROMOTION_STARTED
LEGACY_REGION_PROMOTION_SUCCEEDED
LEGACY_REGION_PROMOTION_FAILED
```

and remove the three entries from `KNOWN_DECISION_EVENT_TYPES`.

- [ ] **Step 4: Verify BookState import deletion**

Run:

```bash
git grep -n -E 'LegacyBookStateImporter|import_book_state_legacy|BookStateLegacyImportResponse|LEGACY_REGION_PROMOTION|legacy_region_promotion|book_state\\.legacy_import' -- forwin scripts
```

Expected: no runtime hits.

## Task 3: Remove Legacy Governance Relaxed Mode

**Files:**
- Modify: `forwin/governance.py`
- Modify: `forwin/api_governance_support.py`
- Modify: `forwin/orchestrator_loop_core/governance.py`
- Modify: `forwin/api_runtime.py`
- Modify: `forwin/runtime_settings.py`
- Modify callsites that pass `treat_empty_as_legacy`

- [ ] **Step 1: Update governance model**

In `forwin/governance.py`, change:

```python
ProgressionMode = Literal["serial_canon", "serial_canon_band_guard"]
```

Set `ProjectGovernanceSettings.progression_mode` to
`"serial_canon_band_guard"`. Delete `legacy_project_governance`.

Change `normalize_project_governance` signature to:

```python
def normalize_project_governance(
    raw: str | dict[str, Any] | None,
    *,
    fallback_operation_mode: str = "blackbox",
    fallback_review_interval: int = 0,
) -> ProjectGovernanceSettings:
```

Remove the empty-payload legacy branch. Before `ProjectGovernanceSettings.model_validate`,
normalize invalid progression modes:

```python
if str(merged.get("progression_mode") or "").strip() not in {"serial_canon", "serial_canon_band_guard"}:
    merged["progression_mode"] = "serial_canon_band_guard"
```

- [ ] **Step 2: Update callsites**

Remove `treat_empty_as_legacy` keyword arguments from:

```text
forwin/api_governance_support.py
forwin/context/assembler_core/canon_quality_context.py
forwin/orchestrator_loop_core/governance.py
```

In `forwin/api_runtime.py`, change:

```python
progression_mode=progression_mode or "serial_canon_band_guard",
```

In `forwin/runtime_settings.py`, remove `"legacy_relaxed"` from accepted modes.

- [ ] **Step 3: Remove runtime relaxed fallback instrumentation**

In `forwin/orchestrator_loop_core/governance.py`, remove the block in
`_project_governance` that records `governance.legacy_relaxed_fallback`.

In `_strict_progression_block`, change:

```python
mode = str(governance.progression_mode or "serial_canon_band_guard")
```

and remove:

```python
if mode == "legacy_relaxed":
    return "", "", ""
```

- [ ] **Step 4: Verify governance deletion**

Run:

```bash
git grep -n -E 'legacy_relaxed|legacy_project_governance|treat_empty_as_legacy|governance\\.legacy_relaxed_fallback' -- forwin scripts ':!forwin/ui_assets'
```

Expected: no backend runtime hits. UI hits under `forwin/ui_assets` may remain
until Phase 6 and must stay covered by `ui.legacy_project_and_governance_labels`.

## Task 4: Remove Legacy Checkpoint Status Shim

**Files:**
- Modify: `forwin/governance.py`
- Modify: `forwin/api_governance_support.py`
- Modify: `forwin/project_payloads/arc_snapshot.py`
- Modify shared project payload imports/callers if needed
- Modify: `forwin/review_engine/audit.py`

- [ ] **Step 1: Change status normalization**

In `forwin/governance.py`, change `normalize_checkpoint_status` to:

```python
def normalize_checkpoint_status(value: object) -> str:
    raw = str(value or "").strip()
    if raw in CHECKPOINT_STATUS_VALUES:
        return raw
    return "error"
```

Delete `checkpoint_reason_with_legacy_status`.

- [ ] **Step 2: Stop emitting checkpoint compatibility events**

In `forwin/api_governance_support.py`, remove imports:

```python
checkpoint_reason_with_legacy_status
GovernanceDecisionEventInfo
build_legacy_compatibility_payload
StateUpdater
```

Remove `_record_legacy_checkpoint_status_compatibility` and its call from
`serialize_band_checkpoint`. Set:

```python
reason=str(row.reason or ""),
```

- [ ] **Step 3: Update project payload serializers**

In `forwin/project_payloads/arc_snapshot.py` and any project payload modules that
use the removed helper, replace:

```python
reason=checkpoint_reason_with_legacy_status(row.reason, row.status),
```

with:

```python
reason=str(row.reason or ""),
```

Remove unused imports.

- [ ] **Step 4: Remove registry entries**

In `forwin/review_engine/audit.py`, delete:

```python
"governance.legacy_relaxed_fallback": {...}
"api.legacy_checkpoint_status": {...}
"migration.legacy_book_state_import": {...}
```

Keep unrelated identity, projection, creation-status, and location registry
entries.

## Task 5: Update Inventory And Run Verification

**Files:**
- Modify: `docs/designs/legacy-inventory.yaml`
- Modify tests affected by removed registry entries

- [ ] **Step 1: Mark Phase 1 inventory entries deleted**

For these entries:

```text
book_state.legacy_import
governance.legacy_relaxed
api.legacy_checkpoint_status
```

set:

```yaml
category: deleted
removal_phase: complete
status: deleted
```

Keep their paths and `allow_patterns` so production residuals fail as
`deleted_residual`.

- [ ] **Step 2: Update audit tests**

In `tests/review_engine/test_legacy_compatibility_audit.py`, remove assertions
that expect `governance.legacy_relaxed_fallback`,
`api.legacy_checkpoint_status`, or `migration.legacy_book_state_import` to exist
in `LEGACY_COMPATIBILITY_REGISTRY`. Keep tests for active remaining runtime
compatibility.

- [ ] **Step 3: Run focused verification**

Run:

```bash
python3 scripts/audit_legacy_inventory.py --check --strict-patterns
python3 -m pytest tests/test_legacy_inventory.py tests/test_architecture_boundaries.py tests/review_engine/test_legacy_compatibility_audit.py -q
python3 -m pytest tests/test_governance_decision_api.py tests/test_generation_audit_checkpoints.py tests/test_book_state_final.py -q
```

Expected: all commands exit 0.

- [ ] **Step 4: Run production grep checks**

Run:

```bash
git grep -n -E 'LegacyBookStateImporter|import_book_state_legacy|BookStateLegacyImportResponse|legacy_relaxed|legacy_project_governance|treat_empty_as_legacy|checkpoint_reason_with_legacy_status|api\\.legacy_checkpoint_status|governance\\.legacy_relaxed_fallback|migration\\.legacy_book_state_import' -- forwin scripts ':!forwin/ui_assets'
```

Expected: no runtime hits except Phase 6 UI files if the grep is widened to
`forwin/ui_assets`.

- [ ] **Step 5: Hygiene and commit**

Run:

```bash
git diff --check
git status --short
```

Commit:

```bash
git add forwin tests docs/designs/legacy-inventory.yaml
git commit -m "refactor: remove phase 1 legacy compatibility"
```

## Self-Review Checklist

- Phase 1 spec requirement coverage:
  - BookState legacy import removal: Task 2.
  - Governance relaxed removal: Task 3.
  - Checkpoint status shim removal: Task 4.
  - Registry/inventory finalization: Task 5.
  - Targeted tests: Task 1 and Task 5.
- No phase crosses into identity, world projection, location, creation status, or UI label cleanup.
- The plan intentionally leaves migration history untouched.
