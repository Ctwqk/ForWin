# ForWin Chapter Review Questionnaire Design

Date: 2026-05-19

Status: approved for implementation planning

## Scope

Replace the entire per-analyzer canon-quality validation layer with a single form-driven LLM review pass per chapter. The form is the sole canon-quality source. All deterministic keyword/regex analyzers and per-analyzer `prompt_json/` modules are removed once migration completes.

This is a destructive cleanup. Legacy `canon_quality/` modules and `prompt_json/` per-analyzer modules will be deleted, not kept as compatibility shells. Only BookState as canon storage and the writer/generation pipeline are preserved.

## Goals

- One LLM call per chapter delivers all canon-quality verdicts.
- LLM-native subject attribution eliminates the entire class of keyword-window false positives (e.g. "X 和 Y 高层合谋导致家族成员死亡" never again misattributes death to X or Y).
- Form schema is the extension point. Adding a new invariant kind means adding a form section, not writing a new analyzer module.
- Every blocking verdict carries an exact-substring evidence quote from chapter text, validated by code.
- Every entity verdict carries a self-declared `subject_of_quote` field that must match the entity being judged.
- BookState remains the only canon source; form answers project to BookState writes through explicit, validated paths.

## Non-Goals

- Do not preserve any per-analyzer `prompt_json/` module as a parallel path. They are replaced.
- Do not preserve deterministic keyword analyzers (`character_state.py`, `countdown_ledger`, `identity.py`, `final_completion.py`, `signals.py` keyword portions). They are replaced.
- Do not change BookState schema as canon storage.
- Do not change writer chapter generation. Only the post-write validation layer changes.
- Do not introduce general-purpose Chinese syntactic parsing. The form leverages LLM comprehension, not a parser.
- Do not split this work to keep `canon_quality/` modules around "for safety". Half-migrations have produced the current 16-module sprawl.

## Why Form-Driven Beats Per-Analyzer Prompt JSON

The recent `prompt_json/` pivot replaces each deterministic analyzer with an LLM analyzer. This is an N→N substitution and inherits four structural problems:

1. **N LLM calls per chapter**, each re-reading the chapter and re-loading partial canon state.
2. **Cross-analyzer inconsistency** — countdown analyzer may say "fulfilled" while character_state analyzer says the actor is still acting on the countdown.
3. **New invariant kind requires a new module** — same sprawl that produced 16 deterministic modules.
4. **Each analyzer is still vulnerable to local prompt misdirection** — a small-window LLM call inherits the same blind spots as a small-window regex.

The form replaces it with a 1→1 model: one call, one self-consistent answer, one extensible schema.

| Dimension | Per-analyzer prompt_json | Form questionnaire |
|---|---|---|
| LLM calls per chapter | N (~10) | 1 |
| Cross-section consistency | possible contradictions | guaranteed within one response |
| Adding a check | new module + wiring | new question in schema |
| Subject attribution | per-analyzer prompt | natural from full-text comprehension |
| Self-consistency check | impossible (separate calls) | `subject_of_quote` field, code-enforced |
| Token usage | N × (chapter + partial context) | 1 × (chapter + relevant context) |

## Selected Approach

### End State

After migration, `forwin/canon_quality/` contains approximately:

```
canon_quality/
├── chapter_review_form/        # form schema + builder + validator + projector + caller
│   ├── form_schema.py          # Pydantic models for form input and answer envelope
│   ├── form_builder.py         # build chapter-specific form from BookState
│   ├── llm_caller.py           # single structured-output LLM call
│   ├── evidence_validator.py   # quote-existence + self-consistency checks
│   ├── canon_projector.py      # validated answers → BookState writes
│   ├── mode_router.py          # shadow | hybrid | primary
│   └── pruning.py              # decide which entities/invariants to include
├── rule_profile.py             # project-level glossary (carried forward unchanged)
├── signals.py                  # CanonQualitySignal data class (carried forward)
├── repository.py               # persistence (carried forward, shrunk)
└── service.py                  # thin orchestration entry point (heavily shrunk)
```

Deleted: `character_state.py`, `countdown_ledger.py` (+ `countdown/` subdir), `identity.py`, `final_completion.py`, the regex/keyword half of `signals.py`, all `prompt_json/` per-analyzer modules under `canon_quality/`, `planning/`, and `gate/`.

Module count drops from 30+ to ~7.

### Form Schema

```python
# forwin/canon_quality/chapter_review_form/form_schema.py

from pydantic import BaseModel, Field
from typing import Literal

# ---- Answer envelope (used everywhere a binding verdict appears) ----

class FormAnswer(BaseModel):
    """Every binding answer must carry evidence and a self-declared subject."""
    value: str                         # the actual verdict
    evidence_quote: str = ""           # exact substring from chapter text
    subject_of_quote: str = ""         # who/what the evidence sentence is about
    confidence: float = 0.0            # 0.0 - 1.0
    explanation: str = ""              # free-text reasoning (not used for blocking)
    
    def is_bindable(self, min_confidence: float) -> bool:
        return (
            self.confidence >= min_confidence
            and bool(self.evidence_quote.strip())
        )

# ---- Section A: Characters ----

class CharacterReviewAsk(BaseModel):
    name: str
    prior_life_state: Literal["alive", "wounded", "dead", "unknown"]
    prior_custody_state: Literal["free", "captured", "unknown"]
    last_seen_chapter: int
    must_track: bool = False

class CharacterReviewAnswer(BaseModel):
    name: str
    appears_in_chapter: bool
    life_state: FormAnswer                       # value: alive | wounded | dead | unknown
    custody_state: FormAnswer                    # value: free | captured | unknown
    participation: FormAnswer                    # value: present_acting | mentioned_only | absent
    bridge_events: list["BridgeEvent"] = Field(default_factory=list)

class BridgeEvent(BaseModel):
    event_kind: Literal["capture", "release", "wound", "death", "resurrection_or_correction"]
    evidence_quote: str
    subject_of_quote: str
    confidence: float

# ---- Section B: Countdowns ----

class CountdownReviewAsk(BaseModel):
    key: str
    label: str
    prior_value_minutes: int | None
    prior_status: Literal["active", "paused", "closed", "fulfilled", "reopened"]
    last_updated_chapter: int

class CountdownReviewAnswer(BaseModel):
    key: str
    mentioned_in_chapter: bool
    status_in_this_chapter: FormAnswer           # value: unchanged | advanced | reset | fulfilled | reopened | closed | not_mentioned
    new_value_minutes: int | None = None
    new_value_evidence: FormAnswer | None = None
    consistent_with_prior: FormAnswer            # value: "true" | "false"
    inconsistency_kind: Literal["regression", "magnitude_mismatch", "reopened_after_close", "other", "none"] = "none"

# ---- Section C: Active obligations ----

class ObligationReviewAsk(BaseModel):
    id: str
    summary: str
    deadline_chapter: int
    must_resolve_now: bool
    payoff_test: str

class ObligationReviewAnswer(BaseModel):
    id: str
    addressed: FormAnswer                        # value: fulfilled | partial | unaddressed | explicitly_deferred
    payoff_evidence: FormAnswer | None = None

# ---- Section D: Open signals (carried-over residual issues) ----

class OpenSignalReviewAsk(BaseModel):
    id: str
    description: str
    severity: str
    age_chapters: int

class OpenSignalReviewAnswer(BaseModel):
    id: str
    status: FormAnswer                           # value: resolved | explained_in_text | persisting | worsened
    resolution_evidence: FormAnswer | None = None

# ---- Section E: Free-form discovery ----

class NewObservations(BaseModel):
    new_characters: list["NewCharacterObservation"] = Field(default_factory=list)
    new_countdowns: list["NewCountdownObservation"] = Field(default_factory=list)
    new_world_facts: list["NewWorldFact"] = Field(default_factory=list)

class NewCharacterObservation(BaseModel):
    name: str
    first_appearance_quote: str
    role_hint: str = ""

class NewCountdownObservation(BaseModel):
    description: str
    initial_value_minutes: int | None
    first_mention_quote: str

class NewWorldFact(BaseModel):
    fact: str
    evidence_quote: str
    category: Literal["setting", "rule", "identity", "relationship", "other"]

# ---- Section F: Final-chapter check (only when applicable) ----

class FinalChapterAsk(BaseModel):
    main_crisis_descriptors: list[str]           # from project glossary
    expected_closure_kinds: list[str]            # e.g. "system_shutdown", "truth_public"

class FinalChapterAnswer(BaseModel):
    main_crisis_status: FormAnswer               # value: closed_with_evidence | left_dangling | denied_or_avoided
    closure_evidence: FormAnswer | None = None
    unresolved_promises: list[str] = Field(default_factory=list)

# ---- Whole-form root ----

class ChapterReviewForm(BaseModel):
    project_id: str
    chapter_number: int
    form_schema_version: str
    characters: list[CharacterReviewAsk]
    countdowns: list[CountdownReviewAsk]
    obligations: list[ObligationReviewAsk]
    open_signals: list[OpenSignalReviewAsk]
    final_chapter: FinalChapterAsk | None = None

class ChapterReviewAnswers(BaseModel):
    project_id: str
    chapter_number: int
    form_schema_version: str
    characters: list[CharacterReviewAnswer]
    countdowns: list[CountdownReviewAnswer]
    obligations: list[ObligationReviewAnswer]
    open_signals: list[OpenSignalReviewAnswer]
    new_observations: NewObservations
    final_chapter: FinalChapterAnswer | None = None
    chapter_summary: str = ""                    # ≤ 280 chars, used by downstream summarizers
```

### Validators

Code (not LLM) performs three validations before any answer is allowed to write BookState or block generation:

1. **Quote existence**: every non-empty `evidence_quote` field must appear as an exact substring in the chapter body. Reject the entire answer if not. (No fuzzy match. LLM may quote with punctuation/whitespace variation — pre-normalize chapter text the same way before checking.)

2. **Self-consistency**: for any binding character verdict (`life_state == "dead"`, `custody_state == "captured"`, etc.), `subject_of_quote` must equal the character's name or a known alias. Reject if the LLM's own declared subject does not match the entity being judged.

3. **Confidence + envelope completeness**: blocking verdicts require `confidence >= min_blocking_confidence` (default 0.8) AND non-empty `evidence_quote`. Lower-confidence or missing-evidence answers degrade to "uncertain" and never block.

Rejected answers fall through to `mode_router` policy:
- `shadow`: log rejection, keep deterministic verdict
- `hybrid`: log rejection, fall back to deterministic verdict
- `primary`: log rejection, emit a `CanonQualitySignal` of kind `form_validation_failed` (informational, not blocking), then keep prior canon state

### LLM Call Structure

Single message structure per chapter:

```
system: You are a strict canon reviewer for a long-form Chinese web novel.
        Read the chapter and answer the form. Every binding answer requires
        an exact quote from the chapter text and an explicit subject_of_quote.
        Do not invent facts. If uncertain, set confidence < 0.5 and explain.

user:   [form schema as JSON with `ask` sections filled in]
        [prior canon state summary]
        [chapter body]
```

Structured output via JSON schema mode (already used by `prompt_json/` analyzers — reuse `PromptJsonClient.complete_json`).

### Form Pruning Rules

The form must stay under a token budget. `pruning.py` decides which entities/invariants make it into a given chapter's form:

- **Always include**:
  - Characters explicitly marked `must_track=True` in project governance (typically ≤ 10 leads)
  - Countdowns with `prior_status in {active, paused, reopened}` (open countdowns)
  - Obligations with `must_resolve_now=True` and `deadline_chapter <= chapter_number`
  - Open signals with `severity in {error, critical, blocker}` and `age_chapters >= 2`

- **Include if mentioned in chapter** (cheap pre-scan: substring match on entity name):
  - Other named characters with prior BookState records
  - Countdowns whose label or alias appears
  - Obligations whose subject names appear

- **Exclude**:
  - Closed countdowns last touched > 5 chapters ago
  - Characters with `prior_life_state == "dead"` and chapter does not mention them
  - Resolved signals
  - Fulfilled obligations

The pre-scan does NOT decide verdicts. It only decides whether to ASK about an entity. If the chapter mentions a dead character, the form will ask "does this chapter resurrect or correct prior canon?" rather than silently skipping.

---

## Phases

### Phase 0: Schema and Skeleton

**Files created:**
- `forwin/canon_quality/chapter_review_form/__init__.py`
- `forwin/canon_quality/chapter_review_form/form_schema.py`
- `forwin/canon_quality/chapter_review_form/mode_router.py`
- `tests/test_chapter_review_form_schema.py`

**Changes:**
- Define all Pydantic models from the Form Schema section.
- `mode_router.py` exposes `FormMode = Literal["off", "shadow", "hybrid", "primary"]` with a default of `"off"`.
- No LLM call, no service wiring.

**Acceptance:**
- Schema models round-trip via `model_validate` / `model_dump`.
- `form_schema_version` is a constant exported from `__init__.py`.
- Tests assert envelope rules: `FormAnswer.is_bindable` returns False without evidence quote.

---

### Phase 1: Builder + Validators (No LLM Yet)

**Files created:**
- `forwin/canon_quality/chapter_review_form/form_builder.py`
- `forwin/canon_quality/chapter_review_form/pruning.py`
- `forwin/canon_quality/chapter_review_form/evidence_validator.py`
- `forwin/canon_quality/chapter_review_form/canon_projector.py`
- `tests/test_form_builder.py`
- `tests/test_form_validators.py`
- `tests/test_canon_projector.py`

**Changes:**
- `form_builder.build_form(*, project_id, chapter_number, chapter_text, book_state, glossary) -> ChapterReviewForm` constructs the form, applying pruning rules.
- `evidence_validator.validate_answers(form, answers, chapter_text) -> ValidationReport` performs quote-existence and self-consistency checks. Returns per-section pass/reject lists.
- `canon_projector.project_validated_answers(answers, validation_report) -> list[BookStateWrite]` converts only validated answers into BookState writes. Rejected answers produce informational `CanonQualitySignal` entries with kind `form_answer_rejected`.
- `pruning.select_characters_to_ask`, `pruning.select_countdowns_to_ask`, etc. with explicit thresholds.

**Acceptance:**
- Builder produces a form that fits under a configurable character budget (default 8000 chars).
- Validator correctly rejects: missing quote, quote not in chapter, `subject_of_quote != name` for binding character verdicts.
- Projector never writes BookState for rejected answers.
- Mock-answer fixtures verify each rejection path with a 1-line example.

---

### Phase 2: LLM Caller + Shadow Mode

**Files created:**
- `forwin/canon_quality/chapter_review_form/llm_caller.py`
- `forwin/canon_quality/chapter_review_form/comparison_report.py`
- `tests/test_form_llm_caller.py`
- `tests/fixtures/chapter_review_form/` (10+ deterministic fixtures)

**Files modified:**
- `forwin/canon_quality/service.py`: after existing analyzers run, build form, call LLM, validate, project to a shadow log (NOT BookState), emit comparison report.
- `forwin/runtime_settings.py`: add `chapter_review_form_mode` setting, default `"off"`.

**Changes:**
- `llm_caller.call_form(form, *, chapter_text, prior_canon_summary, llm_client) -> ChapterReviewAnswers` uses `PromptJsonClient.complete_json` with the answer schema as the JSON schema constraint.
- Shadow mode runs the form path in parallel with existing analyzers. Form answers go to a new `chapter_review_form_shadow` table (or JSON file under artifacts), never to BookState.
- `comparison_report` produces per-chapter diffs: which verdicts agree, which disagree, where deterministic was wrong, where form was wrong, confidence distribution.

**Acceptance:**
- Setting mode to `"shadow"` runs both paths without affecting canon writes.
- Comparison report exists per chapter and is queryable.
- 10 fixture chapters demonstrate at least 3 known-deterministic-wrong cases (including the洛庭若 case) that the form gets right.
- No regressions in any existing deterministic test.

---

### Phase 3: Character State Migration to Hybrid

**Files modified:**
- `forwin/canon_quality/service.py`: in `hybrid` mode, character_state verdicts from the form override deterministic verdicts when validation passes and confidence ≥ threshold.
- `forwin/canon_quality/chapter_review_form/mode_router.py`: per-section override flags (`override_character_state=True`, others still false).

**Changes:**
- Character section of form answers becomes authoritative when validated.
- Deterministic `analyze_character_state_transitions` still runs as fallback when form rejects or LLM call fails.
- All character-related blocking errors now route through form first.

**Acceptance:**
- The 洛庭若 case generates correctly in hybrid mode (no false dead verdict).
- Calibration: across 30 sample chapters, form-overrides-deterministic disagreements are reviewed; threshold tuned so false-acceptance rate (form wrongly clears something deterministic correctly caught) ≤ 2%.
- Existing character_state tests adapted: assertions about specific `CharacterStateTransition` rows are reformulated to assert "given this chapter text, no `dead` verdict for 洛庭若" rather than asserting against the deterministic implementation.

---

### Phase 4: Hybrid Expansion to Countdown, Obligations, Signals, Final Completion

**Files modified:**
- Same service wiring with additional override flags.
- Each section migrated one at a time, with its own calibration pass.

**Changes:**
- Countdown verdicts from form override `countdown_ledger` analyzer.
- Obligation verifier prompt analyzer outputs become advisory only; form `ObligationReviewAnswer` is authoritative.
- Open-signal status changes flow through form; signal regeneration on the same issue stops.
- Final completion gate consults form `FinalChapterAnswer` first.

**Acceptance:**
- Each section has its own ≥ 30 sample-chapter calibration before flipping the override flag.
- Cross-section consistency improves measurably: report shows fewer "countdown says closed, character_state says actor still acting on it" pairs.
- The chapter 47/48/49 countdown regression (where 49 wrote 50 minutes after 47 closed) is caught by the form's `consistent_with_prior=false` answer and surfaces as a blocking signal in 49 instead of silently propagating.

---

### Phase 5: Destructive Cleanup

**Files deleted:**
- `forwin/canon_quality/character_state.py`
- `forwin/canon_quality/countdown_ledger.py`
- `forwin/canon_quality/countdown/` (entire directory)
- `forwin/canon_quality/identity.py`
- `forwin/canon_quality/final_completion.py`
- `forwin/canon_quality/prompt_json/` (entire directory)
- `forwin/planning/prompt_json/` (entire directory)
- `forwin/gate/prompt_json/` (entire directory)
- `forwin/gate/` (if empty after removal)
- All `tests/test_*` for the deleted modules
- All ALLOWED_PRODUCTION_MECHANISM_FILES entries that referenced these modules

**Files modified:**
- `forwin/canon_quality/service.py`: remove all branches that called deleted analyzers. Service becomes a thin entry: build form → call LLM → validate → project → emit signals.
- `forwin/canon_quality/signals.py`: keep only the data class and unrelated post-form helpers; remove keyword-driven signal generation.
- `forwin/canon_quality/repository.py`: prune queries that no longer have callers.
- `forwin/orchestrator/loop.py` (and `orchestrator_loop_core/*`): remove deterministic-fallback branches that imported deleted analyzers.
- `forwin/planning/future_plan_auditor.py`: remove `mode={deterministic,hybrid,prompt_json,shadow}` complexity — auditor reads canon state, which is now form-projected, and emits patches. No more deterministic vs prompt-json branching.
- `forwin/runtime_settings.py`: `chapter_review_form_mode` default flips to `"primary"`; the `"off"` and `"shadow"` values remain valid for emergency rollback.
- `tests/test_no_story_specific_hardcoding.py`: ALLOWED_PRODUCTION_MECHANISM_FILES shrinks to ≤ 2 entries (only `rule_profile.py` and a new `legacy_current_book_data.py` if extracted).

**Acceptance:**
- `forwin/canon_quality/` contains ≤ 7 modules.
- No production code imports from deleted modules.
- Full test suite passes.
- `wc -l forwin/canon_quality/**/*.py` decreases by at least 60% from current size.
- A new `tests/test_architecture_boundaries.py` rule forbids creating any new file under `canon_quality/` that does `text.find(keyword)` over a chapter window with an entity name; the only permitted entity-text matching is in `evidence_validator.py` for substring verification.

---

### Phase 6: Loop Closure with Pre-Write Plan Patching

**Files modified:**
- `forwin/planning/obligation_pre_audit.py`, `signal_pre_audit.py`: consume form-projected canon state instead of legacy signal queries.
- `forwin/planning/future_plan_audit/` (former `future_plan_auditor.py`): plan-time audit reads BookState (form-projected) and emits patches. The `suppressed_prompt_constraint_keys` mechanism stays.
- `forwin/writer/prompts.py`: remove writer-prompt constraint sections that duplicate what plan patches now cover. Keep only the constraints that pre-audit cannot fix (e.g. style guidance, scene-level brevity rules).

**Changes:**
- The form is the single source of "what is canon now". Plan patcher is the single source of "what next chapter must do about it". Writer prompt no longer carries negative-list constraints for things the plan patcher already addressed.
- The reactive constraint route documented as Known Limitation in the 5/17 architecture cleanup is closed.

**Acceptance:**
- Writer prompts for a typical mid-arc chapter shrink in token count (the `_canon_quality_context_section` shrinks since pre-audit handles most cases).
- Cross-chapter regression: the form catches drift between chapters 47-49 and the plan patcher rewrites chapter 49's plan before generation, so the writer never produces the conflicting "剩余 50 分钟" text in the first place.

---

## Token Budget

Per-chapter form call estimate:

| Component | Tokens |
|---|---|
| System prompt | ~500 |
| Form schema (with pruned `ask` sections) | ~2,500 |
| Prior canon state summary | ~1,500 |
| Chapter body | ~3,000-5,000 |
| Output answers JSON | ~2,000-4,000 |
| **Total per call** | ~10,000-14,000 |

Comparison to current `prompt_json/` per-analyzer approach:
- Current: ~10 calls × ~6,000 tokens each = ~60,000 tokens per chapter
- Form: ~12,000 tokens per chapter
- **Net reduction: ~80%**

Caching:
- System prompt: cached across all calls in a project.
- Form schema structure: cached across all chapters of a project (only `ask` field values vary).
- Prior canon state summary: not cached (varies per chapter).

If a project's tracked entity set exceeds budget, pruning rules tighten automatically (drop characters not mentioned in chapter that have stable canon status).

## Risk Controls

- **LLM hallucinated quotes**: defended by `evidence_validator.validate_answers` — any quote not in chapter text causes the entire answer to be rejected.
- **LLM misattributes subject**: defended by `subject_of_quote` self-consistency check — the LLM cannot say "X is dead" while declaring the subject of its evidence is "Y".
- **LLM low confidence accepted as blocking**: defended by `min_blocking_confidence` threshold; sub-threshold answers degrade to advisory.
- **Shadow disagreements masked**: Phase 2 comparison reports must be human-reviewed before Phase 3-4 flip override flags. No automatic promotion.
- **Schema drift**: `form_schema_version` is part of the form. Persistence layer tags every shadow answer with the schema version. Regression fixtures pin a specific version.
- **Pruning hides a real issue**: pre-scan is conservative — when uncertain, include the entity. Pruning rules logged so retroactive review can spot dropped-but-relevant entities.
- **Token budget exceeded**: builder enforces hard cap; over-budget forms trigger pruning escalation rather than truncation.
- **LLM call failures**: `mode_router` falls back to prior canon state (no writes) and emits a `CanonQualitySignal` of kind `form_llm_unavailable`. Hybrid mode falls back to deterministic only during Phases 3-4; after Phase 5 the deterministic fallback is gone and the chapter enters a "canon-quality unverified" state that operator review handles.

## Known Limitations and Deferred Work

- **Form does not handle pre-write planning quality**: this is `future_plan_auditor`'s job. The form only validates what happened. Phase 6 closes the loop but does not change planning quality itself.
- **Form does not catch all stylistic drift**: prose quality (repetition, awkward phrasing, genre mismatch) is not in scope. `reviewer/webnovel.py` and the style telemetry layer remain.
- **Form schema is currently codebase-defined, not project-defined**: a project can extend the glossary (`rule_profile.py`) and that drives the `ask` content, but adding a new section kind (e.g. a per-project bespoke invariant category) still requires editing the schema. A fully data-driven schema is deferred; the current model handles common-case canon invariants (characters, countdowns, obligations, signals, final completion).
- **Form requires a working LLM**: deterministic fallback exists only during Phases 3-4. After Phase 5 the operator must handle LLM outages by pausing generation, not by relying on keyword analyzers.
- **No retroactive correction loop**: the form reports inconsistency with prior canon (`consistent_with_prior=false`) but does not automatically rewrite prior chapters. That is operator workflow, not validator workflow.
- **Chinese-only**: prompt and form text are Chinese. Multilingual generation is out of scope.

## Done Criteria

- `forwin/canon_quality/` contains ≤ 7 modules; deleted modules listed in Phase 5 are gone.
- All deterministic keyword-window entity extractors in canon_quality are removed; the architecture boundary test forbids reintroduction.
- All per-analyzer `prompt_json/` modules under `canon_quality/`, `planning/`, and `gate/` are removed.
- `chapter_review_form` is the only canon-quality validation path; `chapter_review_form_mode` default is `"primary"`.
- The 洛庭若 false-positive case and the chapter 47-49 countdown regression each have a regression fixture under `tests/fixtures/chapter_review_form/` proving the form catches them.
- Writer prompt token count for `_canon_quality_context_section` decreases by ≥ 40% on the existing prompt regression fixtures (because pre-audit patches absorb most constraints).
- ALLOWED_PRODUCTION_MECHANISM_FILES shrinks to ≤ 2 entries.
- Token usage per chapter canon-quality validation drops by ≥ 60% vs current per-analyzer baseline (measured on the same 10 fixture chapters).

## Test Plan

- **Schema and envelope**: `tests/test_chapter_review_form_schema.py` — round-trip, envelope rules, version constants.
- **Builder and pruning**: `tests/test_form_builder.py` — given a synthetic BookState + chapter, builder produces the expected form; pruning rules drop dead-and-unmentioned characters; budget enforcement triggers escalation.
- **Validators**: `tests/test_form_validators.py` — quote-existence, self-consistency, confidence thresholds, all rejection paths.
- **Projector**: `tests/test_canon_projector.py` — validated answers write BookState rows; rejected answers produce signals only.
- **LLM caller**: `tests/test_form_llm_caller.py` — mock client returns structured JSON; assert payload normalization and error handling.
- **Regression fixtures**: `tests/fixtures/chapter_review_form/` — 15+ chapters covering:
  - 洛庭若 case (subject misattribution)
  - chapter 47-49 countdown drift (cross-chapter consistency)
  - "X 不能死" plan vs. "X actually dies" body (negation handling)
  - dead character resurrected with bridge event (must accept)
  - dead character mentioned without resurrection (must not reset state)
  - new character introduced mid-chapter (discovery section)
  - terminal chapter with crisis closed (final section pass)
  - terminal chapter with crisis dangling (final section block)
  - countdown reset with explicit reset event
  - countdown silently regressed (must block)
  - obligation fulfilled with explicit payoff
  - obligation skipped (must block)
  - signal resolved in subsequent chapter
  - signal persisting after N chapters (must block when severity high)
  - chapter where pruning correctly excludes a long-dead minor character
- **Shadow comparison**: `tests/test_shadow_comparison.py` — synthetic disagreement scenarios produce expected comparison-report entries.
- **End-to-end**: `tests/test_chapter_review_form_e2e.py` — full pipeline from BookState + chapter text → validated answers → BookState writes, with a real (recorded) LLM response.
- **Architecture boundary**: `tests/test_architecture_boundaries.py` adds a rule rejecting new files under `forwin/canon_quality/` that do `text.find(keyword)` on chapter text with an entity name. Only `evidence_validator.py` is allowed to substring-match on chapter text.
- **Prompt regression integration**: the existing `tests/test_prompt_regression_samples.py` is extended with a fixture that asserts writer prompts after Phase 6 are smaller (specific char-count threshold per fixture).

## Open Questions for Implementation

These are not blockers but should be decided before Phase 1 ends:

1. **Quote normalization**: should `evidence_validator` strip ASCII vs. fullwidth punctuation differences? Recommend: normalize both sides (chapter text and quote) to a canonical form before substring match.
2. **`subject_of_quote` aliasing**: should the validator accept "她" / "他" if the prior sentence established the pronoun's referent? Recommend: no — only accept name or explicit known alias. Pronoun resolution belongs in the LLM, not the validator.
3. **Shadow-mode storage location**: separate SQLite table vs. JSON files under `data/artifacts/`. Recommend: JSON files initially (cheaper to iterate); promote to a table if shadow run lasts more than two weeks.
4. **Form schema versioning policy**: backward-compatible additions only (new optional fields), or allow breaking changes with a migration script? Recommend: additive only; breaking changes require a `form_schema_version` bump and a re-run of shadow comparison.
