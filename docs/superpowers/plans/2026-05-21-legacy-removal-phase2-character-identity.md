# Legacy Removal Phase 2 Character Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove `legacy_entity_id` and legacy Entity-row creation from runtime character identity paths.

**Architecture:** Make BookState character ids the only runtime character identity. Update creation, registry, SubWorld, context, personality, schema, runtime audit, and inventory in narrow tasks. Keep non-character Entity storage and unrelated compatibility for later phases.

**Tech Stack:** Python 3.12, SQLAlchemy models/migrations, Pydantic models, BookState repository, pytest, ForWin inventory audit.

---

## Scope

This plan implements Phase 2 from
`docs/superpowers/specs/2026-05-21-legacy-removal-phase2-character-identity-design.md`.

Do not remove:

- World v4 projection compatibility.
- `state.location` fallback.
- `creation_status="legacy"` fallback.
- UI legacy labels.
- Non-character Entity table behavior that still represents current domain data.

## File Structure

- `forwin/characters/models.py`: remove legacy fields and alias.
- `forwin/characters/creation.py`: remove Entity side effects, legacy import helper, legacy compatibility event.
- `forwin/characters/identity.py`: remove `legacy_entity_id` lookup/write.
- `forwin/characters/registry.py`: remove legacy lookup fallback.
- `forwin/api_schema/world.py`: remove `legacy_entity_id` from `CharacterCreateRequest`.
- `forwin/models/book_state.py`: remove column/index from `CharacterIdentityMapRow`.
- `forwin/models/entity.py`: loosen relation endpoint fields so canonical character ids can be stored.
- `forwin/models/base.py`: remove current bootstrap SQL for character identity legacy columns.
- `forwin/migrations/versions/0011_no_legacy_char_identity.py`: drop/add column and index.
- `forwin/subworld_manager.py`: use `character_id`/`book_state_node_id`, not legacy Entity ids.
- `forwin/state/updater.py`: materialize roster characters without requiring Entity rows.
- `forwin/context/assembler_core/book_state_overlay.py`: stop exposing `legacy_entity_id`.
- `forwin/context/assembler_core/personality_integrity.py`: stop accepting legacy ids.
- `forwin/personality/enrichment.py`: resolve by canonical character ids.
- `forwin/orchestrator_loop_core/finalization.py`: remove fallback from result legacy id.
- `forwin/review_engine/audit.py`: remove Phase 2 identity runtime registry entries.
- `forwin/governance.py` and `forwin/characters/events.py`: remove `CHARACTER_IMPORTED_FROM_LEGACY` if no callers remain.
- `docs/designs/legacy-inventory.yaml`: mark character identity deleted and retain the remaining schema-history marker.
- Tests:
  - `tests/test_character_creation_helper.py`
  - `tests/test_character_personality_integration.py`
  - `tests/test_character_relationship_enrichment.py`
  - `tests/test_subworld_control.py`
  - `tests/review_engine/test_legacy_compatibility_audit.py`

## Task 1: Update Character Creation Tests

**Files:**
- Modify: `tests/test_character_creation_helper.py`
- Modify: `tests/test_character_personality_integration.py`

- [ ] **Step 1: Replace expectations for Entity side effects**

In tests that call `CharacterCreationHelper.create_character`, update assertions
from:

```python
assert result.legacy_entity_id
assert node.metadata["legacy_entity_id"] == result.legacy_entity_id
```

to:

```python
assert result.character_id
assert "legacy_entity_id" not in node.metadata
assert node.metadata["character_identity"]["canonical_character_id"] == result.character_id
assert node.metadata["character_identity"]["book_state_node_id"] == result.character_id
```

Remove expectations that `characters.create_legacy_entity_default_true` audit
events are emitted.

- [ ] **Step 2: Update registry tests**

Replace `legacy_entity_id` registry resolution tests with canonical id and roster
id resolution tests:

```python
by_character_id = registry.resolve(project_id=project.id, character_id=result.character_id, name="别名")
assert by_character_id.node is not None
assert by_character_id.resolution in {"identity_book_state_node_id", "explicit_character_id"}
```

- [ ] **Step 3: Run focused tests and observe failures**

Run:

```bash
python3 -m pytest tests/test_character_creation_helper.py tests/test_character_personality_integration.py -q
```

Expected before implementation: failures from still-present legacy fields/events.

## Task 2: Remove Character Creation Legacy Bridge

**Files:**
- Modify: `forwin/characters/models.py`
- Modify: `forwin/characters/creation.py`
- Modify: `forwin/characters/events.py`
- Modify: `forwin/governance.py`
- Modify: `forwin/api_schema/world.py`
- Modify: `forwin/orchestrator_loop_core/finalization.py`

- [ ] **Step 1: Remove model fields**

In `forwin/characters/models.py`, remove:

```python
legacy_entity_id: str = ""
create_legacy_entity: bool = True
legacy_entity_id: str = ""
LegacyCharacterImportRequest = CharacterCreationRequest
```

from request/result/aliases.

In `forwin/api_schema/world.py`, remove:

```python
legacy_entity_id: str = ""
```

from `CharacterCreateRequest`.

- [ ] **Step 2: Remove Entity creation and audit event**

In `forwin/characters/creation.py`, remove:

```python
CHARACTER_IMPORTED_FROM_LEGACY
build_legacy_compatibility_payload
StateUpdater.create_entity(...)
legacy_compat_event_id
_save_legacy_entity_compatibility_event
import_legacy_character(...)
legacy_entity_id metadata
legacy Entity state creation
```

Keep `CharacterCreationHelper.create_character` writing BookState `WorldNode`,
identity map, and world node state.

- [ ] **Step 3: Remove event constants and helper aliases**

Remove `CHARACTER_IMPORTED_FROM_LEGACY` from:

```text
forwin/characters/events.py
forwin/governance.py
KNOWN_DECISION_EVENT_TYPES
```

Remove `LegacyCharacterImportRequest` and
`CharacterCreationHelper.import_legacy_character`. Then run:

```bash
git grep -n -E 'CHARACTER_IMPORTED_FROM_LEGACY|LegacyCharacterImportRequest|import_legacy_character' -- forwin tests ':!forwin/migrations/versions'
```

Expected: no hits.

- [ ] **Step 4: Update finalization fallback**

In `forwin/orchestrator_loop_core/finalization.py`, replace:

```python
entity_map[result.character_name] = result.legacy_entity_id or result.character_id
```

with:

```python
entity_map[result.character_name] = result.character_id
```

## Task 3: Remove Identity Map Legacy Column

**Files:**
- Modify: `forwin/characters/identity.py`
- Modify: `forwin/characters/registry.py`
- Modify: `forwin/models/book_state.py`
- Modify: `forwin/models/entity.py`
- Modify: `forwin/models/base.py`
- Create: `forwin/migrations/versions/0011_no_legacy_char_identity.py`

- [ ] **Step 1: Remove runtime lookup/write**

In `forwin/characters/identity.py`, remove `legacy_entity_id` arguments, lookup
clauses, assignments, and `_find_existing` matching by legacy id.

In `forwin/characters/registry.py`, remove the `legacy_entity_id` argument,
identity-map pass-through, and metadata fallback.

- [ ] **Step 2: Remove schema column/index**

In `forwin/models/book_state.py`, remove:

```python
Index("ix_character_identity_project_legacy", "project_id", "legacy_entity_id")
legacy_entity_id: Mapped[str] = mapped_column(...)
```

In `forwin/models/base.py`, remove bootstrap SQL for `legacy_entity_id` and
`ix_character_identity_project_legacy` from current schema creation. Do not edit
historical migrations.

In `forwin/models/entity.py`, change the `RelationEdge` endpoint columns from
Entity foreign keys to current endpoint ids:

```python
source_entity_id: Mapped[str] = mapped_column(String, nullable=False)
target_entity_id: Mapped[str] = mapped_column(String, nullable=False)
```

- [ ] **Step 3: Add migration**

Create `forwin/migrations/versions/0011_no_legacy_char_identity.py`
with upgrade/downgrade:

```python
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0011_no_legacy_char_identity"
down_revision = "0010_future_plan_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS ix_character_identity_project_legacy")
    op.execute("ALTER TABLE character_identity_map DROP COLUMN IF EXISTS legacy_entity_id")
    op.execute("ALTER TABLE relation_edges DROP CONSTRAINT IF EXISTS relation_edges_source_entity_id_fkey")
    op.execute("ALTER TABLE relation_edges DROP CONSTRAINT IF EXISTS relation_edges_target_entity_id_fkey")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.add_column(
        "character_identity_map",
        sa.Column("legacy_entity_id", sa.String(), nullable=False, server_default=""),
    )
    op.create_index(
        "ix_character_identity_project_legacy",
        "character_identity_map",
        ["project_id", "legacy_entity_id"],
    )
    op.execute(
        "ALTER TABLE relation_edges "
        "ADD CONSTRAINT relation_edges_source_entity_id_fkey "
        "FOREIGN KEY (source_entity_id) REFERENCES entities(id) NOT VALID"
    )
    op.execute(
        "ALTER TABLE relation_edges "
        "ADD CONSTRAINT relation_edges_target_entity_id_fkey "
        "FOREIGN KEY (target_entity_id) REFERENCES entities(id) NOT VALID"
    )
```

## Task 4: Update SubWorld And Personality Runtime

**Files:**
- Modify: `forwin/subworld_manager.py`
- Modify: `forwin/state/updater.py`
- Modify: `forwin/personality/enrichment.py`
- Modify context assembler files

- [ ] **Step 1: Remove SubWorld bridge audit and Entity requirement**

In `forwin/subworld_manager.py`, remove `_record_subworld_legacy_compatibility`
and all calls for:

```python
subworld.legacy_entity_id_bridge
subworld.create_legacy_entity
```

Roster items should use metadata keys:

```python
"character_id": node.id
"book_state_node_id": node.id
"canon_source": "book_state"
```

without `legacy_entity_id`.

- [ ] **Step 2: Update roster materialization**

In `forwin/state/updater.py`, call `materialize_roster_character` without
`legacy_entity_id` or `create_legacy_entity`. Replace the postcondition:

```python
entity = self.session.get(Entity, result.legacy_entity_id)
if entity is None: raise ...
```

with metadata update using `result.character_id`; do not require an Entity.

- [ ] **Step 3: Update context/personality code**

Remove `legacy_entity_id` from:

```text
forwin/context/assembler_core/book_state_overlay.py
forwin/context/assembler_core/personality_integrity.py
forwin/personality/enrichment.py
```

Relation enrichment should resolve canonical character nodes by node id or
identity map fields that remain. If no canonical node exists for a relation row,
skip enrichment for that relation.

## Task 5: Registry, Inventory, And Verification

**Files:**
- Modify: `forwin/review_engine/audit.py`
- Modify: `docs/designs/legacy-inventory.yaml`
- Modify: `tests/review_engine/test_legacy_compatibility_audit.py`

- [ ] **Step 1: Remove runtime audit registry entries**

In `forwin/review_engine/audit.py`, delete:

```python
"subworld.legacy_entity_id_bridge"
"subworld.create_legacy_entity"
"characters.create_legacy_entity_default_true"
```

Update tests that asserted those registry entries.

- [ ] **Step 2: Mark inventory entries**

In `docs/designs/legacy-inventory.yaml`, mark:

```text
canonical_identity.legacy_entity_id
```

as:

```yaml
category: deleted
removal_phase: complete
status: deleted
```

Keep narrow residual `allow_patterns` for removed symbols. Leave
`schema.bootstrap_legacy_columns` as `migration_history_keep` with only
`legacy_checkpoint_statuses_v1` allowed, because Phase 2 removes the current
character identity column/index bootstrap but does not rewrite the historical
checkpoint schema marker.

- [ ] **Step 3: Run verification**

Run:

```bash
python3 scripts/audit_legacy_inventory.py --check --strict-patterns
python3 -m pytest tests/test_character_creation_helper.py tests/test_character_personality_integration.py tests/test_character_relationship_enrichment.py -q
python3 -m pytest tests/test_subworld_control.py -q
python3 -m pytest tests/test_legacy_inventory.py tests/test_architecture_boundaries.py tests/review_engine/test_legacy_compatibility_audit.py -q
python3 -m compileall -q forwin
git diff --check
```

Run production residual check:

```bash
git grep -n -E 'create_legacy_entity|LegacyCharacterImportRequest|CHARACTER_IMPORTED_FROM_LEGACY|subworld\\.legacy_entity_id_bridge|subworld\\.create_legacy_entity|characters\\.create_legacy_entity_default_true' -- forwin scripts ':!forwin/migrations/versions'
git grep -n -E 'legacy_entity_id' -- forwin scripts ':!forwin/migrations/versions' ':!forwin/world_model'
```

Expected: no runtime hits except deleted inventory/audit history if the grep is
widened outside production runtime files.

- [ ] **Step 4: Commit**

Commit:

```bash
git add -A
git commit -m "refactor: remove legacy character identity bridge"
```

## Self-Review Checklist

- Character creation no longer writes or returns `legacy_entity_id`.
- SubWorld no longer records identity bridge/create compatibility events.
- Identity map schema no longer includes `legacy_entity_id`.
- Remaining Entity usage is current-domain usage, not character identity bridge.
- `schema.bootstrap_legacy_columns` is retained only for the non-character
  historical checkpoint schema marker.
- Phase 2 does not touch world projection, location fallback, creation status, or UI labels.
