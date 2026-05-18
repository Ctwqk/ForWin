# Chapter Review Form Destructive Replacement Design

Date: 2026-05-18

Status: approved for implementation planning

## Source

This spec refines `docs/superpowers/specs/2026-05-19-chapter-review-questionnaire-design.md` into the selected implementation strategy: one destructive replacement, with no deterministic or per-analyzer `prompt_json` fallback.

The current failure that motivated the work is only a regression fixture. Production rules must remain book-agnostic: no character names, story titles, or plot-specific exceptions may appear in validators, prompts, configuration, or allowlists.

## Scope

Replace the canon-quality validation path with one form-driven LLM review pass per chapter.

The new path is:

```text
WriterOutput + BookState context
  -> ChapterReviewForm
  -> one structured LLM answer
  -> evidence validation
  -> canon projection
  -> review verdict
```

The old paths are removed from production execution:

- deterministic keyword/window analyzers
- per-analyzer `canon_quality/prompt_json` modules
- planning and gate `prompt_json` modules tied to the old analyzer architecture
- hybrid/shadow fallback modes for canon-quality validation

BookState remains the canon store. The form is the only validation and projection entry point.

## Non-Goals

- Do not build a Chinese parser or dependency analyzer.
- Do not preserve old deterministic analyzers as compatibility shells.
- Do not preserve old prompt-json analyzers as fallback.
- Do not encode the current book's names, places, or plot beats into production logic.
- Do not drop legacy database tables as part of this change unless a table has no remaining reader and no migration risk. Old tables may remain as storage compatibility while old code paths are removed.
- Do not change writer prose generation except where old prompt constraint sections must be removed because the new plan patch path owns them.

## Architecture

Create `forwin/canon_quality/chapter_review_form/`:

- `form_schema.py`: Pydantic models for asks, answers, evidence envelopes, and schema versioning.
- `form_builder.py`: builds a chapter-specific form from BookState, project governance, active obligations, open signals, countdown state, and chapter text.
- `pruning.py`: decides which entities and invariants to ask about under a token budget. It never decides verdicts.
- `llm_caller.py`: sends one structured-output LLM request and parses `ChapterReviewAnswers`.
- `evidence_validator.py`: validates quotes, subject attribution, confidence, and answer completeness. It does not infer canon facts.
- `canon_projector.py`: projects only validated answers to BookState-compatible writes and review issues.
- `service.py`: thin orchestration entry point.
- `errors.py`: typed failure reasons such as `form_llm_unavailable`, `form_schema_invalid`, and `form_answer_rejected`.

Keep `forwin/canon_quality/rule_profile.py`, a trimmed `signals.py`, and a trimmed `repository.py` if still required by storage and review surfaces.

After cleanup, `forwin/canon_quality/` must contain only the form package and these small support modules.

## Deleted Production Paths

Remove production imports and callers for:

- `forwin/canon_quality/character_state.py`
- `forwin/canon_quality/countdown_ledger.py`
- `forwin/canon_quality/countdown/`
- `forwin/canon_quality/identity.py`
- `forwin/canon_quality/final_completion.py`
- `forwin/canon_quality/prompt_json/`
- `forwin/planning/prompt_json/`
- `forwin/gate/prompt_json/`

Related orchestration branches in `orchestrator_loop_core/*`, `planning/*`, and `gate/*` must stop referencing deterministic, hybrid, shadow, or prompt-json canon-quality modes.

## Binding Evidence Rules

Every binding answer uses an evidence envelope:

- `value`: verdict value.
- `evidence_quote`: exact quote from the chapter text.
- `subject_of_quote`: who or what the evidence quote is about.
- `confidence`: model confidence.
- `explanation`: non-binding reasoning.

Code validates these rules before projection:

1. **Quote existence**: `evidence_quote` must appear in normalized chapter text. No fuzzy matching.
2. **Subject consistency**: for entity verdicts, `subject_of_quote` must equal the judged entity name or a known alias.
3. **No pronoun binding**: `他`, `她`, `他们`, and similar pronouns are not accepted as `subject_of_quote`.
4. **No group-to-singleton binding**: if the declared subject is a group, member class, organization leadership, follower, victim group, family member, or subordinate, the answer cannot write state to a different individual or organization.
5. **Confidence threshold**: blocking verdicts require `confidence >= 0.8` and a non-empty valid quote.

If the validator rejects an answer, that answer cannot write BookState or emit a blocking canon verdict.

## Generic Regression Principle

The production rule is subject attribution, not any specific story fact.

Example category:

```text
X and Y leadership caused deaths among Z family members.
```

This may support a fact about `Z family members`, but it cannot support `X is dead` or `Y is dead` unless the evidence quote explicitly has X or Y as the subject of death.

Specific current-book examples may appear only in fixtures under `tests/fixtures/chapter_review_form/`.

## Countdown and Closed-State Rules

Countdown and status regressions are also form-driven and generic.

If prior canon says an invariant is `closed`, `fulfilled`, or equivalent, a later answer may only reopen it when the chapter contains an explicit bridge event:

- reset
- reopen
- prior report was false
- new countdown replacing the old one
- explicit correction from canon authority

Without that bridge, a claim that the closed condition is active again becomes a blocking review issue. The block must cite the current chapter quote and the prior canon summary used by the form.

## Error Handling

There is no old analyzer fallback.

### LLM Unavailable

- Chapter enters `needs_review`.
- Emit `form_llm_unavailable`.
- Do not write strong canon state.
- Do not auto-continue generation.

### Invalid JSON or Schema Mismatch

- Allow at most one structured-output retry.
- If retry fails, chapter enters `needs_review`.
- Emit `form_schema_invalid`.
- Do not write strong canon state.

### Evidence Validation Failure

- Reject the failed answer.
- Emit `form_answer_rejected` with reason and answer path.
- If the rejected answer was required to settle a blocking/critical invariant, chapter enters `needs_review`.
- Advisory answer failures do not block, but they also do not project.

## Configuration

Replace old mode settings with:

- `chapter_review_form_mode`: default `primary`.
- `chapter_review_form_min_blocking_confidence`: default `0.8`.
- `chapter_review_form_max_llm_retries`: default `1`.
- `chapter_review_form_token_budget_chars`: default based on current LLM context budget.

`off` may exist only for local tests or emergency operator intervention. Production containers default to `primary`.

Deprecate or remove main-chain use of:

- `canon_quality_mode`
- `prompt_json_analysis_enabled`
- `prompt_json_min_blocking_confidence`
- `prompt_json_require_evidence_for_block`

## Storage Compatibility

Legacy tables such as `character_state_transitions` and `countdown_ledgers` may remain initially because other context builders and UI surfaces may read them.

The new `canon_projector` may write compatibility rows into these tables, but rows must include payload metadata:

```json
{"source": "chapter_review_form", "form_schema_version": "..."}
```

No legacy analyzer may write those rows after migration.

## Review Surface

Existing chapter review APIs should continue returning review issues, residual issues, and status fields.

New issue metadata should include:

- `source_layer="canon_quality"`
- `source_mode="chapter_review_form"`
- `form_schema_version`
- `answer_path`
- `validation_status`
- `evidence_quote`
- `subject_of_quote`

This keeps operator tools usable while replacing the backend source.

## Testing

Add focused tests:

- `tests/test_chapter_review_form_schema.py`
- `tests/test_form_builder.py`
- `tests/test_form_validators.py`
- `tests/test_canon_projector.py`
- `tests/test_form_llm_caller.py`
- `tests/test_chapter_review_form_e2e.py`
- `tests/test_chapter_review_form_api_flow.py`
- `tests/test_chapter_review_form_architecture_boundaries.py`

Fixture coverage under `tests/fixtures/chapter_review_form/`:

- subject misattribution does not write singleton death
- group-subject quote does not bind to unrelated named entity
- closed countdown cannot silently become active
- explicit reset/reopen bridge can reopen a closed invariant
- dead character with explicit correction bridge can return
- dead character merely mentioned remains dead
- new character observation creates advisory discovery data
- obligation fulfilled with evidence
- obligation skipped past deadline blocks
- final chapter closes main crisis
- final chapter leaves main crisis dangling
- LLM unavailable pauses without canon writes
- schema invalid pauses without canon writes
- evidence quote not found rejects answer
- low confidence cannot block

Architecture tests must forbid:

- production imports from deleted analyzer modules
- any `prompt_json` directory under `forwin/canon_quality`, `forwin/planning`, or `forwin/gate`
- new canon-quality code that matches entity names against keyword windows to infer state
- production rules containing fixture-specific story names

## Acceptance Criteria

- `chapter_review_form` is the only canon-quality validation path.
- No production code imports deleted analyzer modules.
- No deterministic/prompt-json fallback is available in the generation path.
- Form validator prevents subject-misattribution state writes.
- Closed-state/countdown regressions block unless explicit bridge evidence exists.
- LLM or schema failure pauses generation instead of writing canon.
- Existing affected chapter review APIs still return usable review details.
- Full affected API, task, and canon-quality tests pass.
- `forwin/canon_quality/` module count is reduced to the form package plus small support modules.

## Implementation Notes

This change should be implemented as a dedicated branch and commit series because it deletes substantial production code.

Recommended implementation order:

1. Add form schema, validator, and projector with tests.
2. Add LLM caller and form service with mock tests.
3. Wire `analyze_writer_output_quality` to the form-only path.
4. Wire orchestrator quality gate to pause on form failures.
5. Remove old analyzer imports and mode config.
6. Delete old analyzer and prompt-json modules.
7. Update context builders and planning surfaces to consume form-projected state.
8. Add architecture boundary tests.
9. Run full affected API/task/generation tests.

Do not add story-specific exceptions during any phase.
