# Legacy Removal Phase 1 Design

## Context

Phase 0 froze production legacy references behind `docs/designs/legacy-inventory.yaml`
and a strict audit gate. Phase 1 removes compatibility that does not require a
60 chapter runtime signal:

- `book_state.legacy_import`
- `governance.legacy_relaxed`
- `api.legacy_checkpoint_status`

This phase intentionally drops old-project and old-client behavior. The user has
pre-authorized execution after the spec and plan are written, so this document is
the audit contract rather than a human approval checkpoint.

## Selected Approach

Three approaches were considered:

1. **Delete all Phase 1 code in one patch.** Fast, but review and rollback would
   be too coarse because API removal, governance defaults, and checkpoint
   serialization touch different boundaries.
2. **Delete by inventory entry with targeted tests.** Slightly slower, but each
   removed entry has a direct test signal and inventory status change. This is
   the chosen approach.
3. **Keep deprecated API shells that return 410.** Safer for old clients, but it
   violates the full legacy exit decision because old compatibility is no longer
   supported.

## Scope

In scope:

- Remove `LegacyBookStateImporter`, the `/book-state/legacy-import` route,
  response schema, package export, and region-promotion event types.
- Remove `legacy_relaxed` as an accepted governance progression mode.
- Make empty governance payloads normalize to current strict defaults.
- Remove checkpoint status normalization from old `"approved"` to `"overridden"`.
- Remove `checkpoint_reason_with_legacy_status` and its runtime audit event.
- Remove the three Phase 1 runtime registry entries after code is gone.
- Mark Phase 1 inventory entries as `deleted`, retaining their audit patterns so
  reintroduction fails.
- Update tests that depended on the removed old behavior.

Out of scope:

- Character identity bridge removal. That is Phase 2 and still requires a 60
  chapter signal.
- World projection exit. That is Phase 3 and needs the flag-off 60 chapter pilot.
- UI label cleanup for `legacy_relaxed`; the UI inventory entry remains Phase 6.
- Migration history containing old status strings.

## Design

### BookState Import Removal

The import route is removed from handler construction and route registration.
`BookStateLegacyImportResponse` is removed from API schema exports.
`forwin/book_state/legacy_import.py` is deleted, and `forwin/book_state/__init__.py`
no longer exports `LegacyBookStateImporter`.

Tests that directly exercised the importer are removed or rewritten to cover the
current BookState compiler/adapter path without import. The architecture and
inventory tests become the guard against accidental reintroduction.

### Governance Mode Removal

`ProgressionMode` becomes `Literal["serial_canon", "serial_canon_band_guard"]`.
`ProjectGovernanceSettings.progression_mode` defaults to
`serial_canon_band_guard`. `normalize_project_governance` no longer accepts
`treat_empty_as_legacy`; empty or invalid payloads merge into current strict
defaults.

Runtime paths no longer emit `governance.legacy_relaxed_fallback` because there
is no fallback. Existing code that passes `treat_empty_as_legacy` is updated.
Config/runtime settings normalization rejects `legacy_relaxed` by falling back to
`serial_canon_band_guard`.

### Checkpoint Status Removal

`normalize_checkpoint_status` only accepts current checkpoint statuses. Unknown
persisted values normalize to `"error"` instead of being treated as old
approvals. `checkpoint_reason_with_legacy_status` is removed, and serializers use
the stored reason unchanged. The API no longer records
`api.legacy_checkpoint_status` compatibility events.

This intentionally changes old `"approved"` rows from accepted/overridden to
invalid/error in serialized output.

## Testing

Required checks:

```bash
python3 scripts/audit_legacy_inventory.py --check --strict-patterns
python3 -m pytest tests/test_legacy_inventory.py tests/test_architecture_boundaries.py tests/review_engine/test_legacy_compatibility_audit.py -q
python3 -m pytest tests/test_governance_decision_api.py tests/test_generation_audit_checkpoints.py -q
python3 -m pytest tests/test_book_state_final.py -q
git grep -n -E 'LegacyBookStateImporter|import_book_state_legacy|BookStateLegacyImportResponse|legacy_relaxed|checkpoint_reason_with_legacy_status|api\\.legacy_checkpoint_status|governance\\.legacy_relaxed_fallback' -- forwin scripts
```

The `git grep` command must return no production hits except retained deleted
inventory/audit history that is intentionally outside runtime behavior.

## Rollback

Phase 1 lands as a commit group. If a current project path breaks, revert this
phase as a normal git revert. No DB migration is introduced in this phase.
