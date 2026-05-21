# Legacy Removal Phase 7 Test And Doc Follow-Up Design

## Context

Phase 7 removes the last active rename-only inventory entry:

- `projection.world_model_current_helpers`

Phase 6 already removed old external compatibility. The final audit now fails
only because `forwin/world_model` still describes current projection/export
helpers as legacy and still accepts `legacy_entity_id` Obsidian frontmatter as
an identity source.

The user has authorized execution after this spec and plan are written. No
approval pause is required.

## Selected Approach

Treat `forwin.world_model` as a deprecated current projection/export facade,
not as legacy runtime compatibility.

This phase will:

- remove legacy wording from `forwin/world_model` production docstrings and
  current README text;
- remove `legacy_entity_id` frontmatter identity support from
  `WorldModelPageRepository`;
- keep migration-history files untouched;
- mark `projection.world_model_current_helpers` deleted in the inventory.

## Runtime Design

`WorldModelPageRepository.identity_for_values()` will resolve identities from
current frontmatter keys only:

- `node_id` -> `book_state_node`
- `forwin_id` / page key -> Genesis or `world_model_page`

`legacy_entity_id` will no longer participate in current Obsidian import,
export, or page-ranking behavior.

## Documentation Design

`forwin/world_model/README.md` and `forwin/world_model/__init__.py` will say
that the package is a deprecated projection/export facade retained for
inspection and import/export workflows. They must not describe it as a legacy
compatibility runtime path.

## Testing

Required verification:

```bash
python3 scripts/audit_legacy_inventory.py --check --strict-patterns
python3 scripts/audit_legacy_inventory.py --check --final --strict-patterns
python3 -m pytest tests/test_architecture_boundaries.py -q
python3 -m pytest tests/test_world_model_repository.py tests/test_obsidian_importer.py -q
python3 -m compileall -q forwin
git diff --check
```

If named test files do not exist, use the closest tests covering
`WorldModelPageRepository`, Obsidian import/export, and the inventory audit, and
record the substitution.

## Rollback

Rollback is a git revert of the Phase 7 implementation commit. There are no
feature flags for restored `legacy_entity_id` frontmatter support.
