# Legacy Removal Spec — Full Legacy Exit

## Goal

ForWin will fully exit legacy compatibility. This means more than deleting the
old-project runtime fallbacks: every production use of `legacy`, `Legacy`, or
`LEGACY` must have an explicit final state.

Final state:

- New-project generation, review, BookState, SubWorld, Genesis, API, publisher,
  and governance runtime paths do not depend on legacy compatibility.
- Production code cannot add a new legacy reference unless it is first recorded
  in `docs/designs/legacy-inventory.yaml`.
- All active inventory entries in `runtime_delete`, `rename_only`, and
  `external_compat_delete` reach `status: deleted`.
- `LEGACY_COMPATIBILITY_REGISTRY` is empty or removed after the runtime deletion
  work lands.
- A clean 60 chapter pilot reports `engine_live_chapters == 60`,
  `legacy_safety_net_chapters == []`, `severe_mismatch_chapters == []`, and
  `legacy_compat.total_events == 0`.
- Static final audit allows legacy wording only in `migration_history_keep`,
  historical docs, and deleted-entry audit history.

This spec intentionally drops old-project compatibility. Projects that only
work through `state.location`, `legacy_entity_id`, `legacy_relaxed`,
`creation_status="legacy"`, world v4 projection writes, or BookState legacy
import are no longer supported.

## Source Of Truth

`docs/designs/legacy-inventory.yaml` is the source of truth for all production
legacy references.

Each entry must declare:

- `id`
- `category`
- `owner_area`
- `paths`
- `reason`
- `removal_phase`
- `verification`
- `delete_when`
- `status`

Allowed categories:

- `runtime_delete`: runtime old-project or old-canon compatibility; must be
  removed.
- `rename_only`: current production path whose name still says legacy; must be
  renamed so new development does not copy the wrong concept.
- `external_compat_delete`: compatibility for old API clients, old publisher
  storage, old env names, or old constructor shapes; must be removed with
  targeted tests.
- `migration_history_keep`: historical migration or revision content that is
  retained but not allowed to leak into runtime paths.
- `test_doc_followup`: tests and current docs that must follow the production
  cleanup.
- `deleted`: historical audit record for removed entries; production paths must
  no longer match.

The inventory is a long-term gate, not a one-off cleanup file. After this
cleanup, new legacy work remains blocked by default and must be explicitly
registered with an owner, expiry condition, and deletion verification.

## Phase 0 — Inventory Freeze

Before deleting runtime code, freeze the boundary.

Deliverables:

- Add `scripts/audit_legacy_inventory.py`.
- Add an architecture test that runs the inventory audit.
- Make the audit scan production roots: `forwin/`, `scripts/`, and frontend
  production sources when present.
- Exclude generated caches, tests, historical docs, and pyc files from the
  production scan.
- Load `docs/designs/legacy-inventory.yaml`.
- Fail if a production legacy hit is not covered by an active inventory entry.
- Fail if a `deleted` entry still has production matches.
- Fail if a `migration_history_keep` entry appears outside its allowed
  migration/history paths.
- Emit a grouped report by category, owner area, and removal phase.

Acceptance:

```bash
python3 scripts/audit_legacy_inventory.py --check
python3 -m pytest tests/test_architecture_boundaries.py tests/test_legacy_inventory.py tests/review_engine/test_legacy_compatibility_audit.py -q
```

After Phase 0, parallel development can continue, but new production legacy
references must be registered first.

## Phase 1 — Low-Risk Deletes

Delete confirmed dead code and zero-usage compatibility that does not require a
60 chapter signal.

Targets:

- `dead_code.*` registry entries that already have zero static callers.
- `legacy_relaxed` governance mode and fallback default.
- `LegacyBookStateImporter`, its API route, response schema, and re-export.
- Legacy checkpoint status normalization if old clients are no longer
  supported.
- Matching `LEGACY_COMPATIBILITY_REGISTRY` entries after the code is gone.

Verification:

- Unit tests for governance, API schema, architecture boundaries, and audit.
- Inventory audit reports these entries as `deleted`.
- `git grep` finds no runtime hits for deleted ids.

## Phase 2 — Canonical Character Identity

Remove `legacy_entity_id` as a runtime bridge.

Targets:

- Character creation no longer creates legacy `Entity` rows.
- Character creation/request/result models drop `legacy_entity_id`.
- Character identity map drops `legacy_entity_id`.
- SubWorld roster resolution stops reading or writing legacy entity ids.
- Context packs stop exposing `legacy_entity_id`.
- Personality enrichment and registry lookup use canonical character ids only.
- DB migration removes `character_identity_map.legacy_entity_id` and its index.
- Runtime audit removes:
  - `subworld.legacy_entity_id_bridge`
  - `subworld.create_legacy_entity`
  - `characters.create_legacy_entity_default_true`

Verification:

- Character, SubWorld, personality, context assembler, and BookState tests pass.
- A 60 chapter pilot reports zero events for the three identity features above.

## Phase 3 — World Projection Exit

Remove world v4 and legacy world model projection writes.

Process:

1. Run one 60 chapter pilot with `FORWIN_WORLD_V4_COMPAT_WRITE=false`.
2. Confirm `projection.legacy_world_model_projection` event count is zero.
3. Delete world v4 compatibility write paths, config fields, debug routes, and
   tables.
4. Keep BookState direct commit/review paths intact.

Targets:

- `forwin/world_v4_compat/`
- world v4 projection branches in orchestrator loop code
- `world_v4_*` compatibility tables
- `projection.legacy_world_model_projection`

Verification:

- 60 chapter pilot before deletion proves the flag-off path works.
- Unit tests prove BookState canon still commits and gates correctly.
- Final 60 chapter pilot has no projection legacy event.

## Phase 4 — Location And Project Creation Status

Remove old project state fallbacks.

Targets:

- `state.location` fallback in BookState runtime, map service, context overlays,
  conflict detection, review warnings, and extraction handling.
- `creation_status="legacy"` defaults and fallback branches in project model,
  API schema, MCP model/client, Genesis payloads, and project payload serializers.
- `project.creation_status_legacy` instrumentation after the branch is gone.

Verification:

- New Genesis projects use the current creation lifecycle states.
- New chapters resolve locations from `location_id` only.
- 60 chapter pilot reports zero events for:
  - `book_state.state.location_fallback`
  - `book_state.state.location_patch_warning`
  - `project.creation_status_legacy`

## Phase 5 — Rename-Only Cleanup

Remove misleading legacy names from current production paths.

Targets:

- Genesis workspace facades that call current `book_genesis` helpers but name
  the local module `_legacy()` or `legacy`.
- Genesis pack compatibility helper names that describe current pack shape as
  legacy when the path is still active.
- Review-engine telemetry names that preserve `legacy_*` fields after the
  runtime safety net is gone.
- Writer/parser helper names such as `_legacy_json_preview_is_accepted` if the
  behavior is still a valid current fallback.

Rule:

- If the behavior is still current, rename it.
- If the behavior only supports old payloads or old model output, delete it.

Verification:

- Genesis workspace tests pass.
- Review engine audit tests pass.
- Static inventory audit shows `rename_only` entries as `deleted`.

## Phase 6 — External Compatibility Deletes

Delete compatibility for old external callers. These are not validated by a 60
chapter generation run.

Targets:

- Flat `ApiRouteDeps(**legacy)` constructor shape.
- `TaskRouteDeps(**legacy_deps)` constructor shape.
- Legacy env aliases such as `FORWIN_DB_PATH`.
- Publisher plaintext session upgrade and `upgrade_legacy=True`.
- Production policy old quota aliases.
- Retrieval and personality model compatibility shims.
- Review protocol old scope adapters.

Verification:

- Targeted API tests assert old shapes are rejected with clear errors.
- Publisher session tests assert only encrypted/current session storage works.
- Config tests assert current env names work and old aliases do not.
- Inventory audit shows `external_compat_delete` entries as `deleted`.

## Phase 7 — Test And Doc Follow-Up

Update tests and current docs after production changes.

Rules:

- Test names should not call the new current path legacy.
- Historical tests for migrations may keep legacy language if they are marked
  `migration_history_keep`.
- Current architecture docs must not describe legacy projection, legacy entity
  ids, or old project modes as supported runtime behavior.

## Phase 8 — Final Gate

Final verification:

```bash
python3 scripts/audit_legacy_inventory.py --check --final
python3 -m pytest -q
python scripts/audit_review_engine_cutover.py \
  --project-id <new_60_chapter_project_id> \
  --expected-chapters 60 \
git grep -n -E 'legacy|Legacy|LEGACY' -- forwin scripts
```

Required results:

- `--final` passes.
- Full tests pass.
- 60 chapter audit reports:
  - `engine_live_chapters == 60`
  - `baseline_safety_net_chapters == []`
  - `severe_mismatch_chapters == []`
- The pre-deletion Phase 8 pilot recorded `legacy_compat.total_events == 0`;
  after Phase 8 deletion, the compatibility audit runtime and
  `--include-legacy-compat` flag no longer exist.
- Remaining `git grep` hits are only migration history, deleted-entry audit
  history, or historical docs/tests outside the production scan.

## Parallel Work Boundaries

The work can run in parallel after Phase 0:

- Worker A: `runtime_delete` identity/subworld/BookState/location.
- Worker B: world projection exit.
- Worker C: Genesis and review-engine `rename_only`.
- Worker D: external API/config/publisher compatibility.
- Worker E: tests and current docs.

Workers must not edit the same files in parallel. If a file appears in multiple
inventory entries, the phase owner for the earliest phase owns that file until
their PR lands.

## Rollback

- Each phase should be a separate PR or commit group.
- Runtime deletion phases must be revertable with a normal git revert.
- DB migrations must include downgrade functions even though old-project support
  is intentionally not retained.
- If a 60 chapter pilot finds a runtime event after deletion, revert that phase
  or reclassify the event in inventory before continuing.
