# ForWin Review Follow-Up Cleanup Design

Date: 2026-05-18

Status: approved for implementation planning

## Scope

This spec fixes three review findings from the quality/architecture cleanup branch:

- `governance_keywords` currently treats negation at the keyword level, causing false negatives when one occurrence is real and a later occurrence is negated.
- `ApiRouteDeps` contains an empty `WorldModelDeps` placeholder that does not own real dependencies.
- `PublisherDeps` duplicates `CoreDeps` fields, so publisher dependency ownership is unclear.

The work is intentionally narrow. It does not reopen the wider API dependency split, legacy module migration, prompt regression work, or giant-file cleanup.

## Goals

- Detect any unnegated keyword occurrence even if another occurrence of the same keyword is negated.
- Remove the empty `WorldModelDeps` group instead of preserving a placeholder.
- Make `PublisherDeps` the only publisher dependency owner while keeping old `deps.get_publisher_manager` style access working through `ApiRouteDeps.__getattr__`.
- Add focused regression tests so these mistakes do not return.

## Non-Goals

- Do not redesign all `ApiRouteDeps` groups.
- Do not split home/system/orchestrator dependencies out of `CoreDeps` in this change.
- Do not delete or alter world-model route behavior.
- Do not change public HTTP endpoint paths or handler behavior.
- Do not remove legacy constructor compatibility for `ApiRouteDeps`.

## Approach Options Considered

### Option A: Minimal Bug Fix Only

Fix only `text_has_unnegated_keyword` and leave API dependency design smells as-is.

Trade-off: lowest code churn, but leaves the two review-confirmed architecture issues unresolved. This is not enough because the review explicitly called out the dependency grouping as misleading.

### Option B: Focused Cleanup

Fix occurrence-level negation, delete `WorldModelDeps`, and make publisher dependencies owned only by `PublisherDeps`.

Trade-off: small API registry refactor plus test updates, but the behavior stays stable because `ApiRouteDeps.__getattr__` remains the compatibility bridge. This is the recommended option.

### Option C: Broader Dependency Repartition

Revisit all `CoreDeps` fields and split home/system/orchestrator dependencies into additional domain groups.

Trade-off: could improve structure, but it expands scope beyond the review findings and risks turning a follow-up patch into another architectural phase.

## Selected Design

Use Option B.

## Governance Keyword Semantics

Add occurrence-level negation handling in `forwin/governance_keywords.py`.

New internal helper:

```python
def _occurrence_is_prefix_negated(text: str, keyword: str, index: int, *, window: int = 12) -> bool:
    ...
```

The helper evaluates exactly one keyword occurrence. It keeps the existing local-window behavior:

- scan only the text between the nearest preceding clause boundary and the occurrence, capped by `window`
- treat `避免`, `不要`, `不得`, `不能`, `防止`, `禁止`, and `阻止误写` as negation markers
- do not implement cross-clause or syntactic negation in this follow-up

`text_has_unnegated_keyword(text, keywords)` should iterate every occurrence of every keyword and return `True` when at least one occurrence is not prefix-negated.

`first_unnegated_keyword(text, keywords)` should use the same occurrence-level logic and return the first keyword that has an unnegated occurrence.

`keyword_is_prefix_negated(text, keyword)` remains available for compatibility, but its semantics should no longer cause mixed-occurrence false negatives. It should return `True` only when the keyword appears and every occurrence is prefix-negated. If the keyword is absent, return `False`.

## API Dependency Cleanup

### Delete `WorldModelDeps`

Remove:

- `WorldModelDeps`
- `ApiRouteDeps.world_model`
- `world_model` constructor parameter
- unconditional `WorldModelDeps()` construction
- `world_model` from the `__getattr__` group search list

Tests that assert the `ApiRouteDeps` group list should expect:

- `core`
- `task`
- `project`
- `governance`
- `observability`
- `publisher`

No route handler should need a `world_model` dependency group because the current world model routes still use shared session/config dependencies.

### Make `PublisherDeps` The Publisher Owner

Remove publisher fields from `CoreDeps`:

- `get_publisher_manager`
- `render_publishers_page`

Keep them in `PublisherDeps`.

`ApiRouteDeps.__init__` should support both construction styles:

1. preferred grouped style:

```python
ApiRouteDeps(core=..., publisher=...)
```

2. legacy flat kwargs style:

```python
ApiRouteDeps(get_publisher_manager=..., render_publishers_page=..., ...)
```

Construction order should avoid requiring publisher fields in `CoreDeps`:

1. construct `CoreDeps` from only core annotations
2. construct `PublisherDeps` from explicit `publisher` or legacy publisher kwargs
3. construct remaining groups as today
4. error on leftover unexpected legacy fields

`register_api_routes()` should read publisher dependencies explicitly:

```python
get_publisher_manager = deps.publisher.get_publisher_manager
render_publishers_page = deps.publisher.render_publishers_page
```

Old call sites that still use `deps.get_publisher_manager` should keep working because `ApiRouteDeps.__getattr__` searches `publisher`.

## Tests

### Governance Tests

Extend `tests/test_governance_keyword_registry.py` with a mixed occurrence regression:

```text
本章主角死亡。要避免死亡披露过早。
```

Expected behavior: `character_availability` violation triggers because the first `死亡` occurrence is not negated.

Keep the existing tests:

- single negated death mention does not trigger
- positive death mention triggers
- registry exposes keyword groups

### API Registry Tests

Update `tests/test_architecture_boundaries.py`:

- `test_api_route_deps_are_grouped_by_domain` no longer expects `world_model`
- add an assertion that `CoreDeps.__annotations__` does not contain publisher fields
- add an assertion that `PublisherDeps.__annotations__` owns those fields
- optionally instantiate `ApiRouteDeps` with legacy flat kwargs and assert `deps.get_publisher_manager is deps.publisher.get_publisher_manager`

### Verification Commands

Run:

```bash
python3 -m pytest tests/test_governance_keyword_registry.py -q
python3 -m pytest tests/test_architecture_boundaries.py tests/test_world_v4_aliases.py tests/test_runtime_container.py -q
python3 -m pytest tests/test_config_defaults.py tests/test_writer_prompt_contract.py -q
python3 -m compileall -q forwin
git diff --check
```

If the broader focused suite is cheap after these pass, run the same 211-test focused suite used after the previous phase. Postgres-backed tests remain environment-dependent and should be reported separately if `psycopg` is still absent.

## Done Criteria

- Mixed negated/unnegated keyword occurrence triggers correctly.
- Single negated occurrence remains non-triggering.
- `WorldModelDeps` no longer exists.
- `ApiRouteDeps` group list contains no `world_model` placeholder.
- `CoreDeps` no longer owns publisher dependencies.
- `PublisherDeps` is the only publisher dependency group.
- Legacy flat `ApiRouteDeps(...)` construction still works.
- Existing route registration behavior is unchanged.
- Focused tests and syntax checks pass.

## Risk Controls

- Keep `__getattr__` compatibility so old attribute access does not break.
- Keep public `register_api_routes(app, deps=...)` shape unchanged.
- Do not change endpoint registration order or route handler arguments except for reading publisher deps from the publisher group.
- Keep negation logic prefix-only; do not expand into cross-clause parsing in this follow-up.
