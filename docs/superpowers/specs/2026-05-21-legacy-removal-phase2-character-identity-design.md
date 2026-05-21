# Legacy Removal Phase 2 Character Identity Design

## Context

Phase 2 removes `legacy_entity_id` as a runtime character identity bridge. Phase
1 deliberately left identity compatibility intact because it needs a focused
code migration and a later 60 chapter runtime audit.

The source of truth is `docs/designs/legacy-inventory.yaml`, especially:

- `canonical_identity.legacy_entity_id`
- `schema.bootstrap_legacy_columns` character-identity column coverage
- runtime audit features:
  - `subworld.legacy_entity_id_bridge`
  - `subworld.create_legacy_entity`
  - `characters.create_legacy_entity_default_true`

The user has pre-authorized execution after the spec and plan are written. This
document is the phase contract; no approval pause is required.

## Selected Approach

Three approaches were considered:

1. **Delete all Entity-backed character support immediately.** This is too risky
   because relation enrichment and SubWorld roster materialization still use
   `Entity` as current runtime storage in some places.
2. **Remove only the `legacy_entity_id` bridge while keeping current Entity
   rows where they are the active domain model.** This is the chosen approach:
   BookState character identity becomes canonical, and any still-current Entity
   usage must use current identifiers or be isolated for later phases.
3. **Leave bridge fields but stop writing audit events.** This would make the 60
   chapter audit look clean while compatibility remains, so it is explicitly
   rejected.

## Scope

In scope:

- Remove `legacy_entity_id` from `CharacterCreationRequest` and
  `CharacterCreationResult`.
- Change `create_legacy_entity` default behavior to no Entity row creation.
- Remove `LegacyCharacterImportRequest` alias and import-legacy helper.
- Remove identity-map lookup/write by `legacy_entity_id`.
- Remove `legacy_entity_id` from character metadata and context payloads.
- Stop SubWorld roster resolution from bridging BookState nodes to old Entity ids.
- Stop SubWorld planned-slot materialization from requiring a legacy Entity row.
- Stop personality enrichment from resolving relations through
  `legacy_entity_id`; use canonical character ids or skip non-canonical
  relations.
- Add a DB migration to remove `character_identity_map.legacy_entity_id` and its
  index.
- Drop Entity foreign-key constraints from `relation_edges.source_entity_id` and
  `relation_edges.target_entity_id`; those columns become current relation
  endpoint ids so they can hold canonical character ids.
- Remove runtime audit registry entries for identity bridge/create once runtime
  code is gone.
- Mark `canonical_identity.legacy_entity_id` deleted. Keep
  `schema.bootstrap_legacy_columns` retained for the remaining historical
  checkpoint version marker, while removing its character identity column and
  index coverage from current bootstrap code.

Out of scope:

- Removing the entire `Entity` table or non-character Entity domain. Some
  current code still models sites/factions/relations there.
- World projection exit.
- Location fallback.
- Creation status fallback.
- UI label cleanup.

## Design

### Canonical Character Creation

Character creation always creates or reuses a BookState `WorldNode` with id
`character_id` or `char_<new_id>`. It no longer creates an `Entity` row as a
side effect. `CharacterCreationResult` returns `character_id`; callers that used
`legacy_entity_id` must store/use `character_id`.

Character metadata keeps current fields:

```json
{
  "roster_item_ids": ["..."],
  "character_identity": {
    "canonical_character_id": "...",
    "book_state_node_id": "...",
    "genesis_ref_id": "...",
    "roster_item_ids": ["..."]
  }
}
```

No `legacy_entity_id` field is written.

### Identity Map

`CharacterIdentityMapRow` keeps canonical, BookState, Genesis, roster, alias, and
display-name lookup. It drops the `legacy_entity_id` column and index. Runtime
lookups through `legacy_entity_id` are removed. Existing migration history may
keep old strings.

### SubWorld Roster

SubWorld roster items should identify characters by `metadata.character_id` and
`metadata.book_state_node_id`. `entity_id` can remain for non-character or old
table compatibility until a later storage cleanup, but runtime should not depend
on it to resolve canonical characters.

When a planned roster slot is materialized, the result character id is stored in
metadata and the roster item status is updated. The path must not create an
Entity row or raise if no Entity exists.

### Personality And Relations

Relation enrichment should resolve character nodes by canonical ids from
BookState and identity metadata. If an old relation only has non-canonical
Entity ids and no canonical mapping, the enrichment path should skip it rather
than resurrecting the bridge.

The existing `source_entity_id` and `target_entity_id` column names remain for
now, but their current contract is "relation endpoint id". They are not foreign
keys to `entities.id` after this phase.

### Runtime Audit

After code removal, the following runtime features should be removed from
`LEGACY_COMPATIBILITY_REGISTRY`:

- `subworld.legacy_entity_id_bridge`
- `subworld.create_legacy_entity`
- `characters.create_legacy_entity_default_true`

The inventory entries stay as `deleted` records with narrow patterns so
reintroduction fails.

`schema.bootstrap_legacy_columns` remains retained, not deleted, because
`legacy_checkpoint_statuses_v1` is a historical schema-version marker outside
the character identity bridge. Its current bootstrap coverage must no longer
create or index `character_identity_map.legacy_entity_id`.

## Testing

Required checks:

```bash
python3 scripts/audit_legacy_inventory.py --check --strict-patterns
python3 -m pytest tests/test_character_creation_helper.py tests/test_character_personality_integration.py tests/test_character_relationship_enrichment.py -q
python3 -m pytest tests/test_subworld_control.py -q
python3 -m pytest tests/test_legacy_inventory.py tests/test_architecture_boundaries.py tests/review_engine/test_legacy_compatibility_audit.py -q
python3 -m compileall -q forwin
git grep -n -E 'legacy_entity_id|create_legacy_entity|LegacyCharacterImportRequest|CHARACTER_IMPORTED_FROM_LEGACY|subworld\\.legacy_entity_id_bridge|subworld\\.create_legacy_entity|characters\\.create_legacy_entity_default_true' -- forwin scripts ':!forwin/migrations/versions'
```

The final grep may still show deleted-entry inventory patterns and migration
history when widened outside `forwin scripts`. Production runtime files must not
contain the removed identity bridge.

## 60 Chapter Follow-Up

After Phase 2 lands and later container deployment is prepared from
`/home/taiwei/ForWin`, the 60 chapter pilot must report zero events for:

- `subworld.legacy_entity_id_bridge`
- `subworld.create_legacy_entity`
- `characters.create_legacy_entity_default_true`

This phase prepares the code for that signal; the actual 60 chapter run remains
part of the final validation sequence.

## Rollback

Rollback is a normal git revert of the Phase 2 commit plus downgrade migration.
Because old-project compatibility is intentionally dropped, rollback is for
current-project regression only, not for restoring old-project support.
