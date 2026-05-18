# Chapter Review Form Replacement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace legacy deterministic and per-analyzer prompt-json canon-quality validation with one form-driven chapter review pass.

**Architecture:** `analyze_writer_output_quality()` becomes a thin form-only orchestrator: build `ChapterReviewForm`, call one structured LLM, validate evidence, project validated answers into compatibility rows/signals, and expose review issues to the existing gate/reviewer surfaces. Old analyzer modules and old prompt-json directories are deleted; planning and gate code fall back to existing deterministic plan checks rather than prompt-json branches.

**Tech Stack:** Python 3.12+, Pydantic v2 models, SQLAlchemy repositories, pytest, existing `WriterOutput` and `CanonQualitySignal` contracts.

---

### Task 1: Form Schema And Evidence Validator

**Files:**
- Create: `forwin/canon_quality/chapter_review_form/__init__.py`
- Create: `forwin/canon_quality/chapter_review_form/form_schema.py`
- Create: `forwin/canon_quality/chapter_review_form/errors.py`
- Create: `forwin/canon_quality/chapter_review_form/evidence_validator.py`
- Test: `tests/test_chapter_review_form_schema.py`
- Test: `tests/test_form_validators.py`

- [ ] **Step 1: Write failing schema tests**

```python
from forwin.canon_quality.chapter_review_form import FORM_SCHEMA_VERSION
from forwin.canon_quality.chapter_review_form.form_schema import ChapterReviewAnswers, FormAnswer

def test_form_answer_requires_quote_to_bind():
    assert FORM_SCHEMA_VERSION
    assert FormAnswer(value="dead", confidence=0.99).is_bindable(0.8) is False
    assert FormAnswer(value="dead", evidence_quote="林青倒下", subject_of_quote="林青", confidence=0.99).is_bindable(0.8)

def test_answers_round_trip_with_sections():
    answers = ChapterReviewAnswers.model_validate({
        "project_id": "p1",
        "chapter_number": 3,
        "form_schema_version": FORM_SCHEMA_VERSION,
        "characters": [],
        "countdowns": [],
        "obligations": [],
        "open_signals": [],
        "new_observations": {},
    })
    assert answers.model_dump(mode="json")["project_id"] == "p1"
```

- [ ] **Step 2: Verify RED**

Run: `python3 -m pytest tests/test_chapter_review_form_schema.py -q`
Expected: import failure for `forwin.canon_quality.chapter_review_form`.

- [ ] **Step 3: Write minimal schema implementation**

Implement the Pydantic models from the approved spec with `FORM_SCHEMA_VERSION = "chapter_review_form.v1"`.

- [ ] **Step 4: Write failing validator tests**

```python
from forwin.canon_quality.chapter_review_form.evidence_validator import validate_answers
from forwin.canon_quality.chapter_review_form.form_schema import (
    CharacterReviewAsk,
    CharacterReviewAnswer,
    ChapterReviewAnswers,
    ChapterReviewForm,
    FormAnswer,
    NewObservations,
)

def test_validator_rejects_quote_not_in_chapter():
    form = ChapterReviewForm(project_id="p1", chapter_number=1, form_schema_version="chapter_review_form.v1", characters=[CharacterReviewAsk(name="林青", prior_life_state="alive", prior_custody_state="free", last_seen_chapter=0)], countdowns=[], obligations=[], open_signals=[])
    answers = ChapterReviewAnswers(project_id="p1", chapter_number=1, form_schema_version="chapter_review_form.v1", characters=[CharacterReviewAnswer(name="林青", appears_in_chapter=True, life_state=FormAnswer(value="dead", evidence_quote="林青死亡", subject_of_quote="林青", confidence=0.99), custody_state=FormAnswer(value="free"), participation=FormAnswer(value="present_acting"))], countdowns=[], obligations=[], open_signals=[], new_observations=NewObservations())
    report = validate_answers(form=form, answers=answers, chapter_text="林青只是回头看了一眼。")
    assert report.rejected
    assert report.rejected[0].reason == "quote_not_found"

def test_validator_rejects_group_subject_for_singleton_state():
    form = ChapterReviewForm(project_id="p1", chapter_number=1, form_schema_version="chapter_review_form.v1", characters=[CharacterReviewAsk(name="林青", prior_life_state="alive", prior_custody_state="free", last_seen_chapter=0)], countdowns=[], obligations=[], open_signals=[])
    quote = "林青和委员会高层的合谋导致家族成员死亡。"
    answers = ChapterReviewAnswers(project_id="p1", chapter_number=1, form_schema_version="chapter_review_form.v1", characters=[CharacterReviewAnswer(name="林青", appears_in_chapter=True, life_state=FormAnswer(value="dead", evidence_quote=quote, subject_of_quote="家族成员", confidence=0.99), custody_state=FormAnswer(value="free"), participation=FormAnswer(value="mentioned_only"))], countdowns=[], obligations=[], open_signals=[], new_observations=NewObservations())
    report = validate_answers(form=form, answers=answers, chapter_text=quote)
    assert report.rejected
    assert report.rejected[0].reason == "subject_mismatch"
```

- [ ] **Step 5: Verify RED**

Run: `python3 -m pytest tests/test_form_validators.py -q`
Expected: import failure for `validate_answers` or missing report model.

- [ ] **Step 6: Implement validator**

Implement exact-substring validation, pronoun rejection, subject-name/alias consistency, and `ValidationReport(validated, rejected, blocking_rejections)`.

- [ ] **Step 7: Verify GREEN**

Run: `python3 -m pytest tests/test_chapter_review_form_schema.py tests/test_form_validators.py -q`
Expected: all tests pass.

### Task 2: Form Builder, Pruning, And Projector

**Files:**
- Create: `forwin/canon_quality/chapter_review_form/pruning.py`
- Create: `forwin/canon_quality/chapter_review_form/form_builder.py`
- Create: `forwin/canon_quality/chapter_review_form/canon_projector.py`
- Test: `tests/test_form_builder.py`
- Test: `tests/test_canon_projector.py`

- [ ] **Step 1: Write failing builder tests**

```python
from forwin.canon_quality.chapter_review_form.form_builder import build_form

def test_builder_includes_active_countdowns_and_mentioned_characters():
    form = build_form(
        project_id="p1",
        chapter_number=7,
        chapter_text="林青再次提到主倒计时。",
        character_rows=[{"character_name": "林青", "to_state": "alive", "chapter_number": 3}],
        countdown_rows=[{"countdown_key": "main", "label": "主倒计时", "normalized_remaining_minutes": 50, "status": "consistent", "chapter_number": 6}],
        open_signal_rows=[],
        obligations=[],
        target_total_chapters=12,
        token_budget_chars=4000,
    )
    assert [item.name for item in form.characters] == ["林青"]
    assert [item.key for item in form.countdowns] == ["main"]
```

- [ ] **Step 2: Verify RED**

Run: `python3 -m pytest tests/test_form_builder.py -q`
Expected: import failure for `form_builder`.

- [ ] **Step 3: Implement builder/pruning**

Implement list-based inputs so service can pass repository rows without introducing a new BookState dependency. Pruning may use substring matching only to decide which asks enter the form; it must not infer verdicts.

- [ ] **Step 4: Write failing projector tests**

```python
from forwin.canon_quality.chapter_review_form.canon_projector import project_validated_answers
from forwin.canon_quality.chapter_review_form.evidence_validator import ValidationReport
from forwin.canon_quality.chapter_review_form.form_schema import CharacterReviewAnswer, ChapterReviewAnswers, FormAnswer, NewObservations

def test_projector_writes_only_validated_character_death():
    answer = CharacterReviewAnswer(name="林青", appears_in_chapter=True, life_state=FormAnswer(value="dead", evidence_quote="林青倒下，再无呼吸。", subject_of_quote="林青", confidence=0.95), custody_state=FormAnswer(value="unknown"), participation=FormAnswer(value="present_acting"))
    answers = ChapterReviewAnswers(project_id="p1", chapter_number=2, form_schema_version="chapter_review_form.v1", characters=[answer], countdowns=[], obligations=[], open_signals=[], new_observations=NewObservations())
    projection = project_validated_answers(answers=answers, validation_report=ValidationReport(validated=["characters[0].life_state"], rejected=[]), draft_id="d1", min_blocking_confidence=0.8)
    assert projection.character_transitions[0].payload["source"] == "chapter_review_form"
    assert not projection.signals
```

- [ ] **Step 5: Verify RED**

Run: `python3 -m pytest tests/test_canon_projector.py -q`
Expected: import failure for `canon_projector`.

- [ ] **Step 6: Implement projector**

Project validated character life/custody answers into compatibility `CharacterStateTransition` rows, countdown answers into compatibility `CountdownLedgerEntry` rows, validation failures into non-blocking `form_answer_rejected` signals, and high-confidence inconsistency answers into blocking signals.

- [ ] **Step 7: Verify GREEN**

Run: `python3 -m pytest tests/test_form_builder.py tests/test_canon_projector.py -q`
Expected: all tests pass.

### Task 3: LLM Caller And Form Service

**Files:**
- Create: `forwin/canon_quality/chapter_review_form/llm_caller.py`
- Create: `forwin/canon_quality/chapter_review_form/service.py`
- Test: `tests/test_form_llm_caller.py`
- Test: `tests/test_chapter_review_form_e2e.py`

- [ ] **Step 1: Write failing caller tests**

```python
from forwin.canon_quality.chapter_review_form.llm_caller import call_form
from forwin.canon_quality.chapter_review_form.form_schema import ChapterReviewForm

class FakeClient:
    def complete_json(self, **kwargs):
        return {"project_id": "p1", "chapter_number": 1, "form_schema_version": "chapter_review_form.v1", "characters": [], "countdowns": [], "obligations": [], "open_signals": [], "new_observations": {}, "chapter_summary": "ok"}

def test_call_form_uses_single_structured_json_call():
    form = ChapterReviewForm(project_id="p1", chapter_number=1, form_schema_version="chapter_review_form.v1", characters=[], countdowns=[], obligations=[], open_signals=[])
    answers = call_form(form=form, chapter_text="正文", prior_canon_summary="既有 canon", llm_client=FakeClient())
    assert answers.chapter_summary == "ok"
```

- [ ] **Step 2: Verify RED**

Run: `python3 -m pytest tests/test_form_llm_caller.py -q`
Expected: import failure for `llm_caller`.

- [ ] **Step 3: Implement caller**

Support clients with `complete_json(**kwargs)`, `complete_json(messages=..., output_schema=...)`, or `generate_json(...)`. Raise `ChapterReviewFormUnavailable` when no compatible client exists.

- [ ] **Step 4: Write failing e2e tests**

```python
from forwin.canon_quality.chapter_review_form.service import review_chapter_with_form
from forwin.protocol.writer import WriterOutput

class FakeClient:
    def complete_json(self, **kwargs):
        quote = "林青倒下，再无呼吸。"
        return {"project_id": "p1", "chapter_number": 2, "form_schema_version": "chapter_review_form.v1", "characters": [{"name": "林青", "appears_in_chapter": True, "life_state": {"value": "dead", "evidence_quote": quote, "subject_of_quote": "林青", "confidence": 0.95}, "custody_state": {"value": "unknown"}, "participation": {"value": "present_acting"}}], "countdowns": [], "obligations": [], "open_signals": [], "new_observations": {}, "chapter_summary": "林青死亡。"}

def test_form_service_projects_validated_answer():
    result = review_chapter_with_form(session=None, project_id="p1", chapter_number=2, writer_output=WriterOutput(project_id="p1", chapter_number=2, title="二", body="林青倒下，再无呼吸。", end_of_chapter_summary=""), draft_id="d1", llm_client=FakeClient(), character_rows=[{"character_name": "林青", "to_state": "alive", "chapter_number": 1}])
    assert result.mode == "chapter_review_form"
    assert result.character_transitions[0].character_name == "林青"
```

- [ ] **Step 5: Verify RED**

Run: `python3 -m pytest tests/test_chapter_review_form_e2e.py -q`
Expected: import failure for `review_chapter_with_form`.

- [ ] **Step 6: Implement form service**

Compose builder, caller, validator, and projector. On LLM unavailable or schema invalid, return a blocking signal with `signal_type` `form_llm_unavailable` or `form_schema_invalid`.

- [ ] **Step 7: Verify GREEN**

Run: `python3 -m pytest tests/test_form_llm_caller.py tests/test_chapter_review_form_e2e.py -q`
Expected: all tests pass.

### Task 4: Wire Canon Quality Service And Configuration

**Files:**
- Modify: `forwin/canon_quality/service.py`
- Modify: `forwin/config.py`
- Modify: `forwin/orchestrator_loop_core/common.py`
- Modify: `forwin/orchestrator_loop_core/quality_gates.py`
- Test: `tests/test_canon_quality_config.py`
- Test: `tests/test_canon_quality_service.py`
- Test: `tests/test_chapter_review_form_api_flow.py`

- [ ] **Step 1: Write failing config/api tests**

Update config tests to assert `chapter_review_form_mode == "primary"`, `chapter_review_form_min_blocking_confidence == 0.8`, and env names `FORWIN_CHAPTER_REVIEW_FORM_MODE`, `FORWIN_CHAPTER_REVIEW_FORM_MIN_BLOCKING_CONFIDENCE`, `FORWIN_CHAPTER_REVIEW_FORM_MAX_LLM_RETRIES`, `FORWIN_CHAPTER_REVIEW_FORM_TOKEN_BUDGET_CHARS`.

- [ ] **Step 2: Verify RED**

Run: `python3 -m pytest tests/test_canon_quality_config.py tests/test_chapter_review_form_api_flow.py -q`
Expected: missing config fields or old service behavior.

- [ ] **Step 3: Implement config and service wiring**

Remove old analyzer imports from `forwin/canon_quality/service.py`. Call `review_chapter_with_form()` only. Preserve non-canon stylistic/support checks only if they do not depend on deleted analyzer modules. Persist compatibility rows via `CanonQualityRepository`.

- [ ] **Step 4: Update orchestrator gate**

Stop passing prompt-json mode into service. Remove `_run_obligation_prompt_json_gate` from gate evaluation and use `analysis.raw_analyzer_results` as form review results only. Gate confidence comes from `chapter_review_form_min_blocking_confidence`.

- [ ] **Step 5: Verify GREEN**

Run: `python3 -m pytest tests/test_canon_quality_config.py tests/test_canon_quality_service.py tests/test_chapter_review_form_api_flow.py -q`
Expected: all tests pass.

### Task 5: Remove Old Prompt-Json Branches From Planning And Reviewer

**Files:**
- Modify: `forwin/planning/future_plan_audit/auditor.py`
- Modify: `forwin/planning/future_plan_audit/*.py`
- Modify: `forwin/planning/plan_patch_validator.py`
- Modify: `forwin/reviewer/hub.py`
- Modify: `forwin/orchestrator_loop_core/service.py`
- Test: `tests/test_future_plan_auditor.py`
- Test: `tests/test_future_plan_audit_persistence.py`
- Test: `tests/test_canon_quality_reviewer_payload.py`

- [ ] **Step 1: Write failing architecture import test**

Architecture test in Task 7 will fail while these imports remain. Use it as the RED signal for this task.

- [ ] **Step 2: Implement deterministic-only planning cleanup**

Remove `PromptJsonMode`, `normalize_prompt_json_mode`, `FuturePlanPromptAuditor`, `FuturePlanPromptJsonMixin`, and prompt-json issue validation imports. `FuturePlanAuditor(mode=...)` should accept legacy mode strings but normalize all values to `"chapter_review_form"` for metadata while running the deterministic BookState/context audit.

- [ ] **Step 3: Implement deterministic-only plan patch validation**

Remove `_validate_prompt_json()` and prompt-json prompt client use. `PlanPatchValidator(mode=...)` should preserve the existing deterministic validation regardless of legacy mode value.

- [ ] **Step 4: Reviewer metadata cleanup**

Change reviewer hub to consume `deterministic_quality_report["review_issues"]` directly when present, without importing prompt-json normalization.

- [ ] **Step 5: Verify GREEN**

Run: `python3 -m pytest tests/test_future_plan_auditor.py tests/test_future_plan_audit_persistence.py tests/test_canon_quality_reviewer_payload.py -q`
Expected: all tests pass after updated expectations.

### Task 6: Delete Old Analyzer And Prompt-Json Modules

**Files:**
- Delete: `forwin/canon_quality/character_state.py`
- Delete: `forwin/canon_quality/countdown_ledger.py`
- Delete: `forwin/canon_quality/countdown/`
- Delete: `forwin/canon_quality/identity.py`
- Delete: `forwin/canon_quality/final_completion.py`
- Delete: `forwin/canon_quality/prompt_json/`
- Delete: `forwin/planning/prompt_json/`
- Delete: `forwin/gate/prompt_json/`
- Delete: old unit tests that import deleted modules.
- Modify: context assembler imports that only needed `extract_candidate_character_names`.

- [ ] **Step 1: Write replacement helper tests if needed**

If context assemblers still need candidate-name extraction, add tests for a small generic helper under `forwin/canon_names.py` or a local context helper.

- [ ] **Step 2: Remove production imports**

Run: `git ls-files '*.py' | xargs grep -n "canon_quality.prompt_json\\|planning.prompt_json\\|gate.prompt_json\\|canon_quality.character_state\\|canon_quality.countdown_ledger\\|canon_quality.identity\\|canon_quality.final_completion"`
Expected after edits: no production hits outside migrations/models/compatibility references.

- [ ] **Step 3: Delete files**

Use `rm` for deleted modules and old tests only after production imports are gone.

- [ ] **Step 4: Verify compile**

Run: `python3 -m compileall -q forwin`
Expected: success.

### Task 7: Architecture Boundaries And Affected Test Sweep

**Files:**
- Create: `tests/test_chapter_review_form_architecture_boundaries.py`
- Modify: `tests/test_no_story_specific_hardcoding.py`
- Modify: `tests/test_large_module_boundaries.py`

- [ ] **Step 1: Write architecture tests**

Assert no production import uses deleted modules, no `prompt_json` directories exist under `forwin/canon_quality`, `forwin/planning`, or `forwin/gate`, and production code outside fixtures/spec docs does not contain the fixture-specific example name `洛庭若`.

- [ ] **Step 2: Verify RED**

Run: `python3 -m pytest tests/test_chapter_review_form_architecture_boundaries.py -q`
Expected before cleanup: fails on deleted-module imports/directories.

- [ ] **Step 3: Update size and hardcoding tests**

Remove deleted module expectations from `tests/test_large_module_boundaries.py` and shrink old allowlists in `tests/test_no_story_specific_hardcoding.py`.

- [ ] **Step 4: Run affected tests**

Run:
`python3 -m pytest tests/test_chapter_review_form_schema.py tests/test_form_builder.py tests/test_form_validators.py tests/test_canon_projector.py tests/test_form_llm_caller.py tests/test_chapter_review_form_e2e.py tests/test_chapter_review_form_api_flow.py tests/test_chapter_review_form_architecture_boundaries.py tests/test_canon_quality_config.py tests/test_canon_quality_service.py tests/test_canon_quality_repository.py tests/test_canon_admission_gate.py tests/test_canon_quality_reviewer_payload.py tests/test_future_plan_auditor.py tests/test_future_plan_audit_persistence.py tests/test_writer_prompt_contract.py -q`
Expected: all tests pass.

- [ ] **Step 5: Run full verification**

Run: `python3 -m compileall -q forwin && python3 -m pytest -q`
Expected: success, or documented unrelated failures with direct evidence.

- [ ] **Step 6: Commit**

Commit message: `feat: replace canon quality with chapter review form`.
