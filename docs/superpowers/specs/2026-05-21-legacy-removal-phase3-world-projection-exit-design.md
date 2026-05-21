# Legacy Removal Phase 3 World Projection Exit Design

## Context

Phase 3 removes legacy WorldModel and world v4 compatibility projection from
runtime generation. Phase 2 proved that canonical character identity can run on
BookState ids. The next remaining runtime legacy event is
`projection.legacy_world_model_projection`.

The source of truth is `docs/designs/legacy-inventory.yaml`, especially:

- `projection.world_v4_compat`
- `legacy_compatibility_audit_runtime`
- `review_engine.legacy_telemetry_names` only where it refers to projection
  failure telemetry

The user has pre-authorized execution after the spec and plan are written. This
document is the phase contract; no approval pause is required.

## Current Finding

`FORWIN_WORLD_V4_COMPAT_WRITE=false` is not enough to prove projection removal.
`forwin/orchestrator_loop_core/world_projection.py` already guards the
`WorldModelCompilerV4.compile_gate_verdict` branch behind that flag, but
`forwin/orchestrator_loop_core/finalization.py` still calls
`LegacyWorldModelCompiler.compile_after_chapter` after acceptance and records
`projection.legacy_world_model_projection` every time it runs.

Therefore the Phase 3 deletion must remove both:

- the flag-gated world v4 compatibility write branch
- the unconditional legacy WorldModel compile-after-acceptance path

## Selected Approach

Three approaches were considered:

1. **Only set `FORWIN_WORLD_V4_COMPAT_WRITE=false`.** This leaves the
   unconditional finalization compile path live and produces false confidence.
2. **Remove write paths but keep legacy modules available for debug and old API
   reads.** This reduces runtime events but leaves active production imports and
   old debug routes that new code can accidentally reuse.
3. **Delete projection runtime, debug routes, config flags, and compatibility
   modules from production; retain only historical migrations/docs and current
   world-model read helpers that still have live owners.** This is the chosen
   approach. BookState and retrieval stay current; legacy projection code is
   removed rather than made dormant.

## Scope

In scope:

- Replace post-acceptance legacy WorldModel compile with a BookState-only
  success path that returns `True` and records no legacy projection events.
- Remove the world v4 compatibility write branch from BookState commit flow.
- Remove `world_v4_compat_write_enabled` and `enable_world_v4_debug_api` config
  fields and env parsing.
- Remove world v4 debug routes from API route registration.
- Delete `api_world_model_v4_routes.py`, `world_v4_compat`, and
  `world_model_v4` production modules.
- Remove legacy WorldModel compiler imports from orchestrator code.
- Remove `projection.legacy_world_model_projection` from
  `LEGACY_COMPATIBILITY_REGISTRY`.
- Split `projection.world_v4_compat` so deleted projection/v4 runtime symbols
  are marked deleted, while current `forwin/world_model` read/storage helpers
  remain inventoried under a later rename/external phase.
- Update tests so BookState direct commit/review remains covered without world
  projection writes.

Out of scope:

- Removing current Knowledge Projection / LLM KB refresh. That path writes
  current retrieval artifacts and must remain.
- Removing Obsidian publisher/importer features that are explicitly handled by
  later external-compat phases.
- Renaming or deleting remaining `world_model` read/storage helpers that still
  serve current World Studio, Obsidian, retrieval, or Genesis bootstrap paths.
  They must stay in the legacy inventory as targets, but Phase 3 does not claim
  they are removed.
- Running final `--final` inventory audit; that waits until all phases complete.

## Runtime Design

### Post-Acceptance Path

`_compile_world_model_after_acceptance` remains temporarily as a compatibility
method name for callers, but its implementation becomes BookState-only:

- no `LegacyWorldModelCompiler`
- no `WORLD_MODEL_COMPILE_*` decision events
- no `LEGACY_PROJECTION_FAILED` event
- no `projection.legacy_world_model_projection` compatibility event
- returns `True`

The method can be renamed in Phase 5 if the name is still misleading after
runtime deletion.

### BookState Commit Path

`world_projection.py` keeps BookState direct commit and Knowledge Projection
refresh. It removes the `world_v4_compat_write_enabled` branch entirely.

### API And Config

World v4 debug API is removed from route registration. Config no longer accepts:

- `FORWIN_WORLD_V4_COMPAT_WRITE`
- `FORWIN_ENABLE_COMPAT_DEBUG_API`
- `world_v4_compat_write_enabled`
- `enable_world_v4_debug_api`

If a caller still sets those env vars, they are ignored by current runtime. Later
external-compat cleanup can turn ignored old env names into explicit validation
errors.

### Modules And Tables

Production modules under `forwin/world_v4_compat` and `forwin/world_model_v4`
are deleted. Historical migrations that created world model tables remain. This
phase does not have to drop historical tables before the 60 chapter runtime
pilot, because table presence alone does not create runtime legacy events.

`forwin/world_model` is not deleted in this phase. It still backs current
World Studio/API pages, Obsidian import/export, retrieval helpers, and Genesis
bootstrap behavior. The inventory must therefore split this path into a
separate later-phase target instead of counting it as Phase 3 deletion.

## Runtime Audit

After code removal, `projection.legacy_world_model_projection` is removed from
`LEGACY_COMPATIBILITY_REGISTRY`. A 60 chapter pilot must report zero projection
legacy events.

## Testing

Required checks:

```bash
python3 scripts/audit_legacy_inventory.py --check --strict-patterns
python3 -m pytest tests/test_architecture_boundaries.py tests/test_legacy_inventory.py tests/review_engine/test_legacy_compatibility_audit.py -q
python3 -m pytest tests/test_world_v4_orchestrator_gate.py tests/test_world_v4_aliases.py tests/test_config_defaults.py tests/test_quality_profile.py -q
python3 -m pytest tests/test_character_creation_helper.py tests/test_subworld_control.py -q
python3 -m compileall -q forwin
git diff --check
git grep -n -E 'projection\\.legacy_world_model_projection|LegacyWorldModelCompiler|WorldModelCompilerV4|legacy_projection|LEGACY_PROJECTION_FAILED|world_v4_compat_write_enabled|enable_world_v4_debug_api' -- forwin scripts ':!forwin/migrations/versions'
```

The grep must return no production runtime hits. Historical docs and migrations
are not production runtime.

## Container Pilot

The Phase 3 runtime signal must be collected from the local checkout
`/home/taiwei/ForWin`, not a worktree, so `.env` model profiles such as Kimi,
DeepSeek, and Minimax are loaded correctly.

Use container deployment:

```bash
cd /home/taiwei/ForWin
FORWIN_ENV_FILE=.env docker compose up -d --build forwin forwin-mcp
```

Then create or continue a clean 60 chapter pilot through ForWin MCP tools. The
acceptance audit command is:

```bash
python3 scripts/audit_review_engine_cutover.py \
  --project-id <project_id> \
  --expected-chapters 60 \
  --include-legacy-compat
```

Required Phase 3 signal:

- `engine_live_chapters == 60`
- `legacy_compat.per_feature_detail.projection.legacy_world_model_projection`
  is absent or has `events == 0`
- `legacy_compat.total_events` does not include projection compatibility events

## Rollback

Rollback is a git revert of the Phase 3 commit. Because old projection writes
are intentionally unsupported after this phase, rollback is only for current
BookState generation regressions, not for restoring old world v4 compatibility.
