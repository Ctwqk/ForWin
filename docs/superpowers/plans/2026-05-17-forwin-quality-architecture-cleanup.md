# ForWin Quality Architecture Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved destructive cleanup phases from `docs/superpowers/specs/2026-05-17-forwin-quality-architecture-cleanup-design.md` without splitting `forwin/orchestrator/loop.py` or deleting legacy modules.

**Architecture:** Add guardrails first, then move story-specific writing rules to project-scoped metadata and profile-driven prompt assembly. Preserve BookState as canon while turning legacy/v4 modules into explicit compatibility surfaces with warnings and tests.

**Tech Stack:** Python 3, Pydantic models, SQLAlchemy-backed ForWin models, pytest.

---

## File Structure

- Create `forwin/writer/prompt_budget.py`: prompt character counting, budget warning helpers, stable prompt hash.
- Create `forwin/writer/profile.py`: `WriterProfile` model and normalization helper.
- Create `forwin/canon_quality/rule_profile.py`: `CountdownRuleProfile`, `CanonGlossary`, legacy profile fallback, and helpers used by prompts and validators.
- Create `forwin/governance_keywords.py`: single source of truth for governance trigger keywords and prefix negation checks.
- Create `forwin/planning/obligation_pre_audit.py`: must-resolve obligation plan patch producer.
- Create `forwin/planning/signal_pre_audit.py`: stale high-severity signal plan patch producer.
- Create prompt regression fixtures under `tests/fixtures/prompt_regression/`.
- Modify `forwin/governance.py`: expose `CanonGlossary` on project governance and re-export keyword helpers.
- Modify `forwin/context/assembler.py`: inject `canon_glossary`, countdown rule profiles, and suppression lists into `canon_quality_context`.
- Modify `forwin/writer/prompts.py`: use `ConstraintSection`, prompt budget helpers, glossary labels, plan-patch suppression, and prompt hash helpers.
- Modify `forwin/writer/chapter_writer.py`: accept `WriterProfile` and attach prompt revision hash to `WriterOutput`.
- Modify `forwin/protocol/writer.py`: add `prompt_revision_hash`.
- Modify `forwin/config.py`, `forwin/runtime_settings.py`, and `forwin/runtime/factories.py`: expose domain config accessors and writer profile compatibility.
- Modify `forwin/governance_checks.py`: import keywords and negation checks from the single registry.
- Modify `forwin/api_route_registry.py` and `forwin/api.py`: group route dependencies by domain while preserving `register_api_routes(app, deps=...)`.
- Modify legacy package `__init__.py` files and `Design-docs/DESIGN_STATUS.md`: add deprecation matrix, warnings, and known limitations.
- Modify tests for hardcoding, prompts, future plan audit, governance checks, config, route deps, architecture boundaries, and prompt revision.

## Task 1: Phase 1 Guardrails

**Files:**
- Create: `forwin/writer/prompt_budget.py`
- Modify: `tests/test_no_story_specific_hardcoding.py`
- Modify: `tests/test_architecture_boundaries.py`
- Create: `tests/test_prompt_budget.py`

- [ ] **Step 1: Add prompt budget helper**
  Create functions `prompt_message_chars(messages)`, `prompt_revision_hash(messages)`, and `prompt_budget_warning(messages, max_chars)` in `forwin/writer/prompt_budget.py`.

- [ ] **Step 2: Expand hardcoding guard**
  Add mechanism terms to production banned terms while allowing current fixture tests to keep story-specific examples.

- [ ] **Step 3: Add legacy import boundary guard**
  Extend architecture boundary tests with an allowlist for production files that may import `forwin.world_model_v4` or `forwin.reviewer_v4`.

- [ ] **Step 4: Add prompt budget test**
  Assert prompt character counts are deterministic and warning metadata appears only when over budget.

- [ ] **Step 5: Run Phase 1 tests**
  Run: `python3 -m pytest tests/test_no_story_specific_hardcoding.py tests/test_architecture_boundaries.py tests/test_prompt_budget.py -q`

- [ ] **Step 6: Compare Phase 1 to spec**
  Confirm the guardrails make hardcoding, prompt size, and legacy imports visible, and that the phase did not introduce new story-specific defaults.

## Task 2: Phase 2 Project-Scoped Writing Rules

**Files:**
- Create: `forwin/canon_quality/rule_profile.py`
- Modify: `forwin/governance.py`
- Modify: `forwin/context/assembler.py`
- Modify: `forwin/writer/prompts.py`
- Modify: `forwin/canon_quality/countdown_ledger.py`
- Modify: `forwin/canon_quality/final_completion.py`
- Modify tests for writer prompts, countdown ledger, final completion, and future plan audit.

- [ ] **Step 1: Add CanonGlossary models**
  Implement `CountdownRuleProfile` and `CanonGlossary` with legacy fallback helpers.

- [ ] **Step 2: Add project governance field**
  Add `canon_glossary: CanonGlossary = Field(default_factory=CanonGlossary)` to `ProjectGovernanceSettings`.

- [ ] **Step 3: Inject glossary into context**
  Add `canon_glossary` and `countdown_rule_profiles` to `canon_quality_context`.

- [ ] **Step 4: Update prompt labels**
  Replace global story-specific label lookup with profile-driven labels and generic fallback labels.

- [ ] **Step 5: Update countdown/final helpers**
  Route aliases, stale phrases, and resolution phrases through `CountdownRuleProfile`, keeping explicit legacy fallback for existing project contexts.

- [ ] **Step 6: Run Phase 2 tests**
  Run: `python3 -m pytest tests/test_writer_prompt_contract.py tests/test_countdown_ledger.py tests/test_final_completion_gate.py tests/test_future_plan_auditor.py -q`

- [ ] **Step 7: Compare Phase 2 to spec**
  Confirm new project contexts do not receive current-book labels unless glossary data supplies them, and confirm legacy fallback is explicit.

## Task 3: Phase 3 Prompt Constraint Pipeline

**Files:**
- Modify: `forwin/writer/prompts.py`
- Modify: `tests/test_writer_prompt_contract.py`
- Create or modify prompt budget tests.

- [ ] **Step 1: Add `ConstraintSection`**
  Add a frozen dataclass with `key`, `priority`, `must_inject`, `text`, and `max_chars`.

- [ ] **Step 2: Split section builders**
  Split final chapter, countdown, character state, open signal, future audit, and active obligation prompt construction into dedicated helpers.

- [ ] **Step 3: Add budget-aware coordinator**
  Make `_canon_quality_context_section()` collect sections, sort by priority, trim optional sections, and join text.

- [ ] **Step 4: Suppress pre-audit-covered prompt constraints**
  Read `suppressed_prompt_constraint_keys` from `canon_quality_context` and skip matching open signals/obligations.

- [ ] **Step 5: Run Phase 3 tests**
  Run: `python3 -m pytest tests/test_writer_prompt_contract.py tests/test_prompt_budget.py -q`

- [ ] **Step 6: Compare Phase 3 to spec**
  Confirm `_canon_quality_context_section()` is a coordinator and did not reintroduce story-specific key branches.

## Task 4: Phase 4 Writer Profile And Governance Semantics

**Files:**
- Create: `forwin/writer/profile.py`
- Create: `forwin/governance_keywords.py`
- Modify: `forwin/config.py`
- Modify: `forwin/runtime_settings.py`
- Modify: `forwin/runtime/factories.py`
- Modify: `forwin/writer/chapter_writer.py`
- Modify: `forwin/governance.py`
- Modify: `forwin/governance_checks.py`
- Add or modify tests.

- [ ] **Step 1: Add `WriterProfile`**
  Implement model normalization with legacy flat-field compatibility.

- [ ] **Step 2: Wire `ChapterWriter`**
  Let `ChapterWriter.__init__` accept `profile` and normalize old parameters into `self.profile`.

- [ ] **Step 3: Add config accessors**
  Add domain accessors for writer, llm, storage, publisher, observability, governance, and codex without removing old fields.

- [ ] **Step 4: Centralize governance keywords**
  Move keyword tuples and issue hints to a single registry and re-export through governance.

- [ ] **Step 5: Add prefix negation handling**
  Use the registry to prevent immediate-prefix negated trigger words from hard-blocking.

- [ ] **Step 6: Run Phase 4 tests**
  Run: `python3 -m pytest tests/test_config_defaults.py tests/test_config_env_resolution.py tests/test_env_llm_profiles.py tests/test_governance_review_and_checkpoint.py -q`

- [ ] **Step 7: Compare Phase 4 to spec**
  Confirm writer defaults are profile-backed and negation handling did not weaken positive trigger cases.

## Task 5: Phase 5 API, Config, And Compatibility Cleanup

**Files:**
- Modify: `forwin/api_route_registry.py`
- Modify: `forwin/api.py`
- Modify: `forwin/config.py`
- Modify: `Design-docs/DESIGN_STATUS.md`
- Modify legacy/v4 `__init__.py` files.
- Modify tests.

- [ ] **Step 1: Add domain dependency dataclasses**
  Split route deps into grouped dataclasses and keep top-level `ApiRouteDeps` as an aggregate.

- [ ] **Step 2: Update route registration**
  Replace flat local assignments with domain-grouped assignments while keeping endpoint registration unchanged.

- [ ] **Step 3: Add deprecation matrix**
  Add matrix rows with target sunset versions and known limitations to `Design-docs/DESIGN_STATUS.md`.

- [ ] **Step 4: Emit deprecation warnings**
  Add `DeprecationWarning` to deprecated package imports, with tests suppressing expected warnings where legacy aliases are intentionally imported.

- [ ] **Step 5: Run Phase 5 tests**
  Run: `python3 -m pytest tests/test_architecture_boundaries.py tests/test_world_v4_aliases.py tests/test_world_v4_api.py tests/test_config_defaults.py -q`

- [ ] **Step 6: Compare Phase 5 to spec**
  Confirm `ApiRouteDeps` is grouped, public endpoint behavior is unchanged, and deprecated imports are explicit.

## Task 6: Phase 6 Pre-Write Audit Expansion

**Files:**
- Create: `forwin/planning/obligation_pre_audit.py`
- Create: `forwin/planning/signal_pre_audit.py`
- Modify: `forwin/planning/future_plan_auditor.py`
- Modify: `forwin/writer/prompts.py`
- Add tests.

- [ ] **Step 1: Add obligation pre-auditor**
  Produce `NarrativePlanPatch` for active or planned obligations with `must_resolve_now=true` and deadline at or before target chapter.

- [ ] **Step 2: Add signal pre-auditor**
  Produce `NarrativePlanPatch` for stale high-severity open signals older than the configured threshold.

- [ ] **Step 3: Wire into future plan auditor**
  Dispatch both auditors after countdown/character-state audit and add suppression metadata to audit result.

- [ ] **Step 4: Suppress duplicate prompt constraints**
  Ensure prompt constraints are skipped when a plan patch already targets the same obligation or signal.

- [ ] **Step 5: Run Phase 6 tests**
  Run: `python3 -m pytest tests/test_future_plan_auditor.py tests/test_writer_prompt_contract.py -q`

- [ ] **Step 6: Compare Phase 6 to spec**
  Confirm obligations and stale high-severity signals patch the plan and do not duplicate writer prompt constraints.

## Task 7: Phase 7 Prompt Revision And Regression Tracking

**Files:**
- Modify: `forwin/writer/prompts.py`
- Modify: `forwin/writer/chapter_writer.py`
- Modify: `forwin/protocol/writer.py`
- Create: `tests/fixtures/prompt_regression/`
- Create: `tests/test_prompt_revision_field.py`
- Create: `tests/test_prompt_regression_samples.py`

- [ ] **Step 1: Add writer output hash field**
  Add `prompt_revision_hash: str = ""` to `WriterOutput`.

- [ ] **Step 2: Compute prompt hash**
  Hash assembled messages after skill layers and attach the hash to `WriterOutput.generation_meta` and `WriterOutput.prompt_revision_hash`.

- [ ] **Step 3: Add deterministic fixtures**
  Create 10 fixture contexts covering opening, mid-arc, final, obligations, stale countdown, character-state, open signals, multi-scene, single-scene, and continue-generation shapes.

- [ ] **Step 4: Add golden prompt regression test**
  Assert fixtures assemble to checked-in prompt snapshots and document update workflow.

- [ ] **Step 5: Run Phase 7 tests**
  Run: `python3 -m pytest tests/test_prompt_revision_field.py tests/test_prompt_regression_samples.py -q`

- [ ] **Step 6: Compare Phase 7 to spec**
  Confirm hash determinism and no-LLM prompt regression fixtures.

## Task 8: Final Verification

- [ ] **Step 1: Run focused suite**
  Run the union of all phase test commands.

- [ ] **Step 2: Run full suite if practical**
  Run: `python3 -m pytest -q`

- [ ] **Step 3: Final phase comparison**
  For every phase, record whether it solved the target problem and whether it reintroduced an issue solved by an earlier phase.

- [ ] **Step 4: Report changed files and verification evidence**
  Summarize exact test commands, pass/fail status, and any residual risk.
