# Pulp Profile Upgrade Design

Date: 2026-05-18

Status: approved for implementation planning

Source plan:

- `Design-docs/pulp_profile_upgrade_plan.md`
- `Design-docs/trope_library_pulp_v1.md`

## Scope

This design implements the full Phase 1-6 pulp profile upgrade. It extends ForWin with a low-cost, high-throughput `quality_profile="pulp"` path while preserving the existing `standard` behavior for old and default projects.

The implementation should happen on one integration branch with phase-sized commits and phase-level verification. The final branch can later be split into PRs if review size requires it, but the work should be developed as one coherent pipeline because the profile switch, hard floor, review bypass, context truncation, trope injection, and pressure metrics validate each other.

## Goals

- Add `quality_profile = pulp | standard | premium` as a configuration-level profile switch.
- Keep `standard` as the default and preserve existing behavior unless the profile or specific env overrides are explicitly set.
- Make `pulp` reduce single-chapter LLM calls to the 2-3 call range by disabling heavy review and extraction paths.
- Add a deterministic hard floor gate so pulp mode still has a minimum quality boundary.
- Add context recency truncation so prompt/context cost stays effectively flat as chapter count grows.
- Extend trope templates, load the pulp seed markdown library, select low-cost tropes, and inject actionable four-part trope instructions into the writer prompt.
- Add a 30-chapter pressure-test script that records cost, hard floor, reward gap, context size, and runtime metrics.

## Non-Goals

- Do not redesign the BookState commit path.
- Do not change BookState core schema.
- Do not delete existing reviewers, checkers, fallback, retry, pause, cancel, or continue behavior.
- Do not change Genesis or publisher workflows.
- Do not add World Studio UI changes.
- Do not require the complete 188-template trope library in this implementation.
- Do not make `premium` behavior meaningful in the first pass beyond a supported empty override hook.

## Approach Options Considered

### Option A: One PR Per Phase

This gives clean review slices and easy rollback, but Phase 1-4 are strongly dependent. Splitting too early creates partially working profile states and slows verification of the real outcome: a full pulp pipeline.

### Option B: One Integration Branch With Phase Commits

This is the selected approach. Each phase gets its own implementation boundary and tests, but the branch can verify the entire 1-6 pipeline before review. It gives enough local isolation without pretending the phases are independent products.

### Option C: Pulp MVP First, Metrics Later

This would ship the visible switch quickly, but it would not prove that context cost flattens, reward gap stays bounded, or the writer receives real trope instructions. It risks producing a profile that exists in config but does not satisfy the original operational goal.

## Selected Design

Use Option B.

Pulp is a configuration-derived fast path, not a second orchestrator. The implementation should avoid scattered `if quality_profile == "pulp"` checks in business logic. Instead, `Config.from_env()` derives a final config using `apply_quality_profile()`, and existing components react to normal config fields:

- reviewer hub enabled flags
- canon quality gate mode
- BookState extraction layers
- context recency window
- trope selector cost ceiling
- hard floor enabled flag

The main `_run_project_chapters()` flow remains recognizable. It gains one new hard floor decision point after review and before operation-mode handling. The remaining changes are made at component boundaries.

## Phase 1: Quality Profile Configuration

### Design

Modify `forwin/config.py` so env parsing can distinguish explicit user configuration from default values. Pulp overrides must only apply to fields the user did not explicitly set.

`_env_values()` currently returns only a dict. Change it to return:

```python
tuple[dict[str, object], set[str]]
```

The set contains Config field names that were provided explicitly by env. Implement this with small local wrappers around the existing `_env_str`, `_env_bool`, `_env_int`, `_env_float`, and list helpers. The wrapper should record the field name when any mapped env key is present.

`Config.from_env()` becomes:

```python
@classmethod
def from_env(cls) -> "Config":
    values, explicit_keys = _env_values()
    config = cls(**values)
    return apply_quality_profile(config, explicit_keys=explicit_keys)
```

Add config fields:

- `quality_profile: Literal["pulp", "standard", "premium"] = "standard"`
- `book_state_layers: list[str] = ["world", "map", "cognition", "narrative"]`
- `hard_floor_gate_enabled: bool = False`
- `context_recency_window_chapters: int = 0`
- `map_movement_review_enabled: bool = True`
- `personality_review_enabled: bool = True`
- `canon_quality_review_in_hub_enabled: bool = True`

Add:

Add a `PULP_OVERRIDES: dict[str, Any]` map containing the override values from the source plan, and an intentionally empty `PREMIUM_OVERRIDES: dict[str, Any]` map. Add `apply_quality_profile(config, *, explicit_keys)` to pick the override map for the selected profile and return `config.model_copy` with an update map containing only non-explicit fields.

The `pulp` override set should follow the source plan, with one adjustment: after Phase 3 lands, `canon_quality_gate` should derive to `fatal_only` rather than `off`. Until Phase 3 exists, tests can assert the intended final value at the end of the phase sequence.

The `premium` override hook stays intentionally empty. It exists so the public enum does not need another migration when premium defaults are designed later.

### Governance

`WritingOrchestrator._project_governance(project)` must read the derived `self.config` fields. During implementation, verify that `auto_band_checkpoint`, `future_constraints_enabled`, and related governance fields come from the post-profile config. If any are cached before profile application, move that cache behind `Config.from_env()`.

### Tests

Add `tests/test_quality_profile.py`:

- `FORWIN_QUALITY_PROFILE=pulp` derives `writer_mode="single"`, `operation_mode="blackbox"`, `book_state_layers=["world"]`, `hard_floor_gate_enabled=True`, and `context_recency_window_chapters=50`.
- Explicit env wins, for example `FORWIN_QUALITY_PROFILE=pulp` plus writer mode env keeps the explicit writer mode.
- `standard` matches current defaults for representative fields.
- `premium` returns a valid config and does not change defaults.

### Acceptance

- Default `Config.from_env()` behavior remains standard.
- Pulp applies only through `quality_profile`.
- Explicit env values are never silently overwritten by profile defaults.

## Phase 2: Hard Floor Gate

### Design

Add the deterministic hard floor checker:

- `forwin/checker/hard_floor.py`
- `forwin/checker/hard_floor_dict.py`

Core API:

```python
class HardFloorResult(BaseModel):
    passed: bool
    fail_reasons: list[str]
    warning_reasons: list[str]
    checks: dict[str, bool]
    metadata: dict[str, Any] = Field(default_factory=dict)

def run_hard_floor(
    *,
    writer_output: WriterOutput,
    context_pack: ChapterContextPack,
    repo,
    project_id: str,
    chapter_number: int,
    config: Config,
) -> HardFloorResult
```

The hard floor should favor high-certainty deterministic checks. Blocking fail reasons:

- `chapter_length`: body length is below `config.min_chapter_chars`.
- `no_garbage`: empty body, obvious model artifacts, markdown JSON fences, instruction tokens, or long non-CJK/non-common-punctuation noise blocks.
- `protagonist_name_stable`: use the existing canonical name violation helper if available in this checkout.
- `at_least_one_event`: writer output has at least one event, state change, or thread beat.
- `must_not_reveal`: direct substring match against `context_pack.must_not_reveal`.

Warning-only checks:

- `ending_hook`: last 200 characters contain a hook signal.
- `reward_gap`: reward gap exceeds the configured pulp expectation.
- `dead_alive`, `teleport`, and `closed_thread`: warn or skip when evidence is missing. Only block if the reused deterministic source proves the violation.

This matches the existing direction that warn, uncertain, and no-evidence findings should not block.

### Orchestrator Integration

Insert hard floor after:

```python
residual_review_issues = self._review_issue_payloads(verdict)
canon_risk_level = self._review_canon_risk(verdict)
```

and before operation-mode branches in `forwin/orchestrator_loop_core/project_chapters.py`.

When `self.config.hard_floor_gate_enabled` is false, no behavior changes.

When hard floor fails:

- Mark the chapter `failed`.
- Persist `residual_review_issues` entries with `reviewer="hard_floor"`.
- Record a decision event. If `DecisionEventType.HARD_GATE_HIT` does not exist, add a clear new enum value instead of reusing an unrelated type.
- Commit the session and follow the existing failed chapter flow.

Do not alter pause, cancel, continue, retry, or fallback behavior.

### Tests

Add `tests/test_hard_floor.py`:

- one fail fixture per blocking rule
- one warning-only fixture proving warnings do not fail
- one pass fixture
- one integration-level test proving standard config does not run hard floor

### Acceptance

- Pulp has a minimum deterministic quality boundary.
- Standard projects do not see hard floor behavior.
- Hard floor decisions are visible in status and decision events.

## Phase 3: Pulp Bypass Points

### Reviewer Hub Switches

Extend `HistoricalReviewHub.__init__`:

```python
map_movement_review_enabled: bool = True
personality_review_enabled: bool = True
canon_quality_review_in_hub_enabled: bool = True
```

In `review()`:

- Wrap the hub-local `analyze_writer_output_quality()` call in `canon_quality_review_in_hub_enabled`.
- Keep existing `experience_review_enabled`.
- Wrap `map_movement_reviewer.review()`.
- Wrap `personality_reviewer.review()`.
- Keep continuity, lint, and governance deterministic paths available unless explicitly disabled by existing config.

Find hub construction in `forwin/runtime/container.py` and/or `forwin/runtime/factories.py` and pass the new config fields through.

### Canon Quality `fatal_only`

Extend `forwin/canon_quality/gate.py`:

```python
GateMode = Literal["off", "shadow", "fatal_only", "strict"]
```

`fatal_only` should block only high-certainty fatal canon signals. The starting fatal set should include:

- `character_dead_alive`
- `character_teleport`
- `closed_thread_reopened`
- `final_dangling`
- `final_denied`
- `countdown_inconsistent`

Warnings, uncertain analyzer results, and signals without required evidence must not block. Preserve analyzer provenance fields such as source layer, analyzer, mode, original verdict, confidence, and blocking origin through normalization and payloads.

In `_apply_canon_quality_gate()`, call `analyze_writer_output_quality()` with `llm_client=None` when gate mode is `off` or `fatal_only`. Strict and shadow keep the existing LLM behavior unless config says otherwise.

### BookState Extraction Layers

Add layer selection to `BookStateGraphDeltaExtractor`:

```python
BookStateGraphDeltaExtractor(layers: set[str] | None = None)
```

Default layers preserve existing behavior:

```python
{"world", "map", "cognition", "narrative"}
```

Pulp passes `{"world"}` from `config.book_state_layers`.

The safest implementation point is after `BookStateDeltaAdapter().from_world_change_set()` returns, filtering graph deltas by stable layer metadata or delta type. If the current graph delta shape cannot reliably distinguish a layer, implement a conservative helper with explicit tests before filtering. Filtered deltas should leave metadata showing which layers were requested and how many deltas were removed.

Do not change BookState schema or commit semantics.

### Other Bypass Effects

The following existing switches should be driven by Phase 1 overrides rather than new logic:

- `future_constraints_enabled=False`
- `auto_band_checkpoint=False`
- `manual_checkpoints_enabled=False`
- `generation_audit_interval_chapters=0`
- `generation_audit_pause_enabled=False`
- `phase4_use_llm=False`
- `review_fail_max_rewrites=0`
- `reviewer_quality_mode="deterministic"`
- planning/final/band gate modes set to `off`

### Tests

Add `tests/test_pulp_pipeline_bypass.py`:

- mock hub dependencies and assert disabled reviewers are not called in pulp config
- assert canon quality hub collection is skipped when disabled
- assert `_apply_canon_quality_gate()` does not pass an LLM client in `fatal_only`
- assert BookState extraction with `["world"]` omits map/cognition/narrative deltas

### Acceptance

- Pulp single-chapter path avoids heavy reviewer and LLM gate calls.
- Standard review and extraction behavior remains unchanged.
- Fatal canon blockers still have a deterministic path.

## Phase 4: Context Recency Gate

### Design

Add `forwin/context/gates/recency_truncate.py`:

```python
class RecencyTruncateGate:
    name = "recency_truncate"

    def __init__(self, window_chapters: int = 0, max_entities: int = 0):
        # Store normalized window and entity cap.
        pass

    def validate(self, request, draft) -> list:
        # Mutate draft data in place and return validation issues.
        pass
```

The gate runs after providers and before `_build_pack()`. A window of `0` is no-op.

The gate should trim only data with reliable recency metadata:

- summaries
- recent state changes
- recent thread beats
- recent events
- entities with `last_seen_chapter` or equivalent recency fields

Items without chapter metadata should not be blindly removed. For entities, rank by:

1. recent appearance
2. direct relation to the current chapter plan
3. explicit importance
4. protagonist or core cast markers

Then cap to `max_entities` if provided. Pulp can rely on `context_recency_window_chapters=50`; `max_entities` may reuse existing retrieval caps rather than adding another config field in the first pass.

### Injection

The source plan says to append this in `ChapterContextAssembler._default_gates()`. If the assembler cannot currently access config there, inject the gate from the context assembler factory or retrieval broker construction site. Do not make the assembler import global config directly.

### Tests

Add `tests/test_context_recency_truncation.py`:

- construct a long-history draft and prove items older than the window are trimmed
- prove window `0` is no-op
- prove recent/core entities survive
- simulate chapter 100/150/200 and assert prompt/context character count remains within a small band

### Acceptance

- Standard context assembly is unchanged.
- Pulp context size does not grow linearly with chapter number.
- Recent and current-plan-critical entities remain available to the writer.

## Phase 5: Trope Schema, Selector, And Prompt Injection

### Phase 5a: Schema And Markdown Loader

Extend `forwin/protocol/trope_library.py::TropeTemplate` with defaulted fields:

- `subcategory`
- `market_tier`
- `cost_weight`
- `genre_fit`
- `pressure_shape`
- `protagonist_action`
- `visible_payoff`
- `audience_reaction`
- `next_hook_shape`
- `anti_patterns`
- `review_signals`
- `desire_setup`
- `resistance`
- `payoff`
- `aftermath`

Add `forwin/protocol/trope_md_loader.py::load_trope_templates_from_md(path)`.

The loader parses the format documented in `Design-docs/trope_library_pulp_v1.md`:

- H2 title: `## {template_id} · {display_name}`
- property list immediately after the H2
- H3 sections: `欲望建立`, `阻力加压`, `爽点兑现`, `余波钩子`, `anti_patterns`, `review_signals`

Update `load_trope_template_library()`:

- If `FORWIN_TROPE_TEMPLATE_PATH` points to `.md`, load via the markdown loader.
- If it points to JSON, preserve current JSON loading.
- If an override path is provided and fails to load, raise a clear error instead of silently falling back to seed. Silent fallback would hide a broken pulp seed.

Existing seed JSON must continue loading with default values.

### Phase 5b: Selector

Do not create a new selector module in this pass. Extend `BandExperienceScheduler.derive_band_delight_schedule()`:

- Add `cost_ceiling: int = 3`.
- Maintain `used_template_ids` across reward selection.
- Prefer templates in the requested category with `cost_weight <= cost_ceiling`.
- Deduplicate recently selected template IDs.
- Fall back deterministically by `(cost_weight, template_id)` when ideal candidates are missing.

Pulp call sites pass `cost_ceiling=2`; standard call sites can keep default `3`.

If the current service layer does not yet pass a profile-aware cost ceiling, add a small helper near the existing schedule construction rather than importing config into the scheduler.

### Phase 5c: Writer Prompt Injection

Update `forwin/writer/prompt_core/sections.py` so `ChapterExperiencePlan.selected_template_ids` expands into executable trope instructions instead of only listing IDs.

For at most two selected templates:

- show display name or template ID
- include `desire_setup`
- include `resistance`
- include `payoff`
- include `aftermath`
- include up to three anti-patterns

Apply a conservative character cap per template before appending to the prompt. Run or add tests around the existing prompt budget warning path so trope injection cannot silently exceed `prompt_budget_chars`.

### Tests

Add:

- `tests/test_trope_schema_compat.py`
- `tests/test_trope_md_loader.py`
- `tests/test_trope_selector.py`
- `tests/test_trope_prompt_injection.py`

The markdown loader test should parse the provided pulp library and assert at least the expected seed count in the current document. It should also validate one known template contains all four instruction sections.

### Acceptance

- Existing JSON seed remains valid.
- `FORWIN_TROPE_TEMPLATE_PATH=Design-docs/trope_library_pulp_v1.md` loads the markdown seed.
- Pulp selector favors low-cost, non-repeated templates.
- Writer prompt contains actionable four-part trope instructions and stays within budget.

## Phase 6: Pressure Test And Metrics

### Design

Add `scripts/pulp_pressure_test.py`.

Example:

```bash
FORWIN_QUALITY_PROFILE=pulp python scripts/pulp_pressure_test.py \
  --project-id PROJECT_ID \
  --chapters 30 \
  --output reports/pulp_test_TIMESTAMP/
```

The script should not bypass ForWin workflow rules. If it needs to start or continue generation against a running backend, use the configured ForWin MCP tools when operating live project/task/chapter truth. If it runs purely as a local diagnostic around existing services, it must still respect active task checks before mutation.

### Metrics

Record one row per chapter:

- `chapter_number`
- `wall_time_seconds`
- `llm_call_count`
- `output_token_count`
- `prompt_char_count`
- `context_pack_char_count`
- `hard_floor_passed`
- `hard_floor_fail_reasons`
- `reward_beats_in_plan`
- `reward_gap_since_last`
- `selected_trope_ids`
- `ending_hook_detected`
- `chapter_length`
- `bookstate_compile_succeeded`
- `rewrite_count`
- `verdict`

Prefer existing observability spans, writer metadata, chapter plans, context pack dumps, and hard floor results. If a metric is unavailable, write `null` and list the missing source in the generated report. Do not fabricate counts.

Output:

- `metrics.csv`
- `summary.json`
- `README.md`

### Summary Thresholds

Compute:

- prompt/context cost slope versus chapter number
- average LLM calls per chapter
- hard floor fail rate
- reward gap p95
- BookState compile failures
- average wall time per chapter

Operational targets:

- prompt/context slope stays effectively flat
- average LLM calls per chapter <= 3
- hard floor fail rate is observable and not extreme
- reward gap p95 <= 2
- BookState compile failures == 0 after allowed retry behavior
- average wall time per chapter <= 60 seconds

The hard floor 5%-20% target is a pressure-test quality target, not a unit-test assertion, because real LLM output varies. Unit tests should verify the calculation and report generation, not force a specific fail rate.

### Tests

Add focused script tests for:

- metrics row writing
- summary calculations
- missing metric source reporting
- reward gap calculation
- slope calculation

### Acceptance

- The script can generate all three report artifacts.
- Metrics distinguish real zero values from unavailable values.
- Pressure-test output is sufficient to decide whether the pulp profile meets the cost and quality goals.

## Data Flow

1. Environment variables build raw config values and explicit key metadata.
2. `apply_quality_profile()` derives final config.
3. Runtime factories pass final config into writer, reviewer hub, context assembler, and orchestrator dependencies.
4. `_run_project_chapters()` assembles context, writes, reviews, runs hard floor if enabled, then continues through the existing operation-mode and canon-commit flow.
5. Reviewer hub skips configured components in pulp mode.
6. Canon quality gate uses `fatal_only` and deterministic analysis in pulp mode.
7. BookState extractor filters generated deltas by requested layers.
8. Context assembler gate truncates old context items.
9. Experience scheduler selects low-cost trope templates.
10. Writer prompt sections inject trope instructions.
11. Pressure script reads runtime outputs and observability to produce chapter metrics and summary reports.

## Error Handling

- Invalid `quality_profile` fails config validation.
- Invalid env overrides should keep existing config error behavior.
- Invalid trope override path raises a clear loader error.
- Hard floor failure records status, residual issues, and decision event payloads.
- Recency gate should not fail generation because a draft item lacks recency metadata; it should skip unknown shapes.
- Pressure script should fail loudly on missing project, active-task conflict, or inability to write output files.
- Pressure script should report unavailable metric sources instead of inventing values.

## Backward Compatibility

- Default profile is `standard`.
- Existing projects keep current behavior unless runtime env changes.
- Existing trope seed JSON remains valid through defaulted fields.
- Existing reviewer hub constructor call sites continue working by defaulted new args.
- `context_recency_window_chapters=0` preserves current context assembly.
- `hard_floor_gate_enabled=False` preserves current chapter status flow.

## Implementation Order

1. Phase 1: config and explicit-key tracking.
2. Phase 2: hard floor checker and orchestrator integration.
3. Phase 3: reviewer bypass, `fatal_only`, BookState layers.
4. Phase 4: context recency gate.
5. Phase 5a: trope schema and markdown loader.
6. Phase 5b: selector cost and dedup.
7. Phase 5c: prompt injection.
8. Phase 6: pressure script and reports.
9. Documentation updates:
   - `Design-docs/CURRENT_ARCHITECTURE.md`
   - `Design-docs/DESIGN_STATUS.md`
   - `README.md`

Phase 5a and Phase 6 can be started after Phase 1 interfaces are stable, but final verification should run in the order above.

## Verification Plan

Focused tests:

```bash
pytest tests/test_quality_profile.py -q
pytest tests/test_hard_floor.py -q
pytest tests/test_pulp_pipeline_bypass.py -q
pytest tests/test_context_recency_truncation.py -q
pytest tests/test_trope_schema_compat.py tests/test_trope_md_loader.py -q
pytest tests/test_trope_selector.py tests/test_trope_prompt_injection.py -q
pytest tests/test_pulp_pressure_test.py -q
```

Regression tests should include existing config, reviewer, canon quality, context, experience, and writer prompt tests. At final integration, run the repository's full test suite.

Runtime verification:

1. Start a local runtime with `FORWIN_QUALITY_PROFILE=pulp`.
2. Confirm derived config and hub switches.
3. Run a short 2-3 chapter smoke before the 30-chapter pressure test.
4. Run the 30-chapter pressure script only after confirming no active generation task conflicts.
5. Review `summary.json` and `README.md` for threshold results and missing metric sources.

## Risks And Mitigations

- Explicit-key tracking can miss env aliases. Mitigation: test representative aliases and keep mapping close to existing `_env_values()` entries.
- `fatal_only` can accidentally block warnings. Mitigation: preserve source verdict/confidence/evidence fields and test warn/non-evidence cases.
- BookState delta layer filtering may lack stable metadata. Mitigation: inspect delta shapes first and add explicit helper tests before enabling filtering.
- Context truncation can remove critical old context. Mitigation: trim only timestamped data and rank current-plan/core entities first.
- Trope injection can exceed prompt budget. Mitigation: cap injected template text and test prompt budget behavior.
- Pressure metrics can be partially unavailable. Mitigation: record `null` plus missing-source notes rather than fake values.

## Completion Criteria

The implementation is complete when:

- All Phase 1-6 focused tests pass.
- Full pytest passes or any unrelated pre-existing failures are documented with evidence.
- `standard` behavior remains unchanged in focused regression tests.
- `pulp` derived config enables the low-cost path.
- Writer prompt contains loaded pulp trope instructions when the markdown library is configured.
- Pressure script produces `metrics.csv`, `summary.json`, and `README.md`.
- Documentation is updated to describe `FORWIN_QUALITY_PROFILE` and `FORWIN_TROPE_TEMPLATE_PATH`.
