# Legacy Removal Phase 4B Metadata Source Fields Design

## Context

Phase 4 removed location and project lifecycle compatibility. One runtime
Phase 4 inventory entry remains:

- `metadata.legacy_source_fields`

This is not rename-only documentation. It is runtime metadata that can still
write or read `legacy_source` for SubWorld map region provenance.

The user has pre-authorized implementation after this spec and its plan are
written. No approval pause is required.

## Selected Approach

Delete the old metadata key and keep the current source key:

- Current key: `region_source`
- Deleted key: `legacy_source`

`StateRepository.get_active_subworld_region_drafts()` reads
`metadata["region_source"]` for persisted `MapRegionRow` rows and defaults to
`"map_regions"` when absent. It does not read `metadata["legacy_source"]`.

`SubWorldManager._persist_region_seeds()` writes `region_source` into map region
metadata. It does not write `legacy_source`.

## Runtime Design

Persisted map region rows use one provenance field:

```json
{
  "region_source": "runtime_generated"
}
```

The subworld metadata path already uses `region_source` and
`region_promotion_state`; Phase 4B keeps that shape.

Old rows that only contain `legacy_source` are intentionally not migrated in
runtime code. If such data appears, it is treated as missing source and falls
back to `"map_regions"`.

## Inventory

Mark `metadata.legacy_source_fields` as deleted. Its residual patterns remain
narrow:

- `"legacy_source"`
- `'legacy_source'`
- `_record_subworld_legacy_compatibility`

`region_source` is current and must not remain an allow pattern for a deleted
entry.

## Testing

Required verification:

```bash
python3 -m pytest tests/test_subworld_control.py -q
python3 scripts/audit_legacy_inventory.py --check --strict-patterns
git grep -n -E "\"legacy_source\"|'legacy_source'|_record_subworld_legacy_compatibility" -- forwin/state/repo.py forwin/subworld_manager.py
python3 -m compileall -q forwin
git diff --check
```

The grep must return no production hits.

## Rollback

Rollback is a git revert of the Phase 4B implementation commit. There is no
runtime flag to re-enable `legacy_source`.
