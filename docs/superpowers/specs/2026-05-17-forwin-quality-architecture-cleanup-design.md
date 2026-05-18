# ForWin Quality And Architecture Cleanup Design

Date: 2026-05-17

Status: approved for planning

## Scope

Fix the reviewed quality and architecture problems except for giant-file phase splitting. This design covers story-specific hardcoding, prompt constraint sprawl, writer magic numbers, governance keyword duplication, false positive keyword matching (with documented limits), route dependency overload, config grouping, legacy/v4 compatibility cleanup with deprecation enforcement, plan-time pre-audit expansion from countdown/character-state to obligations and open signals, and prompt revision tracking with regression fixtures.

Structural overfitting that this work does not resolve is documented in the "Known Limitations And Deferred Work" section near the end so that the next architecture revision starts from the right premise.

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
- Add conservative negation-scope handling for common Chinese negations before trigger keywords, such as "避免", "不要", "不得", "不能", "防止", "禁止", and "阻止误写". This handles immediate-prefix negation only. Cross-clause negations (e.g. "避免把主角从被捕状态救出后又写成被关押") and split negations (e.g. "假设秘密已被揭露，但实际还未") are out of scope for Phase 4 and recorded under Known Limitations.

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
- Add a deprecation matrix to `Design-docs/DESIGN_STATUS.md` for `reviewer_v4`, `world_model_v4`, `world_v4_compat`, `world_v4_review_gate`, legacy `world_model`, and scenario rehearsal legacy/service names. Each matrix row records a target sunset version (e.g. "removed in v5.0"). The matrix is the single source of truth for when legacy/v4 surfaces leave the codebase.
- Emit `DeprecationWarning` from the top-level `__init__.py` of each module marked deprecated in the matrix. Warnings cite the matrix row and target sunset version. Suppress warnings inside legacy compatibility paths exercised by tests via fixture-scoped `warnings.simplefilter` or a module-level test allowlist so the deprecation signal does not become test noise.
- Add or preserve module docstrings and warnings that distinguish compatibility aliases from primary production paths.
- Enforce that new production imports prefer `world_v4_compat` and `world_v4_review_gate`; legacy aliases remain allowed for tests, migration, and debug APIs.

Acceptance:

- `ApiRouteDeps` is no longer a flat field list.
- Config has domain-level access while preserving old field compatibility.
- New production code cannot silently expand legacy alias usage.
- Design status documents the allowed and deprecated paths.
- Each deprecated module emits `DeprecationWarning` and the matrix records its target sunset version.

## Phase 6: Pre-Write Audit Expansion

Purpose: extend the existing `future_plan_auditor` plan-time patch mechanism to cover narrative obligations and residual quality signals, so that quality fixes mutate the plan handed to the writer rather than only stacking as negative constraints in the writer prompt.

Background: `forwin/planning/future_plan_auditor.py` already issues plan patches for countdown ledger conflicts and character-state conflicts; the patch rewrites plan text before the writer ever sees it. Obligations (`forwin/narrative_obligations/`) and open signals (`forwin/canon_quality/signals.py`) currently take the other route: their state is injected into `canon_quality_context` and surfaces as negative-list rules inside the writer prompt. That route accumulates constraint sprawl and depends on the writer self-policing.

Planned files:

- `forwin/planning/obligation_pre_audit.py` (new)
- `forwin/planning/signal_pre_audit.py` (new)
- `forwin/planning/future_plan_auditor.py` (extend dispatch to call the new auditors)
- `forwin/protocol/plan_patch.py` (extend patch kinds if needed)
- Tests for obligation pre-audit, signal pre-audit, and plan-patch vs prompt-constraint precedence

Changes:

- For each active obligation whose `deadline_chapter` equals the next chapter or earlier and whose `must_resolve_now=true`, emit a `PlanPatch` that adds an explicit task entry to the chapter plan describing the required payoff (uses the obligation `payoff_test` as guidance text). Do not also inject that obligation as a negative constraint in the writer prompt for the same chapter.
- For each high-severity open signal that has gone unaddressed for more than a configurable window (default 2 chapters), emit a `PlanPatch` that adds an explicit task to explain, resolve, or close the signal in the next chapter plan.
- When both a pre-audit patch and a writer prompt constraint would target the same underlying issue, the pre-audit patch wins and the prompt constraint is suppressed for that chapter. Add a regression test for this precedence rule.
- Keep the existing writer-prompt constraint injection as a fallback so projects without pre-audit configuration still see the constraints. The fallback path is explicitly marked as transitional in code comments and in Known Limitations.

Acceptance:

- Obligations with `must_resolve_now=true` on the next chapter produce a plan patch, not a duplicated prompt constraint.
- High-severity stale signals produce a plan patch when older than the configurable threshold.
- A chapter never receives duplicate fixes (one as plan task and one as prompt rule) for the same underlying issue.
- Existing future_plan_auditor tests remain green; new audit kinds have unit tests covering positive, negated, and already-resolved cases.

## Phase 7: Prompt Revision And Regression Tracking

Purpose: make prompt changes measurable. Phase 1 adds size budget logging, which catches one failure mode (prompt blowing past LLM attention). This phase adds version identity and prompt-assembly diffing so any edit to `prompts.py` produces a reviewable signal.

Planned files:

- `forwin/writer/prompts.py` (add revision hash computation at the prompt-builder boundary)
- `forwin/protocol/writer.py` (add `prompt_revision_hash: str` field to `WriterOutput`)
- `tests/fixtures/prompt_regression/` (new directory with about 10 fixed `ChapterContextPack` JSON fixtures covering: opening chapter, mid-arc, final chapter, with-obligations, with-stale-countdown, with-character-state-conflict, with-open-signals, multi-scene, single-scene, continue-generation)
- `tests/test_prompt_regression_samples.py` (new)
- `tests/test_prompt_revision_field.py` (new)

Changes:

- Compute a stable hash over the assembled writer prompt (system plus user messages, after `_apply_skill_layers`) and attach it to `WriterOutput.prompt_revision_hash`. The hash is informational; do not gate writing on it.
- Persist the hash alongside other generation metadata in `WriterOutput` so downstream observability and review tools can group outputs by prompt revision.
- Build the regression fixture suite. For each fixture, snapshot the assembled prompt text (not LLM output) and assert via golden-file diff. The intent is not pass/fail correctness; a failure requires the PR author to confirm the diff is intentional and update the snapshot, with the diff visible in code review.
- The regression suite must not call the LLM. It only verifies prompt assembly produced the expected text. This keeps the suite fast and deterministic.
- Optional follow-up not required for done criteria: a separate offline harness that takes the same fixtures, calls the LLM, and stores outputs for human comparison. Document the harness interface in the test file even if the implementation is deferred.

Acceptance:

- `WriterOutput.prompt_revision_hash` is present and reproducible for identical inputs.
- The regression suite covers at least the 10 fixture shapes listed above.
- A deliberate edit to `prompts.py` produces snapshot diffs that fail the suite until the snapshot is updated. The test file documents the update workflow.
- The suite runs in under 5 seconds with no LLM calls.

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
- Pre-write audit: obligation pre-audit, signal pre-audit, plan-patch vs prompt-constraint precedence
- Prompt revision: regression fixture suite snapshot diffs, revision hash determinism, hash field presence on `WriterOutput`
- Deprecation signal: `DeprecationWarning` emitted for matrix-listed legacy modules on import, suppressed in compatibility test fixtures

Final verification should run the focused suite first. If practical, run full `python3 -m pytest`. If full tests are blocked by environment or duration, record exactly which focused tests passed and why full verification was not completed.

## Risk Controls

- Dirty worktree risk: implementation must inspect `git status --short` and relevant diffs before editing already-modified files.
- Prompt regression risk: keep old compatibility output available while adding structured sections and budget logging.
- Fixture explosion risk: separate production banned terms from fixture/test terms.
- API break risk: keep `register_api_routes` public shape and endpoint paths stable.
- Config break risk: add domain accessors before removing or changing old flat fields. Do not remove old fields in this work.
- Legacy confusion risk: document and test allowed usages rather than deleting legacy modules.

## Known Limitations And Deferred Work

This work makes the most direct quality and architecture issues addressable, but intentionally leaves some structural overfitting in place to keep scope bounded. The following limitations are known and should be treated as the next major architecture iteration, not as defects of this design. They are documented here so the next revision starts from the right premise instead of rediscovering them.

### `CanonGlossary` schema is countdown-centric

`CountdownRuleProfile` is a first-class type in `CanonGlossary`; character states, obligations, and other invariants stay as string lists or live in separate ledger types. Several `CountdownRuleProfile` fields encode the current book's specific failure modes rather than genre-neutral semantics:

- `local_window_aliases` exists because the current book's main mechanic (`memory_reset`) collides with a secondary timer (`terminal_audit_window`). Genres without nested timers will not populate this field.
- `monotonic=True` assumes countdowns decrease. Cooldowns that reset, cultivation backsteps, and reversible-pressure mechanics do not fit cleanly.
- `closure_requires_evidence` and `resolution_phrases` encode the current final-chapter rules, which assume a single primary crisis is being closed.

For genres without countdown-centric stakes (slice-of-life, romance progression, mystery deduction sequences), the `canon_quality/` subsystem will offer thin coverage even after this work. New projects should not be assumed to need countdown profiles at all. Phase 2 should not add further countdown-specific fields beyond what is listed here; new failure modes from the current book belong in a project-level data file, not in the schema.

### `canon_quality/` is organized by state kind, not by generic invariant

The 16 modules under `canon_quality/` are split by the kind of state being tracked (countdown_ledger, character_state, obligations, signals, final_completion, etc.). Each new failure category has historically produced a new specialized module with its own ledger, validator, and prompt-template pieces. A more general architecture would expose a single `CanonInvariant` abstraction:

- An invariant is a cross-chapter fact whose value evolves over time and has consistency rules (monotonic / discrete state machine / set membership / inventory delta / deadline).
- Countdown is an invariant where value is integer minutes and the rule is monotonic-decreasing-or-reset-with-evidence.
- Character custody is an invariant where value is enum and the rule is bridged transitions.
- Obligation is an invariant where value is open/satisfied with a deadline.

Building this generic engine is out of scope here. The countdown-centric design is accepted as the intermediate step. Document this trade-off in `Design-docs/DESIGN_STATUS.md` so the next architecture revision starts with the invariant abstraction in mind rather than adding a fifth specialized ledger.

### Negation handling is prefix-only

Phase 4 negation-scope handling catches `避免|不要|不得|不能|防止|禁止|阻止误写` immediately preceding a trigger keyword. Two known classes are not covered:

- Cross-clause negation: "避免把主角从被捕状态救出后又写成被关押" — the negation governs a multi-clause structure where the trigger keyword appears in a clause the negation does not directly precede.
- Split negation: "假设秘密已被揭露，但实际还未" — the retraction lives in a later clause.

A correct fix requires either light syntactic parsing (jieba pos plus dependency cues) or LLM-tagged structured output ("did this clause actually happen?"). Both are deferred.

### Reactive constraint route remains as fallback

Phase 6 moves obligation and signal fixes into plan patches, but the writer-prompt constraint route still exists as a fallback for projects without pre-audit configuration. Long term, the goal is for plan patches to be the only route and the prompt constraints to be a deprecated transition mechanism. That deprecation is not scheduled here. When it is scheduled, the boundary test from Phase 1 should be extended to flag new constraint additions to the writer prompt that have no corresponding plan-patch producer.

### Strategic pivot to structured output projection is deferred

The most fundamental simplification — having the writer emit structured deltas alongside narrative text, and projecting those deltas to canon ledgers via code rather than scanning Chinese phrases — is out of scope. Steps in this design move incrementally toward it (structured countdown rules in prompt, pre-write plan patching for more invariant kinds), but the writer is still asked to produce free narrative text and the validators still parse Chinese phrases for state extraction. This pivot remains the recommended next major direction; combined with the generic `CanonInvariant` abstraction above, it would let the validator layer shrink rather than grow with each new book.

## Done Criteria

- New projects do not inherit current-book terms such as the existing memory-reset/audit/core-layer/archive-cleanup mechanisms unless configured in project glossary.
- Story-specific mechanism terms are blocked from production code by tests or allowed only in explicit compatibility fixtures.
- `_canon_quality_context_section()` is a coordinator, not a large branch-heavy rule block.
- `ChapterWriter` supports `WriterProfile`; old constructor/config usage remains compatible.
- Governance keywords have one source of truth, and common negated mentions do not hard-block.
- `ApiRouteDeps` is domain-grouped.
- Config exposes domain-level accessors or submodels while preserving flat field compatibility.
- Legacy/v4 aliases have a deprecation matrix with target sunset versions, runtime `DeprecationWarning` emission, and boundary tests.
- Obligations with `must_resolve_now=true` on the next chapter and stale high-severity signals are addressed via plan patches, with the writer-prompt constraint route suppressed for the same chapter.
- `WriterOutput.prompt_revision_hash` is populated and the prompt regression fixture suite is in place.
- Known Limitations are recorded in `Design-docs/DESIGN_STATUS.md` (countdown-centric schema, state-kind canon_quality organization, prefix-only negation, reactive-route fallback, deferred structured-output pivot) so the next architecture revision starts from the right premise.
- No legacy module deletion and no `orchestrator/loop.py` phase split are included.
