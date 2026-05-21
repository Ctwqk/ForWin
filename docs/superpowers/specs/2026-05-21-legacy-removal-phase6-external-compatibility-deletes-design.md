# Legacy Removal Phase 6 External Compatibility Deletes Design

## Context

Phase 6 removes compatibility for old external callers and old operator
surfaces. A 60 chapter generation run cannot validate these paths, so this
phase relies on targeted unit/API/static tests.

Targets:

- `external.legacy_migration_scripts`
- `external.route_dependency_legacy_kwargs`
- `external.config_legacy_aliases`
- `external.publisher_plaintext_session_upgrade`
- `external.model_and_protocol_aliases`
- `ui.legacy_project_and_governance_labels`

The user has pre-authorized implementation after this spec and its plan are
written. No approval pause is required.

## Selected Approach

Hard-delete old external compatibility. Current callers must use current
objects, current env names, current encrypted publisher session storage, and
current schema keys.

## Runtime Design

### Route Dependencies

`ApiRouteDeps` and `api_task_routes.build_handlers()` accept only grouped deps.
Flat keyword construction is rejected by Python signature/type errors rather
than migrated internally.

### Config And Planning Aliases

`FORWIN_DB_PATH`, `FORWIN_LEGACY_PROVISIONAL_BLOCKING`,
`legacy_provisional_blocking`, `from_legacy_args`, and old quota/profile alias
names are removed.

Current replacement concepts:

- `FORWIN_DATABASE_URL`
- `Config.provisional_preview_enabled`
- explicit `PlanningServices(...)`
- current production quota names

### Publisher Sessions

Publisher session reads accept only current encrypted session records. The API
does not ask the manager to upgrade plaintext storage. Metadata no longer
stores `"legacy"`.

### Protocol And Writer Inputs

Current schemas reject old aliases:

- personality model old key adapters
- old repair scope aliases
- writer JSON preview fallback format

Writer output must use the current response shape.

### UI Labels

The home UI stops offering or naming old governance/provisional-preview labels.
`legacy_relaxed` is removed from selectors and defaults. `Legacy Preview` is
renamed to current provisional preview wording.

### Migration Scripts

One-shot old migration scripts are deleted:

- `scripts/migrate_legacy_canon_to_form.py`
- `scripts/migrate_sqlite_to_postgres.py`

Tests that only validate these scripts are removed or repointed to current
migration/replay behavior.

## Inventory

After each subdomain is deleted, mark the matching inventory entry deleted with
narrow residual patterns.

## Testing

Required verification:

```bash
python3 -m pytest tests/test_architecture_boundaries.py tests/test_api_task_routes.py -q
python3 -m pytest tests/test_config_env_resolution.py tests/test_postgres_engine.py tests/test_phase05_regressions.py tests/test_scenario_rehearsal.py -q
python3 -m pytest tests/test_publisher_routes_security.py -q
python3 -m pytest tests/test_personality_runtime.py tests/test_repair_instruction.py tests/test_chapter_writer.py -q
python3 scripts/audit_legacy_inventory.py --check --strict-patterns
python3 -m compileall -q forwin
git diff --check
```

When a named test file does not exist, use the closest existing file covering
the changed module and record it in the commit notes.

Residual production grep for Phase 6 allow patterns must return no hits in
their target paths.

## Rollback

Rollback is a git revert of the Phase 6 implementation commit. There are no
feature flags for old external compatibility.
