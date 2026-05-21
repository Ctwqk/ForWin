# Legacy Removal Phase 4 Location And Project Lifecycle Design

## Context

Phase 4 removes old-project state fallbacks that still affect new generation:

- `book_state.state.location_fallback`
- `book_state.state.location_patch_warning`
- `project.creation_status_legacy`
- `genesis.legacy_pack_payload_upgrade`

The user has pre-authorized implementation after this spec and its plan are
written. No approval pause is required.

## Selected Approach

Use a hard delete rather than another shadow period.

1. Location identity becomes `location_id` only. BookState review rejects
   `state.location` movement patches, runtime/map resolution no longer reads
   `state.location`, and compatibility events are removed from the registry.
2. Project creation lifecycle stops defaulting to `"legacy"`. New projects use
   explicit Genesis lifecycle states such as `creating`, `genesis_ready`, or
   `writing`. API/MCP payload models no longer imply an old project when the
   field is absent.
3. Old Genesis pack top-level payload upgrade is deleted. Current packs must
   provide `world` directly; `world_bible`, `map_atlas`, and `story_engine`
   are not auto-promoted into `world`.

## Runtime Design

### Location

`forwin/book_state/runtime.py::_resolve_location` returns only:

- `state.location_id`
- `current_activity_id -> current_location_id`
- faction `headquarters_location_id`

If none exists, it returns an empty string. It no longer records
`book_state.state.location_fallback`.

`forwin/book_state/reviewer.py` treats `state.location` as an invalid movement
field. `_is_location_patch()` accepts only `state.location_id`.

`forwin/map/service.py::resolve_world_node_location_id` mirrors runtime
resolution and no longer returns `state.location`.

### Project Lifecycle

`Project.creation_status` default becomes `creating`. Project creation helpers
accept a current explicit status and normalize empty input to `creating`, not
`legacy`.

Genesis-only guards no longer record `project.creation_status_legacy`. A
project with `creation_status="legacy"` is treated as an invalid/currently
unsupported lifecycle value, not as a compatibility path.

Project payloads, MCP models, and API schemas default missing status to
`creating` or the actual persisted value. They do not serialize absent status
as `legacy`.

### Genesis Pack Shape

`_legacy_world_root_from_pack()` is removed. `_initial_pack_dummy_merge()` starts
from `payload["world"]` when present and otherwise uses an empty current world
root. Top-level old sections are ignored rather than promoted.

`GenesisNormalizer` docstrings and helpers describe current pack normalization
only.

## Inventory

Mark these entries deleted after code removal:

- `book_state.state.location_fallback`
- `project.creation_status_legacy`
- `genesis.legacy_pack_payload_upgrade`

Remove the corresponding runtime registry entries from
`LEGACY_COMPATIBILITY_REGISTRY`.

## Testing

Required verification:

```bash
python3 -m pytest tests/test_book_state_runtime.py tests/test_book_state_final.py tests/test_map_runtime.py -q
python3 -m pytest tests/test_project_creation_status.py tests/test_project_api.py tests/test_mcp_models.py -q
python3 -m pytest tests/test_book_genesis_core.py tests/test_genesis_workspace.py tests/test_architecture_boundaries.py tests/review_engine/test_legacy_compatibility_audit.py -q
python3 scripts/audit_legacy_inventory.py --check --strict-patterns
python3 -m compileall -q forwin
git diff --check
```

Residual production grep must return no hits:

```bash
git grep -n -E 'book_state\.state\.location_fallback|book_state\.state\.location_patch_warning|project\.creation_status_legacy|_record_project_creation_status_legacy_compatibility|_legacy_world_root_from_pack|legacy payload compatibility|state\.location([^_a-zA-Z0-9]|$)' -- forwin scripts ':!forwin/migrations/versions'
```

`state.location_id` is allowed; the grep pattern excludes the underscore suffix.

## Runtime Pilot Signal

The next clean 60 chapter pilot must report zero events for the three removed
runtime compatibility features. Because the registry entries are removed, their
expected signal is absence from `legacy_compat.per_feature_detail`.

## Rollback

Rollback is a git revert of the Phase 4 commit. There is no runtime flag to
re-enable old project lifecycle or `state.location` fallback.
