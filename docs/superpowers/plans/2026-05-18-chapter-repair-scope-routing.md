# Chapter Repair Scope Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the full chapter-repair routing design so infrastructure, subworld, active-rule, band-contract, and prose failures route to the layer that can actually fix them.

**Architecture:** Keep BookState as canon and add small boundaries around repair routing instead of widening writer prompts. Signal kinds map to repair scopes through one audited Python table; metadata-only handlers run before writer scopes; infrastructure failures default to operator review. Each phase adds focused tests before implementation.

**Tech Stack:** Python 3, Pydantic, SQLAlchemy, pytest, existing ForWin canon-quality, BookState, subworld, planning, and orchestrator modules.

---

### Task 1: Form Coercion and Infrastructure Filtering

**Files:**
- Modify: `forwin/canon_quality/chapter_review_form/llm_caller.py`
- Create: `forwin/reviewer/infrastructure_errors.py`
- Modify: `forwin/reviewer/hub.py`
- Modify: `forwin/orchestrator/repair_coordinator.py`
- Test: `tests/test_form_coercion_dict_bool.py`
- Test: `tests/test_repair_prompt_filters_infrastructure_errors.py`

- [ ] Add tests that `_normalize_answer_payload()` converts nested answer dict values: `{"value": False}` to `"false"`, `True` to `"true"`, `None` to `""`, and integers to strings.
- [ ] Add tests proving a `form_schema_invalid` / Pydantic validation issue is not copied into writer-facing `RepairInstruction.must_fix`.
- [ ] In `_coerce_form_answer()`, normalize `answer["value"]` through `_scalar_answer_value()` whenever a dict contains `value`.
- [ ] Add `is_infrastructure_issue()` and `filter_writer_fixable_issues()` in `forwin/reviewer/infrastructure_errors.py`.
- [ ] Use the filter in `HistoricalReviewHub._continuity_repair_instruction()` and `_governance_repair_instruction()`.
- [ ] Add an operator review path in `ChapterRepairCoordinator` for infrastructure issues before starting a writer rewrite.
- [ ] Run `python3 -m pytest tests/test_form_coercion_dict_bool.py tests/test_repair_prompt_filters_infrastructure_errors.py -q`.

### Task 2: Signal Kind Routing Table and Verdict Reconciliation

**Files:**
- Modify: `forwin/canon_quality/signals.py`
- Create: `forwin/reviewer/repair_scope_router.py`
- Modify: `forwin/protocol/review.py`
- Modify: `forwin/reviewer/outcome.py`
- Modify: `forwin/canon_quality/gate.py`
- Modify: `forwin/orchestrator/repair_coordinator.py`
- Test: `tests/test_repair_scope_router_dispatch.py`
- Test: `tests/test_signal_kind_routing_exhaustive.py`
- Test: `tests/test_verdict_reconciliation.py`

- [ ] Add `SignalKind` and `RepairScopeKind` enums with all currently known signal kinds.
- [ ] Add `SIGNAL_KIND_TO_SCOPE` as a hard-coded table; unknown kinds route to `operator`.
- [ ] Add router tests for infra+draft mix, subworld+draft mix, active_rules routing, and unknown signal fallback.
- [ ] Extend repair scope normalization to accept `subworld`, `active_rules`, and `operator`.
- [ ] Use the new router in `ReviewOutcomeRouter` / `ChapterRepairCoordinator` before the existing linear escalation.
- [ ] Update gate verdict logic so blocking requires at least one open error signal, form blocking ref, LLM fail verdict, or obligation blocker.
- [ ] Run `python3 -m pytest tests/test_repair_scope_router_dispatch.py tests/test_signal_kind_routing_exhaustive.py tests/test_verdict_reconciliation.py tests/test_repair_scope_router.py -q`.

### Task 3: Subworld Admission Auto-Population

**Files:**
- Create: `forwin/planning/subworld_admission.py`
- Modify: `forwin/state/repo.py`
- Modify: `forwin/subworld_manager.py`
- Modify: `forwin/planning/band_plan_service.py`
- Create: `forwin/reviewer/repair_handlers/subworld.py`
- Test: `tests/test_subworld_admission_auto_population.py`

- [ ] Add `EntityKind` enum with `person`, `organization`, `location`, `item`, `code`, `concept`, and `placeholder`.
- [ ] Add kind normalization from legacy `character`, `named_person`, `archive_code`, and `system_id`.
- [ ] Build admission from active roster plus accepted canon entities from the last five accepted chapters.
- [ ] Auto-carry canon people, organizations, locations, items, and concepts with `auto_carried=True`.
- [ ] Admit project-configured code patterns such as `^PS-\d+$`; keep placeholders chapter-local.
- [ ] Add subworld repair handler that applies missing-canon-entity admissions without invoking writer.
- [ ] Run `python3 -m pytest tests/test_subworld_admission_auto_population.py -q`.

### Task 4: BookState Query Interface and ActiveRuleStore

**Files:**
- Create: `forwin/book_state/query_interface.py`
- Create: `forwin/canon_quality/active_rule_store.py`
- Create: `forwin/canon_quality/active_rules_handler.py`
- Modify: `forwin/planning/countdown_drift_pre_audit.py`
- Create: `forwin/reviewer/repair_handlers/active_rules.py`
- Test: `tests/test_bookstate_query_interface.py`
- Test: `tests/test_active_rule_store.py`
- Test: `tests/test_countdown_live_state_source.py`
- Test: `tests/test_active_rules_auto_registration.py`

- [ ] Define `CountdownState`, `ActiveRule`, `TriggerQuote`, `ActiveRulePatch`, and result models in the new boundary modules.
- [ ] Implement `SqlBookStateQueryInterface` against BookState projection first, with countdown-ledger fallback only inside the interface.
- [ ] Implement `CanonQualityActiveRuleStore` using existing canon-quality signal rows as the canonical persistence location for active-rule events.
- [ ] Make countdown drift target selection optionally read current countdown values from `BookStateQueryInterface`.
- [ ] Add active-rules handler that applies validated `ActiveRulePatch` records through `ActiveRuleStore`.
- [ ] Add architecture test forbidding direct repair-time `world_model` canonical reads outside `book_state/query_interface.py`.
- [ ] Run `python3 -m pytest tests/test_bookstate_query_interface.py tests/test_active_rule_store.py tests/test_countdown_live_state_source.py tests/test_active_rules_auto_registration.py -q`.

### Task 5: Band Role Classification and Contract Templates

**Files:**
- Create: `forwin/planning/band_plan/band_role.py`
- Create: `forwin/planning/band_plan/contract_templates.py`
- Modify: `forwin/planning/band_plan_service.py`
- Modify: `forwin/planning/future_plan_audit/obligations.py`
- Test: `tests/test_band_role_classification.py`
- Test: `tests/test_band_contract_template_selection.py`

- [ ] Add `BandRole.opening`, `BandRole.mid_arc`, and `BandRole.final`.
- [ ] Classify final only when `last_chapter_of_band == target_total_chapters`; otherwise non-opening bands are `mid_arc`.
- [ ] Add opening, mid-arc, and final contract templates.
- [ ] Store band role and contract in schedule metadata without retroactively rewriting existing band rows.
- [ ] Make obligation/future-plan audit skip final-closure checks for mid-arc bands.
- [ ] Run `python3 -m pytest tests/test_band_role_classification.py tests/test_band_contract_template_selection.py -q`.

### Task 6: Repair Loop Detection and Operator Report

**Files:**
- Create: `forwin/reviewer/repair_loop_detector.py`
- Create: `forwin/canon_quality/chapter_review_form/operator_report.py`
- Modify: `forwin/orchestrator/repair_coordinator.py`
- Test: `tests/test_repair_loop_detection.py`

- [ ] Track per-attempt scope, signal kind, subject key, and verdict.
- [ ] Detect repeated same-scope signal sets with Jaccard similarity above `0.7`.
- [ ] Build operator report with latest signals, history, suspected root cause, suggested actions, and artifact links.
- [ ] Route repeated same-scope failure to `needs_operator_review` on the second matching attempt.
- [ ] Run `python3 -m pytest tests/test_repair_loop_detection.py -q`.

### Task 7: End-to-End Regression and Hygiene

**Files:**
- Create: `tests/fixtures/repair_routing/chapter_18_fail_loop/`
- Test: `tests/test_chapter18_repair_routing_regression.py`
- Modify: any failing focused tests discovered during integration.

- [ ] Add a chapter-18 fixture with schema invalid, countdown inconsistency, and missing canon entity signals.
- [ ] Assert infra routes to operator, subworld routes to metadata repair, active rules routes to active-rule handler, and draft writer is skipped until only writer-fixable issues remain.
- [ ] Run all focused phase tests together.
- [ ] Run `python3 -m compileall -q forwin`.
- [ ] Run `git diff --check`.
