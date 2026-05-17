# ForWin Quality And Architecture Cleanup Design

Date: 2026-05-17

Status: approved for planning

## Scope

Fix the reviewed quality and architecture problems except for giant-file phase splitting. This design covers story-specific hardcoding, prompt constraint sprawl, writer magic numbers, post-hoc quality repair, governance keyword duplication, false positive keyword matching, route dependency overload, config grouping, and legacy/v4 compatibility cleanup.

The work must preserve BookState as the only canon source. Obsidian, LLM KB, World Studio, Qdrant, legacy `world_model`, and world-v4 modules remain projections, compatibility layers, migration sources, or debug surfaces.

## Non-Goals

- Do not split `forwin/orchestrator/loop.py` by phase in this work.
- Do not delete legacy modules such as `world_model_v4`, `reviewer_v4`, `world_model`, or v4 debug APIs.
- Do not change public HTTP endpoint behavior while cleaning `ApiRouteDeps`.
- Do not remove existing environment variable compatibility from `Config`.
- Do not overwrite current uncommitted user changes. Implementation must inspect diffs before editing files that are already modified.

## Recommended Approach

Use phased compatibility cleanup. Establish guardrails and data structures first, migrate writing and canon-quality behavior to project-scoped metadata, then clean engineering boundaries. This gives each phase a narrow test gate and keeps current projects compatible.

Rejected alternatives:

- Quality-only cleanup would improve generation sooner but leave route/config/legacy ambiguity in place.
- Architecture-first cleanup would make the repo cleaner earlier but would not address the most direct causes of poor generated chapters.

## Phase 1: Baseline Guardrails

Purpose: make the problems visible and prevent further spread before changing behavior.

Planned files:

- `tests/test_no_story_specific_hardcoding.py`
- `tests/test_architecture_boundaries.py` or a new compatibility-boundary test
- `tests/test_prompt_budget.py`
- `forwin/writer/prompt_budget.py`

Changes:

- Expand hardcoding checks to include mechanism terms such as current-book countdown and system mechanism names.
- Split production banned terms from fixture/test-allowed terms so existing fixtures do not block the first cleanup step.
- Add prompt budget instrumentation at prompt-builder boundaries. A first pass can use character counts; tokenizer integration can come later.
- Add import-boundary tests so new production code does not directly import legacy aliases such as `forwin.world_model_v4` or `forwin.reviewer_v4` outside allowed compatibility/debug/migration surfaces.

Acceptance:

- Production hardcoding regressions are test-visible.
- Prompt size can be logged or asserted in focused tests.
- Legacy import boundaries have an allowlist.

## Phase 2: Project-Scoped Writing Rules

Purpose: remove current-book story mechanisms from global code paths.

Planned files:

- `forwin/governance.py`
- `forwin/context/assembler.py`
- `forwin/writer/prompts.py`
- `forwin/canon_quality/countdown_ledger.py`
- `forwin/canon_quality/final_completion.py`
- Relevant tests for writer prompt, countdown ledger, future plan audit, and final completion gate

New model:

```python
class CountdownRuleProfile(BaseModel):
    key: str
    label: str = ""
    aliases: list[str] = []
    local_window_aliases: list[str] = []
    forbidden_stale_phrases: list[str] = []
    resolution_phrases: list[str] = []
    closure_requires_evidence: bool = True
    monotonic: bool = True


class CanonGlossary(BaseModel):
    countdowns: dict[str, CountdownRuleProfile] = {}
    mechanism_terms: list[str] = []
    final_crisis_terms: list[str] = []
```

Changes:

- Add glossary/rule-profile fields to project governance or an equivalent project metadata surface.
- Inject countdown labels, aliases, stale phrases, and resolution phrases into `canon_quality_context`.
- Stop treating internal keys such as `memory_reset` as globally equivalent to a specific Chinese mechanism label.
- Keep a legacy fallback for existing projects that lack profiles, but mark it as compatibility behavior.
- Make final completion checks profile-driven rather than hardcoding current-book resolution phrases.

Acceptance:

- New projects do not receive current-book mechanism terms unless their glossary explicitly configures them.
- Existing projects without glossary data can still run through legacy fallback.

## Phase 3: Prompt Constraint Pipeline

Purpose: replace one large natural-language constraint block with prioritized, budget-aware sections.

Planned files:

- `forwin/writer/prompts.py`
- Prompt contract tests

New structure:

```python
@dataclass(frozen=True)
class ConstraintSection:
    key: str
    priority: int
    must_inject: bool
    text: str
    max_chars: int = 0
```

Changes:

- Split `_canon_quality_context_section()` into focused section builders:
  - final chapter constraint
  - countdown constraints
  - character state constraints
  - open residual signals
  - future plan audit summary
  - active narrative obligations
- Keep `_canon_quality_context_section()` as the coordinator that sorts, budgets, and joins sections.
- Convert countdown instructions from long negative lists into structured ledger rules: key, label, latest minutes, status, allowed bridge, forbidden aliases, and monotonicity.
- Only trim non-mandatory sections when over budget.

Acceptance:

- The main canon-quality prompt function no longer contains story-specific key branches.
- Final-chapter and blocking constraints remain non-trimmable.
- Prompt budget behavior is test-covered.

## Phase 4: Runtime Profiles And Governance Semantics

Purpose: move writing-quality knobs and constraint keywords behind typed APIs.

Planned files:

- `forwin/writer/profile.py`
- `forwin/config.py`
- `forwin/runtime_settings.py`
- `forwin/runtime/factories.py`
- `forwin/writer/chapter_writer.py`
- `forwin/governance.py`
- `forwin/governance_checks.py`

New model:

```python
class WriterProfile(BaseModel):
    temperature: float = 0.85
    max_tokens: int = 16384
    default_scene_count: int = 3
    max_scene_count: int = 4
    min_chapter_chars: int = 2500
    target_chapter_chars: int = 2800
    max_chapter_chars: int = 3200
    prompt_budget_chars: int = 12000
```

Changes:

- Add `WriterProfile` and let `ChapterWriter` accept `profile=`.
- Keep old constructor parameters and old config fields, but normalize them internally into `WriterProfile`.
- Allow runtime settings to persist or derive a writer profile without breaking existing `runtime_settings.json`.
- Centralize governance constraint keywords in `governance.py` or a single registry module.
- Make `governance_checks.py` call the registry instead of maintaining a second keyword set.
- Add conservative negation-scope handling for common Chinese negations before trigger keywords, such as "避免", "不要", "不得", "不能", "防止", "禁止", and "阻止误写".

Acceptance:

- Writer defaults are accessible through one profile object.
- Existing config/env/runtime settings still work.
- "不要写死角色" does not hard-trigger the same way as "角色死亡".
- Positive trigger cases still fire.

## Phase 5: API, Config, And Compatibility Cleanup

Purpose: reduce engineering coupling without changing public behavior.

Planned files:

- `forwin/api_route_registry.py`
- `forwin/api.py`
- `forwin/config.py`
- `Design-docs/DESIGN_STATUS.md`
- Legacy/v4 package `__init__.py` files and existing architecture-boundary tests

Changes:

- Split the 172-field flat `ApiRouteDeps` into domain dependency groups such as `CoreDeps`, `TaskDeps`, `ProjectDeps`, `GovernanceDeps`, `ObservabilityDeps`, `PublisherDeps`, and `WorldModelDeps`.
- Keep the top-level `register_api_routes(app, deps=...)` shape stable by letting `ApiRouteDeps` aggregate domain deps.
- Add typed config accessors or domain submodels such as writer, llm, storage, publisher, governance, observability, and codex. Keep legacy flat fields readable.
- Add a deprecation matrix to `Design-docs/DESIGN_STATUS.md` for `reviewer_v4`, `world_model_v4`, `world_v4_compat`, `world_v4_review_gate`, legacy `world_model`, and scenario rehearsal legacy/service names.
- Add or preserve module docstrings and warnings that distinguish compatibility aliases from primary production paths.
- Enforce that new production imports prefer `world_v4_compat` and `world_v4_review_gate`; legacy aliases remain allowed for tests, migration, and debug APIs.

Acceptance:

- `ApiRouteDeps` is no longer a flat field list.
- Config has domain-level access while preserving old field compatibility.
- New production code cannot silently expand legacy alias usage.
- Design status documents the allowed and deprecated paths.

## Compatibility Strategy

Existing project behavior must remain available through explicit legacy fallback. New project behavior must be project-scoped and data-driven.

Rules:

- If a project has `CanonGlossary` countdown profiles, all prompt labels, countdown aliases, stale phrases, and close signals come from the profile.
- If a project lacks profiles, legacy fallback can preserve old current-book behavior for compatibility, but tests should prevent that fallback from becoming the default for new projects.
- Legacy v4 modules stay importable. Their primary purpose is compatibility projection, migration, or debug/export support.
- BookState remains the canon source. Compatibility projection failures must not roll back accepted BookState canon.

## Test Plan

Run focused tests after each phase:

- Hardcoding: `tests/test_no_story_specific_hardcoding.py`
- Prompt contracts and budget: writer prompt tests plus new prompt budget tests
- Countdown and final gate: countdown ledger, canon quality service, final completion tests
- Plan-time audit: future plan auditor tests
- Governance: governance check tests with positive and negated trigger cases
- Config/runtime: config defaults, env resolution, runtime settings tests
- API registry: route registry tests or import smoke tests
- Architecture boundaries: legacy/v4 alias boundary tests

Final verification should run the focused suite first. If practical, run full `python3 -m pytest`. If full tests are blocked by environment or duration, record exactly which focused tests passed and why full verification was not completed.

## Risk Controls

- Dirty worktree risk: implementation must inspect `git status --short` and relevant diffs before editing already-modified files.
- Prompt regression risk: keep old compatibility output available while adding structured sections and budget logging.
- Fixture explosion risk: separate production banned terms from fixture/test terms.
- API break risk: keep `register_api_routes` public shape and endpoint paths stable.
- Config break risk: add domain accessors before removing or changing old flat fields. Do not remove old fields in this work.
- Legacy confusion risk: document and test allowed usages rather than deleting legacy modules.

## Done Criteria

- New projects do not inherit current-book terms such as the existing memory-reset/audit/core-layer/archive-cleanup mechanisms unless configured in project glossary.
- Story-specific mechanism terms are blocked from production code by tests or allowed only in explicit compatibility fixtures.
- `_canon_quality_context_section()` is a coordinator, not a large branch-heavy rule block.
- `ChapterWriter` supports `WriterProfile`; old constructor/config usage remains compatible.
- Governance keywords have one source of truth, and common negated mentions do not hard-block.
- `ApiRouteDeps` is domain-grouped.
- Config exposes domain-level accessors or submodels while preserving flat field compatibility.
- Legacy/v4 aliases have a deprecation matrix and boundary tests.
- No legacy module deletion and no `orchestrator/loop.py` phase split are included.
