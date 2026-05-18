# Chapter Review Form Gap Closure Design

Date: 2026-05-18

Status: approved for implementation planning

## Scope

Close the gaps remaining after the destructive form replacement landed on branch `codex/chapter-review-form-replacement`. The replacement correctly deletes deterministic keyword analyzers and per-analyzer `prompt_json/` modules and puts the chapter review form on the primary canon-quality path, but several edge-case and rollout concerns remain. This document specifies the follow-up work needed to make the form path production-ready.

The work covers five categories:

- Edge cases that make the form reject legitimate answers or accept the wrong ones
- Legacy canon data left behind by the deleted deterministic era
- Safety net for production rollback when the LLM diverges from prior canon
- Regression fixture coverage matching what the replacement design called for
- Closing the loop so detected drift mutates the next chapter's plan rather than only blocking the current chapter

## Goals

- The form does not reject legitimate LLM answers because of cosmetic differences between LLM-quoted text and chapter source (punctuation form, whitespace).
- The form does not reject legitimate answers because the LLM used a descriptive reference or pronoun that the chapter naturally uses, as long as the reference unambiguously identifies the tracked entity.
- Budget pruning preserves high-priority entities (must-resolve obligations, active countdowns, error-severity signals) even when the form must shrink to fit the token budget.
- Blocking severity is configurable per verdict category, not always `error`.
- Old canon rows produced by the deleted deterministic analyzers no longer feed the form as ground truth.
- A non-blocking rollout mode exists so an operator can validate form judgments against current production behavior before flipping primary on.
- The fixture suite covers the regression categories the replacement design enumerated, generalized so they do not encode any single story's mechanics.
- Drift detected by the form propagates as a plan patch for the next chapter; the writer prompt's negative-constraint section shrinks correspondingly.

## Non-Goals

- Do not reintroduce deterministic keyword analyzers, per-analyzer `prompt_json/` modules, or any compatibility shell for them.
- Do not change the form schema's section structure. Additive fields are allowed; renaming or restructuring sections is out of scope.
- Do not change the form's primary-mode default once the safety-net phase is in place.
- Do not extend the validator into general-purpose Chinese syntactic analysis.
- Do not couple the form schema to any specific project's mechanism vocabulary.

## Context

The form replacement implementation:

- Replaces ~8000 lines of deterministic + per-analyzer code with ~3500 lines of form subsystem.
- Validates evidence quotes via exact-substring match after whitespace normalization.
- Enforces `subject_of_quote` equality with the entity being judged.
- Rejects pronoun subjects.
- Defaults to `primary` mode; `off` is the only escape.
- Blocks the chapter with a `form_llm_unavailable` signal when the LLM client is missing or fails.

Gaps observed during review:

- Quote normalization only strips whitespace; punctuation variants are not equivalent.
- Subject matching only accepts the canonical name or pre-declared aliases; descriptive references that the chapter itself uses are rejected.
- Pronoun rejection is unconditional even when the quote and surrounding sentence make the referent unambiguous.
- `_fit_budget` pops from the end of each section without priority awareness; only `must_track` characters are protected.
- All blocking verdicts emit `severity="error"`; there is no per-category configuration.
- `form_builder` reads prior state from existing transition rows; rows produced by the deleted deterministic analyzers still influence the form's "prior" baseline.
- Shadow and hybrid modes were skipped; the only rollback is to disable canon quality entirely.
- The fixture suite covers basic happy path and one rejection case. The replacement design called for ~15 fixtures including multi-chapter consistency, resurrection bridges, silent regression, and final chapter closure.
- The pre-write plan patcher does not consume form drift signals, so detected inconsistency blocks the current chapter but does not steer the next chapter's plan.

## Phases

### Phase 1: Validator Edge-Case Hardening

Purpose: stop the validator from rejecting answers that are factually correct but cosmetically different from the chapter text, and stop it from accepting answers that are over-blocking.

Files affected:

- `forwin/canon_quality/chapter_review_form/evidence_validator.py`
- `forwin/canon_quality/chapter_review_form/llm_caller.py`
- `forwin/canon_quality/chapter_review_form/canon_projector.py`
- `forwin/canon_quality/chapter_review_form/form_schema.py` (additive only)
- `forwin/config.py` (additive settings)
- Tests: `tests/test_form_validators.py`, `tests/test_form_llm_caller.py`

Steps:

1. **Quote normalization equivalence table.** Replace `_normalize_text` so it lowercases ASCII, strips whitespace, and translates a fixed punctuation equivalence table covering: fullwidth/halfwidth quotes, parentheses, brackets, commas, periods, exclamation, question, semicolon, colon, dash, ellipsis. Apply the same normalization to both chapter text and the LLM quote before substring check. Document the table inline.

2. **LLM canonical-name instruction.** Extend `SYSTEM_PROMPT` in `llm_caller.py` so the model is told: when the quote uses a descriptive reference, pronoun, role title, or other indirect reference to a tracked entity, `subject_of_quote` must still resolve to the entity's canonical name from the form's `name` field (or one of its `aliases`). Provide one in-prompt example. Do not change validator behavior in this step; the burden is on the LLM to comply.

3. **Pronoun acceptance via alias auto-expansion.** When `form_builder` builds a `CharacterReviewAsk`, allow callers to pass `descriptive_aliases` alongside `aliases`. Validator behavior unchanged: subject must match `aliases ∪ descriptive_aliases ∪ {name}`. Auto-population of `descriptive_aliases` is out of scope here; this step only opens the field so future callers can populate it.

4. **Configurable blocking severity.** Add a config block:

   ```python
   class FormBlockingPolicy:
       character_dead: Literal["error", "warning"] = "error"
       character_wounded: Literal["error", "warning"] = "warning"
       character_captured: Literal["error", "warning"] = "error"
       countdown_inconsistent: Literal["error", "warning"] = "error"
       countdown_reset: Literal["error", "warning"] = "warning"
       countdown_advanced: Literal["error", "warning"] = "warning"
       obligation_unaddressed: Literal["error", "warning"] = "error"
       obligation_partial: Literal["error", "warning"] = "warning"
       signal_persisting: Literal["error", "warning"] = "error"
       signal_worsened: Literal["error", "warning"] = "error"
       final_dangling: Literal["error", "warning"] = "error"
       final_denied: Literal["error", "warning"] = "error"
   ```

   `canon_projector` consults this policy when emitting `_blocking_signal` instead of hardcoding `error`. Default values reproduce current behavior.

5. **Reject envelope-only answers that claim binding values without evidence.** Validator already requires `is_bindable`, but `canon_projector` should also drop transitions where `evidence_quote` is empty even if the path is in `validated`. Add a regression test covering this combination.

6. **Quote-not-found rejection should preserve the answer for diagnostics.** When `RejectedAnswer` is emitted, include `value` and `confidence` in the message so operators can see what the LLM tried to claim. Currently only `reason` and `message` are kept.

Acceptance:

- Quote with fullwidth quotation marks matches chapter text in halfwidth, and vice versa.
- LLM prompt explicitly instructs canonical-name resolution; integration test asserts the instruction text appears in the assembled system message.
- `descriptive_aliases` field exists on `CharacterReviewAsk` and is honored by validator.
- Blocking severity per verdict category is configurable through `FormBlockingPolicy`.
- A binding answer with empty `evidence_quote` is rejected even when the path is in `validated`.
- Rejection diagnostics include the rejected `value` and `confidence`.

---

### Phase 2: Legacy Canon Data Migration

Purpose: stop the form from inheriting bad prior state from deleted deterministic analyzers.

Files affected:

- New: `scripts/migrate_legacy_canon_to_form.py`
- `forwin/canon_quality/repository.py`
- `forwin/canon_quality/chapter_review_form/form_builder.py`
- Tests: `tests/test_legacy_canon_supersede.py`

Steps:

1. **Mark non-form transitions as superseded.** Add a column or payload flag on `CharacterStateTransitionRow` and `CountdownLedgerEntryRow` (or use a payload key like `"superseded_by": "chapter_review_form_migration"`). Default: rows produced by the form already mark themselves with `payload.source == "chapter_review_form"`. Rows without that marker are eligible for supersede.

2. **Migration script.** Iterate over each project's transition and countdown rows. For any row where `payload.source != "chapter_review_form"`, mark superseded. Do not delete; preserve for audit. Record before/after counts.

3. **`form_builder` ignores superseded rows.** When listing prior state, exclude superseded rows. If a tracked character has no non-superseded transition row, treat `prior_life_state` as `"unknown"` and `prior_custody_state` as `"unknown"`. This forces the form to re-establish state from scratch through chapter review.

4. **Repository helper:** `list_character_transitions(... include_superseded=False)` defaults to excluding. Other callers can opt in for audit.

5. **One-pass re-review optional path.** Provide a script flag `--rebuild-from-chapter N` that, for each chapter from N onward, re-runs `analyze_writer_output_quality` so the form re-establishes state. This is destructive on canon and should require explicit confirmation.

Acceptance:

- After migration, `list_character_transitions` returns only form-sourced rows by default.
- Form built for any chapter shows `prior_life_state = "unknown"` for characters whose only history is from deleted analyzers.
- Migration script has a dry-run mode that prints affected row counts without writing.
- `--rebuild-from-chapter` regenerates form-sourced canon for the specified range.

---

### Phase 3: Safety Net For Rollback

Purpose: provide a non-blocking diagnostic mode so operators can compare form verdicts against the previous chapter's canon state before flipping primary on, and a way to inspect form rejections in the running system.

Files affected:

- `forwin/canon_quality/service.py`
- `forwin/canon_quality/chapter_review_form/service.py`
- `forwin/config.py`
- New: `forwin/canon_quality/chapter_review_form/comparison_report.py`
- Tests: `tests/test_chapter_review_form_dry_run.py`

Steps:

1. **Add `dry_run` mode.** Extend `_normalize_form_mode` to recognize `"dry_run"` in addition to `"off"` and `"primary"`. In dry-run mode, the pipeline runs the full form path (build, call, validate, project) but the projection result emits all signals with `severity="warning"` regardless of `FormBlockingPolicy`, and `blocking` is forced to `False`. Canon writes are skipped.

2. **Persist form outputs in dry-run.** Even though canon writes are skipped, persist the raw answer JSON, validation report, and projection to a dedicated artifact location (existing `artifact_ledger.py` or new JSON file under `data/artifacts/chapter_review_form/<project>/<chapter>.json`). This enables retrospective inspection.

3. **Comparison report helper.** `comparison_report.summarize_form_run(answers, validation_report, projection) -> dict` returns a structured summary: counts of validated vs rejected per section, blocking-eligible verdicts, top rejected paths with reasons. Used in dry-run logs and in primary-mode debug endpoints.

4. **Failure-mode unbypassable signal.** When mode is `dry_run` or `primary` and the LLM is unavailable, still emit `form_llm_unavailable` but route it through the existing residual-signal channel so the writer knows the chapter is unverified. In `dry_run`, this is a warning; in `primary`, an error.

5. **Inspection endpoint.** Add a read-only API endpoint or CLI command that, given `(project_id, chapter_number)`, returns the persisted form artifact. Useful for operator validation without re-running the LLM.

Acceptance:

- Setting `chapter_review_form_mode="dry_run"` runs the full form path but never blocks generation and never writes canon transitions.
- Artifacts for each dry-run chapter are queryable.
- Comparison report counts are exposed in observability logs.
- LLM-unavailable in dry-run downgrades to warning, not error.
- Inspection endpoint returns the persisted artifact for a given chapter.

---

### Phase 4: Pruning Priority And Budget Robustness

Purpose: when the form must shrink to fit the token budget, drop low-priority items, not high-priority ones.

Files affected:

- `forwin/canon_quality/chapter_review_form/pruning.py`
- `forwin/canon_quality/chapter_review_form/form_builder.py`
- Tests: `tests/test_form_builder.py`

Steps:

1. **Define priority orders.** Add module-level constants:

   ```python
   SIGNAL_SEVERITY_ORDER = {"blocker": 0, "critical": 1, "error": 2, "warning": 3, "info": 4}
   COUNTDOWN_STATUS_PRIORITY = {"reopened": 0, "active": 1, "paused": 2, "warning": 3, "conflict": 4, "fulfilled": 5, "closed": 6, "resolved": 7, "consistent": 8}
   ```

2. **Sort before truncation.** In `_fit_budget`, sort each list by priority before popping from the end:

   - `open_signals` by `SIGNAL_SEVERITY_ORDER[severity]` ascending (lowest priority first at end, so pops drop lowest)
   - `obligations` by `(not must_resolve_now, -deadline_chapter)` ascending
   - `countdowns` by `COUNTDOWN_STATUS_PRIORITY[prior_status]` ascending
   - `characters` already protected by `must_track`; secondary sort by recency descending

3. **Hard-protect critical items.** Even when over budget, never pop an `open_signal` with severity in `{"blocker", "critical", "error"}`, an `obligation` with `must_resolve_now=True`, or a `countdown` with `prior_status in {"reopened", "active"}`. If protected items alone exceed budget, raise `FormBudgetExceeded` with a structured payload listing what could not be dropped.

4. **Budget exceeded fallback.** When `FormBudgetExceeded` is raised, the service emits a `form_budget_exceeded` signal (severity `warning`), proceeds with the unfittable form, and lets the LLM caller handle the oversized payload. This is preferable to silently dropping critical items.

5. **Observability.** Each pruning pass logs the count of items dropped per section so operators can see when budget pressure is real.

Acceptance:

- A form with mixed-severity signals drops the lowest-severity ones first.
- An obligation with `must_resolve_now=True` is never dropped.
- A countdown with `prior_status="active"` is never dropped.
- When critical items alone exceed budget, the form is built anyway and a `form_budget_exceeded` signal is emitted.
- Pruning counts appear in logs.

---

### Phase 5: Fixture Suite Completion

Purpose: lock in correct behavior for the regression categories the replacement design enumerated. Each fixture must be generalized so it does not depend on any one project's mechanism vocabulary.

Files affected:

- New directory: `tests/fixtures/chapter_review_form/`
- New: `tests/test_chapter_review_form_regression_suite.py`

Each fixture is a directory containing:
- `form.json`: prior canon state in form-builder input format
- `chapter.txt`: synthetic chapter body in Chinese
- `expected_answers.json`: the LLM answer the test fakes
- `expected_signals.json`: signals the projector must emit
- `expected_transitions.json`: canon transitions the projector must produce
- `notes.md`: one-paragraph description of what the fixture tests

Steps (each step adds one fixture; all under generic placeholder names like `角色A`, `角色B`, `倒计时甲`, `义务-1`):

1. **`subject_attribution_misdirection`**: chapter mentions tracked character A in the same sentence as a death keyword applied to a different entity (e.g. "A 与某组织合作导致一些无名成员死亡"). Expected: form answer claims A is dead with `subject_of_quote` set to the wrong subject; projector rejects; no `dead` transition.

2. **`cross_chapter_countdown_regression`**: prior canon has countdown 甲 with `prior_value_minutes=0, prior_status="closed"` from a chapter several chapters back; current chapter body explicitly mentions countdown 甲 with a positive remaining time. Expected: form answer flags `consistent_with_prior=false`, `inconsistency_kind="reopened_after_close"`; projector emits `form_countdown_inconsistency` signal with blocking severity.

3. **`already_dead_character_resurrected_with_bridge`**: prior canon has character A with `prior_life_state="dead"`; chapter explicitly narrates a bridging event that restores A (e.g. clone activation, prior death was misreported). Expected: form answer reports bridge event with quote; projector emits a `resurrection_or_correction` bridge transition; A's life state changes to `alive`.

4. **`already_dead_character_mentioned_without_resurrection`**: prior canon has character A dead; chapter mentions A's prior actions in past tense or as memory. Expected: form answer reports `appears_in_chapter=true, participation="mentioned_only", life_state="dead"`; no state change.

5. **`final_chapter_main_crisis_closed`**: chapter is `target_total_chapters`; chapter body explicitly closes the main crisis with evidence; no new task is left dangling. Expected: form answer for `final_chapter.main_crisis_status="closed_with_evidence"`; no `form_final_chapter_unresolved` signal.

6. **`final_chapter_main_crisis_dangling`**: chapter is `target_total_chapters`; chapter ends with the main crisis still unresolved. Expected: blocking `form_final_chapter_unresolved` signal.

7. **`obligation_silently_skipped`**: obligation with `must_resolve_now=True, deadline_chapter=current`; chapter body does not address it. Expected: form answer `addressed="unaddressed"`; blocking `form_obligation_unresolved`.

8. **`obligation_partial_default_warning`**: same obligation; chapter partially addresses it (e.g. one step of a multi-step payoff). Expected: form answer `addressed="partial"`; with default `FormBlockingPolicy`, this emits warning; with `obligation_partial="error"`, this blocks. Test both.

9. **`open_signal_resolved_with_evidence`**: prior canon has open signal S; chapter body explains and resolves it. Expected: form answer `status="resolved"` with evidence; no blocking signal; old signal marked resolved in projection.

10. **`open_signal_persisting_high_severity`**: prior canon has open signal S with `severity="error"`, `age_chapters >= 3`; chapter body does not address it. Expected: form answer `status="persisting"`; blocking `form_open_signal_persisting`.

11. **`pruning_drops_long_dead_minor_character`**: form input includes 30 tracked characters, one is a long-dead minor with no mention in current chapter. Budget set so pruning is required. Expected: dead minor character is dropped; all `must_track=True` characters are retained.

12. **`pruning_protects_active_countdown_under_pressure`**: form input includes one active countdown and many low-priority signals; budget tight. Expected: countdown survives; signals are pruned first.

13. **`quote_punctuation_form_difference`** (Phase 1 verification): LLM quote uses fullwidth punctuation; chapter source uses halfwidth equivalent. Expected: validator accepts after normalization.

14. **`subject_descriptive_reference`** (Phase 1 verification): chapter refers to character A as "那个穿白衣的人"; LLM answer sets `subject_of_quote="角色A"`. Expected: validator accepts because canonical name matches.

15. **`budget_exceeded_emits_warning`** (Phase 4 verification): form built with critical items totaling more than `token_budget_chars`. Expected: `form_budget_exceeded` warning signal; form proceeds.

Each fixture uses generic names (`角色A`, `角色B`, `倒计时甲`, `事件-1`, `章节十`, etc.). No fixture may reference any real project's mechanism vocabulary. The architecture-boundary test should be extended to scan fixtures for project-specific terms.

Acceptance:

- All 15 fixtures exist and pass.
- The regression test runs them in under 10 seconds (no real LLM calls; fake client returns the `expected_answers.json` content).
- Architecture boundary test scans `tests/fixtures/chapter_review_form/` for banned mechanism terms and fails if any appear.
- Each fixture's `notes.md` is non-empty and describes the regression category.

---

### Phase 6: Plan-Patcher Loop Closure

Purpose: turn form drift signals into next-chapter plan patches so the same drift does not recur. Eliminate the reactive negative-list constraints in the writer prompt that the pre-audit can now absorb.

Files affected:

- `forwin/planning/future_plan_audit/auditor.py`
- `forwin/planning/obligation_pre_audit.py`
- `forwin/planning/signal_pre_audit.py`
- New: `forwin/planning/countdown_drift_pre_audit.py`
- `forwin/writer/prompts.py`
- `forwin/canon_quality/chapter_review_form/canon_projector.py`
- Tests: `tests/test_plan_patcher_loop_closure.py`

Steps:

1. **Tag form-emitted signals as plan-patchable.** When `canon_projector` emits a blocking signal, include `payload.plan_patchable=True` and `payload.patch_kind in {"countdown_drift", "obligation_unresolved", "signal_persisting", "final_dangling"}`. This is metadata only; existing consumers are unaffected.

2. **Countdown drift pre-auditor.** When the future plan auditor runs for chapter N, it queries open signals from chapter N-1. For any signal where `signal_type=="form_countdown_inconsistency"` and `payload.plan_patchable=True`, it emits a plan patch on chapter N's plan that adds an explicit task: "本章必须明确处理 [countdown_label] 的当前状态：[prior_value_minutes] 分钟。如继续，必须 ≤ 该值；如已 closed，不得再次出现正数剩余时间；如确实重新开启，必须显式写出 reopen 事件并命名为新的局部窗口。" The patch records a `suppression_key=f"countdown:{countdown_key}"`.

3. **Signal pre-auditor extension.** Extend `select_stale_signal_targets` to also handle `form_open_signal_persisting` signals. The signal's `description` and `subject_key` become the plan-patch task body.

4. **Obligation pre-auditor extension.** Same pattern: `form_obligation_unresolved` signals feed `select_urgent_obligation_targets` as if they were active obligations with `must_resolve_now=True`.

5. **Writer-prompt constraint section shrink.** In `_canon_quality_context_section`, when a signal has `payload.plan_patchable=True` and the corresponding `suppression_key` appears in the chapter's plan-patch suppression list, omit the corresponding negative-list constraint. The writer no longer needs to be told "don't write the wrong countdown value" if the plan already has an explicit positive task.

6. **Loop closure observability.** Add a metric (or structured log line) per chapter: count of form-derived signals consumed by the plan patcher, count suppressed from the writer prompt, count of remaining writer-prompt constraints. Expected: as the loop closes, the third count drops.

Acceptance:

- A `form_countdown_inconsistency` signal emitted at chapter N causes chapter N+1's plan to receive a `countdown_drift` patch that names the countdown and the binding state.
- The writer prompt for chapter N+1 omits the negative constraint about that countdown because the plan patch already covers it.
- A regression test demonstrates: chapter N drifts → form blocks → chapter N+1 plan is patched → writer for N+1 receives the patched plan, not the negative constraint → chapter N+1 is generated without the drift recurring.
- Average `_canon_quality_context_section` token count on the existing prompt-regression fixtures drops by at least 30% relative to pre-loop-closure baseline.

---

## Test Plan

Run after each phase:

- Phase 1: `pytest tests/test_form_validators.py tests/test_form_llm_caller.py tests/test_canon_projector.py -q`
- Phase 2: `pytest tests/test_legacy_canon_supersede.py -q`; on a copy of staging data, run the migration script with `--dry-run` and confirm row counts; then run for real and confirm form builder returns `unknown` for previously-buggy entities.
- Phase 3: `pytest tests/test_chapter_review_form_dry_run.py -q`; manually set mode to `dry_run` in staging and verify the artifact endpoint returns the expected JSON for a recently generated chapter.
- Phase 4: `pytest tests/test_form_builder.py -q`
- Phase 5: `pytest tests/test_chapter_review_form_regression_suite.py -q`; the existing architecture boundary test must pass with the fixture directory scan extension.
- Phase 6: `pytest tests/test_plan_patcher_loop_closure.py tests/test_writer_prompt_contract.py tests/test_prompt_regression_samples.py -q`

Cross-phase: `pytest tests/test_chapter_review_form_*.py -q` then full suite when environment permits.

## Risk Controls

- **LLM begins citing canonical names that the chapter does not literally contain.** Mitigation: validator still checks `evidence_quote` is in chapter; only `subject_of_quote` is allowed to be an alias. The LLM cannot bypass quote-existence by aliasing.
- **Migration removes good canon state along with bad.** Mitigation: rows are marked `superseded`, not deleted; an audit path reads superseded rows; rebuild is opt-in and chapter-ranged.
- **Dry-run mode hides real bugs.** Mitigation: dry-run still emits warnings and artifacts; operator dashboards must surface warnings from form path with at least the visibility primary-mode errors get.
- **Pruning hard-protects too much and `FormBudgetExceeded` becomes common.** Mitigation: log structured payload of what could not be dropped; operator can adjust `must_track` flags, narrow `aliases`, or raise `token_budget_chars`.
- **Plan patcher loop produces over-prescriptive plans.** Mitigation: each plan patch records its source signal id and is rejectable by `plan_patch_validator`; existing validator already enforces scope rules.
- **Fixtures encode a specific project's mechanism vocabulary.** Mitigation: architecture-boundary test scans fixtures and fails on banned terms; CI gates the addition.

## Done Criteria

- Quote validator accepts cosmetically equivalent quotes; subject validator accepts canonical-name resolution of descriptive references.
- `FormBlockingPolicy` controls severity per verdict category; tests cover at least one warning-downgrade case.
- Legacy non-form transition rows are superseded; `form_builder` no longer treats them as prior canon.
- `dry_run` mode runs the full form path without blocking and persists artifacts.
- Pruning sorts by priority and hard-protects critical items; `form_budget_exceeded` surfaces when protection exceeds budget.
- All 15 regression fixtures exist, pass, and use only generic names.
- Form drift signals are consumed by the plan patcher; corresponding negative constraints are suppressed from the writer prompt for the patched chapter.
- Architecture boundary test extends to fixture-directory term scanning.
- All existing deletion guards (deleted modules, forbidden imports, no `prompt_json/` directories) still pass.

## Known Limitations And Deferred Work

- The form schema remains canon-quality-only. It does not assess prose style, repetition, or genre consistency; those stay in `reviewer/`.
- Subject resolution still requires the canonical name to be derivable. Descriptive aliases must be populated by callers; automatic alias discovery from prior canon is deferred.
- Cross-clause Chinese negation is still not parsed. The form sidesteps this by asking the LLM to read full chapter context, but isolated `subject_of_quote` errors in narrow LLM responses are still possible and rely on the validator's rejection path.
- The plan patcher loop closure addresses canon drift; it does not address narrative pacing, scene weight, or arc structure issues.
- Form schema versioning is single-version. Multi-version migration is not in scope until a breaking change is needed.
