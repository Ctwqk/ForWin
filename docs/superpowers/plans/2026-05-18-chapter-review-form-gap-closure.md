# Chapter Review Form Gap Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the production gaps in the Chapter Review Form path while validating and deploying one phase at a time.

**Architecture:** Keep the Chapter Review Form as the sole canon-quality analyzer path. Add narrow helpers around validation, projection policy, canon supersede state, dry-run artifacts, budget pruning, regression fixtures, and plan-patcher consumption without restoring deleted deterministic analyzers or `prompt_json` modules. Each phase is independently tested, committed, deployed, and validated on one live chapter through ForWin MCP.

**Tech Stack:** Python 3.12/3.13, Pydantic, SQLAlchemy ORM, pytest, Docker Compose, ForWin MCP.

---

## Source Documents

- Functional authority: `docs/superpowers/specs/2026-05-18-chapter-review-form-gap-closure-design.md`
- Execution authority: `docs/superpowers/specs/2026-05-18-chapter-review-form-gap-closure-execution-design.md`
- Live workflow authority: `AGENTS.md`

## Execution Rules

- Work on `/home/taiwei/ForWin` `master`; the user explicitly approved execution and deployment on master.
- Use TDD for behavior changes: write a failing test, run it, then change production code.
- Commit one phase at a time with the phase-local tests passing.
- Before each deployment, run:

```bash
git status --short --branch
python3 -m compileall -q forwin
python3 -m pytest --ignore=tests/browser -q
```

- Use ForWin MCP for project, task, and chapter state. Do not query the database or call ad hoc HTTP endpoints when an MCP tool covers the workflow.
- At each live validation point, re-read live state through MCP. If the 60-chapter task is still active, pause through MCP before container deployment. If it has completed, use the active 30-chapter task.
- Deploy each phase with:

```bash
docker compose up -d --build forwin forwin-mcp
python3 scripts/check_codex_operator_ready.py
```

## File Responsibility Map

- `forwin/canon_quality/chapter_review_form/evidence_validator.py`: quote normalization, subject validation, rejected-answer diagnostics, evidence-required rejection.
- `forwin/canon_quality/chapter_review_form/form_schema.py`: additive schema fields such as `descriptive_aliases`.
- `forwin/canon_quality/chapter_review_form/form_builder.py`: populate additive ask fields, ignore superseded canon rows, pass forms through priority-aware pruning.
- `forwin/canon_quality/chapter_review_form/llm_caller.py`: system prompt and answer-shape normalization only.
- `forwin/canon_quality/chapter_review_form/canon_projector.py`: projection, per-category severity policy, plan-patchable signal metadata.
- `forwin/canon_quality/chapter_review_form/service.py`: form mode behavior, artifacts, comparison summaries, budget-exceeded fallback.
- `forwin/canon_quality/chapter_review_form/pruning.py`: priority ordering and hard protection logic.
- `forwin/canon_quality/chapter_review_form/comparison_report.py`: dry-run artifact summary helper.
- `forwin/canon_quality/repository.py`: supersede-aware reads and writes for form-era canon rows.
- `forwin/config.py`: config fields and typed `FormBlockingPolicy`.
- `scripts/migrate_legacy_canon_to_form.py`: audited dry-run and opt-in supersede/rebuild migration.
- `forwin/api.py` or an existing router module: read-only form artifact inspection route if the API path is selected over CLI.
- `forwin/planning/countdown_drift_pre_audit.py`: countdown drift signal to plan patch conversion.
- `forwin/planning/future_plan_audit/auditor.py`: include form-derived countdown drift pre-audit.
- `forwin/planning/obligation_pre_audit.py`: consume form obligation signals as urgent obligations.
- `forwin/planning/signal_pre_audit.py`: consume form open-signal persistence.
- `forwin/writer/prompts.py`: suppress negative constraints when an equivalent plan patch exists.

## Phase 1: Validator Edge-Case Hardening

**Files:**
- Modify: `forwin/canon_quality/chapter_review_form/evidence_validator.py`
- Modify: `forwin/canon_quality/chapter_review_form/form_schema.py`
- Modify: `forwin/canon_quality/chapter_review_form/form_builder.py`
- Modify: `forwin/canon_quality/chapter_review_form/llm_caller.py`
- Modify: `forwin/canon_quality/chapter_review_form/canon_projector.py`
- Modify: `forwin/canon_quality/chapter_review_form/service.py`
- Modify: `forwin/config.py`
- Test: `tests/test_form_validators.py`
- Test: `tests/test_form_llm_caller.py`
- Test: `tests/test_canon_projector.py`
- Test: `tests/test_canon_quality_config.py`

- [ ] **Step 1: Add validator red tests**

Append these focused tests to `tests/test_form_validators.py`:

```python
def test_validator_accepts_punctuation_equivalent_quote() -> None:
    form = _form_for_character("角色A")
    answers = _answers_for_character(
        "角色A",
        life_state=FormAnswer(
            value="dead",
            evidence_quote="角色A说：“时间到了……”",
            subject_of_quote="角色A",
            confidence=0.95,
        ),
    )

    report = validate_answers(
        form=form,
        answers=answers,
        chapter_text='角色A说: "时间到了..."',
    )

    assert "characters[0].life_state" in report.validated
    assert report.rejected == []


def test_validator_accepts_descriptive_alias_subject() -> None:
    form = ChapterReviewForm(
        project_id="p1",
        chapter_number=1,
        form_schema_version=FORM_SCHEMA_VERSION,
        characters=[
            CharacterReviewAsk(
                name="角色A",
                aliases=["A"],
                descriptive_aliases=["那个穿白衣的人"],
                prior_life_state="alive",
                prior_custody_state="free",
                last_seen_chapter=0,
            )
        ],
        countdowns=[],
        obligations=[],
        open_signals=[],
    )
    answers = _answers_for_character(
        "角色A",
        life_state=FormAnswer(
            value="wounded",
            evidence_quote="那个穿白衣的人捂住伤口退后。",
            subject_of_quote="那个穿白衣的人",
            confidence=0.91,
        ),
    )

    report = validate_answers(
        form=form,
        answers=answers,
        chapter_text="那个穿白衣的人捂住伤口退后。",
    )

    assert "characters[0].life_state" in report.validated
    assert report.rejected == []


def test_validator_rejects_binding_answer_without_evidence() -> None:
    form = _form_for_character("角色A")
    answers = _answers_for_character(
        "角色A",
        life_state=FormAnswer(
            value="dead",
            evidence_quote="",
            subject_of_quote="角色A",
            confidence=0.96,
        ),
    )

    report = validate_answers(form=form, answers=answers, chapter_text="角色A仍然站着。")

    assert report.rejected[0].reason == "missing_evidence"
    assert report.rejected[0].blocking is True
    assert report.rejected[0].value == "dead"
    assert report.rejected[0].confidence == 0.96


def test_rejection_diagnostics_include_value_and_confidence() -> None:
    form = _form_for_character("角色A")
    answers = _answers_for_character(
        "角色A",
        life_state=FormAnswer(
            value="dead",
            evidence_quote="角色A倒下。",
            subject_of_quote="角色A",
            confidence=0.87,
        ),
    )

    report = validate_answers(form=form, answers=answers, chapter_text="角色A转身离开。")

    rejected = report.rejected[0]
    assert rejected.reason == "quote_not_found"
    assert rejected.value == "dead"
    assert rejected.confidence == 0.87
    assert "value=dead" in rejected.message
    assert "confidence=0.87" in rejected.message
```

- [ ] **Step 2: Verify validator red tests fail**

Run:

```bash
python3 -m pytest tests/test_form_validators.py -q
```

Expected: failures mention `descriptive_aliases` missing, quote punctuation not accepted, and missing-evidence answer currently validated.

- [ ] **Step 3: Add LLM prompt red test**

Append this test to `tests/test_form_llm_caller.py`:

```python
def test_system_prompt_instructs_canonical_name_resolution() -> None:
    client = FakeClient()
    form = ChapterReviewForm(
        project_id="p1",
        chapter_number=1,
        form_schema_version=FORM_SCHEMA_VERSION,
        characters=[],
        countdowns=[],
        obligations=[],
        open_signals=[],
    )

    call_form(form=form, chapter_text="正文", prior_canon_summary="", llm_client=client)

    system_content = client.calls[0]["messages"][0]["content"]
    assert "descriptive reference" in system_content
    assert "pronoun" in system_content
    assert "canonical name" in system_content
    assert "subject_of_quote" in system_content
```

- [ ] **Step 4: Verify LLM prompt red test fails**

Run:

```bash
python3 -m pytest tests/test_form_llm_caller.py::test_system_prompt_instructs_canonical_name_resolution -q
```

Expected: assertion failure because the current prompt does not mention descriptive references or canonical names.

- [ ] **Step 5: Add projection and config red tests**

Append this test to `tests/test_canon_projector.py`:

```python
from forwin.config import FormBlockingPolicy
from forwin.canon_quality.chapter_review_form.form_schema import ObligationReviewAnswer


def test_projector_uses_form_blocking_policy_for_warning_category() -> None:
    obligation = ObligationReviewAnswer(
        id="义务-1",
        addressed=FormAnswer(
            value="partial",
            evidence_quote="角色A只完成了第一步。",
            subject_of_quote="义务-1",
            confidence=0.93,
        ),
    )
    answers = ChapterReviewAnswers(
        project_id="p1",
        chapter_number=2,
        form_schema_version=FORM_SCHEMA_VERSION,
        characters=[],
        countdowns=[],
        obligations=[obligation],
        open_signals=[],
        new_observations=NewObservations(),
    )

    projection = project_validated_answers(
        answers=answers,
        validation_report=ValidationReport(
            validated=["obligations[0].addressed"],
            blocking_paths=["obligations[0].addressed"],
        ),
        blocking_policy=FormBlockingPolicy(obligation_partial="warning"),
    )

    assert projection.signals[0].signal_type == "form_obligation_unresolved"
    assert projection.signals[0].severity == "warning"
    assert projection.review_issues[0]["severity"] == "warning"
```

Append this test to `tests/test_canon_quality_config.py`:

```python
def test_form_blocking_policy_can_be_loaded_from_env(monkeypatch) -> None:
    monkeypatch.setenv("FORWIN_FORM_BLOCKING_OBLIGATION_PARTIAL", "error")

    config = Config.from_env()

    assert config.form_blocking_policy.obligation_partial == "error"
    assert config.form_blocking_policy.character_wounded == "warning"
```

- [ ] **Step 6: Verify projection and config red tests fail**

Run:

```bash
python3 -m pytest tests/test_canon_projector.py::test_projector_uses_form_blocking_policy_for_warning_category tests/test_canon_quality_config.py::test_form_blocking_policy_can_be_loaded_from_env -q
```

Expected: import or signature failure because `FormBlockingPolicy` and `blocking_policy` are absent.

- [ ] **Step 7: Implement validator schema additions**

Change `CharacterReviewAsk` in `forwin/canon_quality/chapter_review_form/form_schema.py`:

```python
class CharacterReviewAsk(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    descriptive_aliases: list[str] = Field(default_factory=list)
    prior_life_state: Literal["alive", "wounded", "dead", "unknown"]
    prior_custody_state: Literal["free", "captured", "unknown"]
    last_seen_chapter: int
    must_track: bool = False
```

Change `_character_ask` in `forwin/canon_quality/chapter_review_form/form_builder.py` so it reads both fields:

```python
descriptive_aliases = []
if isinstance(payload, dict) and isinstance(payload.get("descriptive_aliases"), list):
    descriptive_aliases = [str(item).strip() for item in payload.get("descriptive_aliases", []) if str(item).strip()]
return CharacterReviewAsk(
    name=name,
    aliases=aliases,
    descriptive_aliases=descriptive_aliases,
    prior_life_state=state,
    prior_custody_state=custody,
    last_seen_chapter=int(row_value(row, "chapter_number", 0) or 0),
    must_track=bool(payload.get("must_track") if isinstance(payload, dict) else False),
)
```

Change `known_character_subjects` in `evidence_validator.py`:

```python
known_character_subjects = {
    ask.name: {
        ask.name,
        *[alias for alias in ask.aliases if alias],
        *[alias for alias in ask.descriptive_aliases if alias],
    }
    for ask in form.characters
}
```

- [ ] **Step 8: Implement quote normalization and rejection diagnostics**

Replace `_normalize_text` and enrich `RejectedAnswer` in `evidence_validator.py`:

```python
PUNCTUATION_EQUIVALENTS = str.maketrans(
    {
        "＂": '"',
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "（": "(",
        "）": ")",
        "【": "[",
        "】": "]",
        "［": "[",
        "］": "]",
        "，": ",",
        "。": ".",
        "！": "!",
        "？": "?",
        "；": ";",
        "：": ":",
        "－": "-",
        "—": "-",
        "–": "-",
        "…": ".",
        "·": ".",
    }
)


class RejectedAnswer(BaseModel):
    path: str
    reason: str
    message: str = ""
    blocking: bool = False
    value: str = ""
    confidence: float = 0.0


def _rejection(
    *,
    path: str,
    reason: str,
    message: str,
    blocking: bool = False,
    answer: FormAnswer | None = None,
) -> RejectedAnswer:
    value = str(answer.value or "").strip() if answer is not None else ""
    confidence = float(answer.confidence or 0.0) if answer is not None else 0.0
    details = f"{message}; value={value}; confidence={confidence:.2f}" if answer is not None else message
    return RejectedAnswer(path=path, reason=reason, message=details, blocking=blocking, value=value, confidence=confidence)


def _normalize_text(value: str) -> str:
    normalized = str(value or "").translate(PUNCTUATION_EQUIVALENTS).lower()
    return re.sub(r"\s+", "", normalized)
```

Before quote-substring validation in `_validate_form_answer`, reject binding values with no quote:

```python
requires_evidence = value in (binding_values or set()) and answer.confidence >= float(min_blocking_confidence)
if requires_evidence and not quote:
    report.rejected.append(
        _rejection(
            path=path,
            reason="missing_evidence",
            message="binding answer requires evidence_quote",
            blocking=value in (blocking_values or set()),
            answer=answer,
        )
    )
    return
```

Use `_rejection(...)` for quote, pronoun, subject, bridge-event, and observation quote rejections.

- [ ] **Step 9: Implement prompt instruction**

Extend `SYSTEM_PROMPT` in `forwin/canon_quality/chapter_review_form/llm_caller.py` with this exact semantic content:

```python
"When a quote uses a descriptive reference, pronoun, role title, or other indirect reference "
"to a tracked entity, resolve subject_of_quote to that entity's canonical name from the form's "
"name field, or to one of that entity's aliases. Example: if the form asks for name='角色A' "
"and the chapter says '那个穿白衣的人倒下', return subject_of_quote='角色A', not '那个穿白衣的人'. "
```

- [ ] **Step 10: Implement `FormBlockingPolicy`**

Add to `forwin/config.py`:

```python
from typing import Literal


class FormBlockingPolicy(BaseModel):
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

Add these `_ConfigFields` attributes and `_env_values()` keys, using env names prefixed with `FORWIN_FORM_BLOCKING_`:

```python
form_blocking_character_dead: str = "error"
form_blocking_character_wounded: str = "warning"
form_blocking_character_captured: str = "error"
form_blocking_countdown_inconsistent: str = "error"
form_blocking_countdown_reset: str = "warning"
form_blocking_countdown_advanced: str = "warning"
form_blocking_obligation_unaddressed: str = "error"
form_blocking_obligation_partial: str = "warning"
form_blocking_signal_persisting: str = "error"
form_blocking_signal_worsened: str = "error"
form_blocking_final_dangling: str = "error"
form_blocking_final_denied: str = "error"
```

Add this property to `Config`:

```python
@property
def form_blocking_policy(self) -> FormBlockingPolicy:
    return FormBlockingPolicy(
        character_dead=self.form_blocking_character_dead,
        character_wounded=self.form_blocking_character_wounded,
        character_captured=self.form_blocking_character_captured,
        countdown_inconsistent=self.form_blocking_countdown_inconsistent,
        countdown_reset=self.form_blocking_countdown_reset,
        countdown_advanced=self.form_blocking_countdown_advanced,
        obligation_unaddressed=self.form_blocking_obligation_unaddressed,
        obligation_partial=self.form_blocking_obligation_partial,
        signal_persisting=self.form_blocking_signal_persisting,
        signal_worsened=self.form_blocking_signal_worsened,
        final_dangling=self.form_blocking_final_dangling,
        final_denied=self.form_blocking_final_denied,
    )
```

- [ ] **Step 11: Apply policy in projection**

Change `project_validated_answers` signature in `canon_projector.py`:

```python
def project_validated_answers(
    *,
    answers: ChapterReviewAnswers,
    validation_report: ValidationReport,
    draft_id: str = "",
    min_blocking_confidence: float = 0.8,
    blocking_policy: FormBlockingPolicy | None = None,
) -> ProjectionResult:
    policy = blocking_policy or FormBlockingPolicy()
```

Map answer paths to policy keys:

```python
def _severity_for_answer(*, policy: FormBlockingPolicy, signal_type: str, answer: FormAnswer) -> str:
    value = str(answer.value or "").strip()
    if signal_type == "form_countdown_inconsistency":
        if value in {"reset", "reopened"}:
            return policy.countdown_reset
        if value == "advanced":
            return policy.countdown_advanced
        return policy.countdown_inconsistent
    if signal_type == "form_obligation_unresolved":
        return policy.obligation_partial if value == "partial" else policy.obligation_unaddressed
    if signal_type == "form_open_signal_persisting":
        return policy.signal_worsened if value == "worsened" else policy.signal_persisting
    if signal_type == "form_final_chapter_unresolved":
        return policy.final_denied if value == "denied_or_avoided" else policy.final_dangling
    if value == "wounded":
        return policy.character_wounded
    if value == "captured":
        return policy.character_captured
    return policy.character_dead
```

Pass `severity=_severity_for_answer(...)` into `_blocking_signal` and include the severity in `review_issues`.

- [ ] **Step 12: Thread policy through service**

Change `review_chapter_with_form` to accept `blocking_policy: FormBlockingPolicy | None = None` and pass it to `project_validated_answers`. Change `analyze_writer_output_quality` in `forwin/canon_quality/service.py` to pass `config.form_blocking_policy`.

- [ ] **Step 13: Verify Phase 1 targeted tests pass**

Run:

```bash
python3 -m pytest tests/test_form_validators.py tests/test_form_llm_caller.py tests/test_canon_projector.py tests/test_canon_quality_config.py -q
```

Expected: all selected tests pass.

- [ ] **Step 14: Verify Phase 1 affected suite**

Run:

```bash
python3 -m pytest tests/test_chapter_review_form_e2e.py tests/test_chapter_review_form_schema.py tests/test_chapter_review_form_api_flow.py -q
```

Expected: all selected tests pass.

- [ ] **Step 15: Commit Phase 1**

```bash
git add forwin/canon_quality/chapter_review_form/evidence_validator.py forwin/canon_quality/chapter_review_form/form_schema.py forwin/canon_quality/chapter_review_form/form_builder.py forwin/canon_quality/chapter_review_form/llm_caller.py forwin/canon_quality/chapter_review_form/canon_projector.py forwin/canon_quality/chapter_review_form/service.py forwin/canon_quality/service.py forwin/config.py tests/test_form_validators.py tests/test_form_llm_caller.py tests/test_canon_projector.py tests/test_canon_quality_config.py
git commit -m "feat: harden chapter review form validation"
```

- [ ] **Step 16: Deploy and live-test Phase 1**

Use MCP to identify the selected validation task and project, pause only if needed, deploy containers, continue one chapter, then inspect `task_get`, `project_get`, and `chapter_get`. Record whether any review-form issue is system-level or evidence-backed narrative quality.

## Phase 2: Legacy Canon Data Migration

**Files:**
- Create: `scripts/migrate_legacy_canon_to_form.py`
- Modify: `forwin/canon_quality/repository.py`
- Modify: `forwin/canon_quality/chapter_review_form/form_builder.py`
- Test: `tests/test_legacy_canon_supersede.py`

- [ ] **Step 1: Add supersede red tests**

Create `tests/test_legacy_canon_supersede.py` with tests that insert one legacy transition, one form transition, one legacy countdown entry, and one form countdown entry. Assert that `CanonQualityRepository.list_character_transitions(project_id)` and `list_countdown_entries(project_id, include_details=True)` return only form-sourced rows by default, while `include_superseded=True` returns all rows with payload markers preserved.

- [ ] **Step 2: Verify supersede red tests fail**

```bash
python3 -m pytest tests/test_legacy_canon_supersede.py -q
```

Expected: `include_superseded` is not accepted and legacy rows are still returned.

- [ ] **Step 3: Add repository supersede helpers**

Add default-excluding parameters:

```python
def list_character_transitions(
    self,
    project_id: str,
    *,
    before_chapter: int | None = None,
    include_superseded: bool = False,
) -> list[dict[str, Any]]:
    ...
    payload = _loads(row.payload_json, {})
    if not include_superseded and payload.get("superseded_by"):
        continue
```

Mirror the same pattern in `list_countdown_entries`.

- [ ] **Step 4: Add migration script tests**

Extend `tests/test_legacy_canon_supersede.py` to call pure helper functions from `scripts/migrate_legacy_canon_to_form.py`: `is_form_sourced(payload)`, `mark_payload_superseded(payload)`, and `summarize_rows(rows)`. Expected behavior: form-sourced rows are not marked; rows without `payload.source == "chapter_review_form"` receive `superseded_by="chapter_review_form_migration"`.

- [ ] **Step 5: Verify migration helper red tests fail**

```bash
python3 -m pytest tests/test_legacy_canon_supersede.py::test_migration_marks_only_non_form_rows -q
```

Expected: import failure for the migration script.

- [ ] **Step 6: Implement migration script**

Create `scripts/migrate_legacy_canon_to_form.py` with argparse flags `--dry-run`, `--project-id`, `--apply`, `--rebuild-from-chapter`, and `--confirm-rebuild`. The script loads `Config.from_env()`, opens a database session through existing repository/session helpers, prints before/after counts, refuses writes unless `--apply` is present, and refuses rebuild unless both `--rebuild-from-chapter N` and `--confirm-rebuild` are present.

- [ ] **Step 7: Make builder treat superseded-only prior as unknown**

Add a test that passes only rows whose payload includes `superseded_by`. Assert `build_form(...).characters[0].prior_life_state == "unknown"` for tracked rows whose only prior state is superseded. Change `form_builder` to ignore superseded rows before `_character_ask` and `_countdown_ask`.

- [ ] **Step 8: Verify Phase 2 tests pass**

```bash
python3 -m pytest tests/test_legacy_canon_supersede.py tests/test_form_builder.py -q
```

Expected: all selected tests pass.

- [ ] **Step 9: Commit Phase 2**

```bash
git add scripts/migrate_legacy_canon_to_form.py forwin/canon_quality/repository.py forwin/canon_quality/chapter_review_form/form_builder.py tests/test_legacy_canon_supersede.py tests/test_form_builder.py
git commit -m "feat: supersede legacy canon rows for review form"
```

- [ ] **Step 10: Deploy and live-test Phase 2**

Run migration dry-run after deployment and record counts. Run real supersede only when dry-run counts show non-form rows only. Continue one live chapter through MCP and confirm form prior state no longer inherits superseded rows while audit reads still see them.

## Phase 3: Dry-Run Safety Net

**Files:**
- Create: `forwin/canon_quality/chapter_review_form/comparison_report.py`
- Modify: `forwin/canon_quality/service.py`
- Modify: `forwin/canon_quality/chapter_review_form/service.py`
- Modify: `forwin/config.py`
- Modify: `forwin/api.py` or the smallest existing API router that owns canon-quality diagnostics
- Test: `tests/test_chapter_review_form_dry_run.py`

- [ ] **Step 1: Add dry-run red tests**

Create `tests/test_chapter_review_form_dry_run.py` with a fake client returning one blocking countdown inconsistency. Assert `analyze_writer_output_quality(..., mode="dry_run", persist=True)` runs validation, emits warning-level signals only, returns `blocking is False`, and does not call repository canon-write methods for transitions or countdown entries.

- [ ] **Step 2: Verify dry-run red tests fail**

```bash
python3 -m pytest tests/test_chapter_review_form_dry_run.py -q
```

Expected: `_normalize_form_mode("dry_run")` currently resolves to primary or the dry-run artifact path is absent.

- [ ] **Step 3: Add comparison report helper**

Create `comparison_report.py`:

```python
def summarize_form_run(answers: ChapterReviewAnswers, validation_report: ValidationReport, projection: ProjectionResult) -> dict[str, Any]:
    return {
        "validated_count": len(validation_report.validated),
        "rejected_count": len(validation_report.rejected),
        "signals_by_severity": _count_by(signal.severity for signal in projection.signals),
        "blocking_eligible": [
            signal.model_dump(mode="json")
            for signal in projection.signals
            if signal.severity in {"error", "warning"}
        ],
        "top_rejections": [item.model_dump(mode="json") for item in validation_report.rejected[:10]],
    }
```

- [ ] **Step 4: Persist dry-run artifacts**

Add service helper `persist_form_artifact(root, result)` that writes JSON under `data/artifacts/chapter_review_form/<project_id>/<chapter_number>.json`. The artifact contains form, answers, validation report, projection summary, signals, review issues, and mode.

- [ ] **Step 5: Add inspection path**

Prefer a read-only API route if a canon-quality diagnostics router is already present; otherwise create a CLI helper. The endpoint or CLI takes project id and chapter number and returns the persisted artifact JSON without re-running the LLM.

- [ ] **Step 6: Route LLM-unavailable by mode**

Change `_failure_result` to accept `severity` and `blocking`. In dry-run, `form_llm_unavailable` is warning and non-blocking; in primary, it remains error and blocking.

- [ ] **Step 7: Verify Phase 3 tests pass**

```bash
python3 -m pytest tests/test_chapter_review_form_dry_run.py tests/test_chapter_review_form_e2e.py -q
```

Expected: all selected tests pass.

- [ ] **Step 8: Commit Phase 3**

```bash
git add forwin/canon_quality/chapter_review_form/comparison_report.py forwin/canon_quality/service.py forwin/canon_quality/chapter_review_form/service.py forwin/config.py tests/test_chapter_review_form_dry_run.py
git commit -m "feat: add chapter review form dry run artifacts"
```

- [ ] **Step 9: Deploy and live-test Phase 3**

Temporarily set `FORWIN_CHAPTER_REVIEW_FORM_MODE=dry_run` for the validation chapter, deploy, run one chapter through MCP, inspect the artifact, then restore primary mode.

## Phase 4: Pruning Priority And Budget Robustness

**Files:**
- Modify: `forwin/canon_quality/chapter_review_form/pruning.py`
- Modify: `forwin/canon_quality/chapter_review_form/form_builder.py`
- Modify: `forwin/canon_quality/chapter_review_form/service.py`
- Test: `tests/test_form_builder.py`

- [ ] **Step 1: Add pruning red tests**

Append tests to `tests/test_form_builder.py` asserting that low-severity signals are dropped before error signals, `must_resolve_now=True` obligations survive, `prior_status="active"` countdowns survive, and protected-only over-budget forms yield a `form_budget_exceeded` warning instead of silent removal.

- [ ] **Step 2: Verify pruning red tests fail**

```bash
python3 -m pytest tests/test_form_builder.py -q
```

Expected: current `_fit_budget` pops from section ends and has no `FormBudgetExceeded`.

- [ ] **Step 3: Add priority constants and exception**

Add to `pruning.py`:

```python
SIGNAL_SEVERITY_ORDER = {"blocker": 0, "critical": 1, "error": 2, "warning": 3, "info": 4}
COUNTDOWN_STATUS_PRIORITY = {"reopened": 0, "active": 1, "paused": 2, "warning": 3, "conflict": 4, "fulfilled": 5, "closed": 6, "resolved": 7, "consistent": 8}


class FormBudgetExceeded(RuntimeError):
    def __init__(self, protected_counts: dict[str, int]) -> None:
        self.protected_counts = protected_counts
        super().__init__(f"Chapter review form protected items exceed budget: {protected_counts}")
```

- [ ] **Step 4: Sort and hard-protect before truncation**

Move `_fit_budget` into `pruning.py` or delegate from `form_builder.py`, then apply the spec order: signals by severity, obligations by `must_resolve_now` and deadline, countdowns by prior status, characters by `must_track` and recency. Refuse to pop protected signals, protected obligations, and protected countdowns.

- [ ] **Step 5: Emit budget-exceeded warning and proceed**

Catch `FormBudgetExceeded` in `review_chapter_with_form`, add `form_budget_exceeded` warning signal to the result, and continue with the unfittable form.

- [ ] **Step 6: Verify Phase 4 tests pass**

```bash
python3 -m pytest tests/test_form_builder.py tests/test_chapter_review_form_e2e.py -q
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit Phase 4**

```bash
git add forwin/canon_quality/chapter_review_form/pruning.py forwin/canon_quality/chapter_review_form/form_builder.py forwin/canon_quality/chapter_review_form/service.py tests/test_form_builder.py
git commit -m "feat: prioritize chapter review form pruning"
```

- [ ] **Step 8: Deploy and live-test Phase 4**

Run one live chapter and confirm no active countdown, must-resolve obligation, or error signal is silently pruned. If `form_budget_exceeded` appears, record that it is warning-level and form execution proceeds.

## Phase 5: Fixture Suite Completion

**Files:**
- Create: `tests/fixtures/chapter_review_form/`
- Create: `tests/test_chapter_review_form_regression_suite.py`
- Modify: `tests/test_chapter_review_form_architecture_boundaries.py`

- [ ] **Step 1: Add fixture runner red test**

Create `tests/test_chapter_review_form_regression_suite.py` that discovers fixture directories, loads `form.json`, `chapter.txt`, `expected_answers.json`, `expected_signals.json`, and `expected_transitions.json`, runs fake-client validation/projection without real LLM calls, and asserts expected signals and transitions exactly.

- [ ] **Step 2: Verify fixture runner red test fails**

```bash
python3 -m pytest tests/test_chapter_review_form_regression_suite.py -q
```

Expected: fixture directory or required cases are missing.

- [ ] **Step 3: Add the 15 generic fixtures**

Create these fixture directories with `form.json`, `chapter.txt`, `expected_answers.json`, `expected_signals.json`, `expected_transitions.json`, and non-empty `notes.md`:

```text
subject_attribution_misdirection
cross_chapter_countdown_regression
already_dead_character_resurrected_with_bridge
already_dead_character_mentioned_without_resurrection
final_chapter_main_crisis_closed
final_chapter_main_crisis_dangling
obligation_silently_skipped
obligation_partial_default_warning
open_signal_resolved_with_evidence
open_signal_persisting_high_severity
pruning_drops_long_dead_minor_character
pruning_protects_active_countdown_under_pressure
quote_punctuation_form_difference
subject_descriptive_reference
budget_exceeded_emits_warning
```

Use generic labels only: `角色A`, `角色B`, `倒计时甲`, `义务-1`, `信号-1`, `事件-1`.

- [ ] **Step 4: Extend architecture boundary scan**

Extend `tests/test_chapter_review_form_architecture_boundaries.py` to scan `tests/fixtures/chapter_review_form/` for banned project-specific mechanism terms and to assert every `notes.md` is non-empty.

- [ ] **Step 5: Verify Phase 5 tests pass and runtime target**

```bash
time python3 -m pytest tests/test_chapter_review_form_regression_suite.py tests/test_chapter_review_form_architecture_boundaries.py -q
```

Expected: all selected tests pass in under 10 seconds for the fixture suite.

- [ ] **Step 6: Commit Phase 5**

```bash
git add tests/fixtures/chapter_review_form tests/test_chapter_review_form_regression_suite.py tests/test_chapter_review_form_architecture_boundaries.py
git commit -m "test: complete chapter review form regression fixtures"
```

- [ ] **Step 7: Deploy and live-test Phase 5**

Deploy the fixture-only phase and run one live chapter to confirm no production behavior changes or new review-form system issue appears.

## Phase 6: Plan-Patcher Loop Closure

**Files:**
- Modify: `forwin/canon_quality/chapter_review_form/canon_projector.py`
- Create: `forwin/planning/countdown_drift_pre_audit.py`
- Modify: `forwin/planning/future_plan_audit/auditor.py`
- Modify: `forwin/planning/obligation_pre_audit.py`
- Modify: `forwin/planning/signal_pre_audit.py`
- Modify: `forwin/writer/prompts.py`
- Test: `tests/test_plan_patcher_loop_closure.py`
- Test: `tests/test_writer_prompt_contract.py`
- Test: `tests/test_prompt_regression_samples.py`

- [ ] **Step 1: Add plan-patchable signal red tests**

Create `tests/test_plan_patcher_loop_closure.py` with a projection test asserting `form_countdown_inconsistency`, `form_obligation_unresolved`, `form_open_signal_persisting`, and `form_final_chapter_unresolved` include `payload.plan_patchable is True` and a `patch_kind` from the spec.

- [ ] **Step 2: Verify signal metadata red tests fail**

```bash
python3 -m pytest tests/test_plan_patcher_loop_closure.py::test_form_signals_are_plan_patchable -q
```

Expected: payload keys are missing.

- [ ] **Step 3: Add projector metadata**

Extend `_blocking_signal` calls with:

```python
"plan_patchable": True,
"patch_kind": patch_kind,
"suppression_key": suppression_key,
```

Use patch kinds `countdown_drift`, `obligation_unresolved`, `signal_persisting`, and `final_dangling`. Countdown suppression keys use `countdown:<countdown_key>`.

- [ ] **Step 4: Add countdown drift pre-auditor red test**

Add a test where a chapter N `form_countdown_inconsistency` open signal causes chapter N+1 planning to receive a positive countdown handling task with suppression key `countdown:倒计时甲`.

- [ ] **Step 5: Verify countdown pre-auditor red test fails**

```bash
python3 -m pytest tests/test_plan_patcher_loop_closure.py::test_countdown_drift_signal_creates_next_chapter_plan_patch -q
```

Expected: `forwin.planning.countdown_drift_pre_audit` does not exist.

- [ ] **Step 6: Implement countdown drift pre-auditor**

Create `forwin/planning/countdown_drift_pre_audit.py` with a pure selector that takes open signals and returns patch objects or dictionaries with task text:

```python
def select_countdown_drift_targets(signals: list[Any]) -> list[dict[str, Any]]:
    targets = []
    for signal in signals:
        payload = row_value(signal, "payload", {}) or {}
        if row_value(signal, "signal_type") != "form_countdown_inconsistency":
            continue
        if payload.get("plan_patchable") is not True:
            continue
        countdown_key = str(row_value(signal, "subject_key") or payload.get("countdown_key") or "").strip()
        targets.append(
            {
                "patch_kind": "countdown_drift",
                "suppression_key": f"countdown:{countdown_key}",
                "task": f"本章必须明确处理 {countdown_key} 的当前状态。如继续，必须不大于既有值；如已 closed，不得再次出现正数剩余时间；如确实重新开启，必须显式写出 reopen 事件并命名为新的局部窗口。",
            }
        )
    return targets
```

- [ ] **Step 7: Extend obligation and signal pre-auditors**

Add tests and code so `form_obligation_unresolved` behaves like an urgent obligation and `form_open_signal_persisting` behaves like a stale signal target. Preserve existing selector outputs and append form-derived targets with source metadata.

- [ ] **Step 8: Suppress writer negative constraints**

Add a prompt-contract test where the chapter plan contains suppression key `countdown:倒计时甲`; `_canon_quality_context_section` omits the matching negative countdown constraint and keeps unrelated constraints. Then change `forwin/writer/prompts.py` to read suppression keys from the plan patch payload.

- [ ] **Step 9: Add observability counts**

Add structured log or returned metadata counts: `form_plan_patch_signals_consumed`, `form_prompt_constraints_suppressed`, and `form_prompt_constraints_remaining`.

- [ ] **Step 10: Verify Phase 6 tests pass**

```bash
python3 -m pytest tests/test_plan_patcher_loop_closure.py tests/test_writer_prompt_contract.py tests/test_prompt_regression_samples.py -q
```

Expected: all selected tests pass, and prompt-regression samples show at least a 30 percent reduction in `_canon_quality_context_section` token count for covered constraints.

- [ ] **Step 11: Commit Phase 6**

```bash
git add forwin/canon_quality/chapter_review_form/canon_projector.py forwin/planning/countdown_drift_pre_audit.py forwin/planning/future_plan_audit/auditor.py forwin/planning/obligation_pre_audit.py forwin/planning/signal_pre_audit.py forwin/writer/prompts.py tests/test_plan_patcher_loop_closure.py tests/test_writer_prompt_contract.py tests/test_prompt_regression_samples.py
git commit -m "feat: feed review form drift into planning"
```

- [ ] **Step 12: Deploy and live-test Phase 6**

Run one live chapter. Inspect the next chapter plan and writer prompt inputs through MCP-supported task/chapter state and application logs. Confirm form-derived drift creates a positive plan patch and matching negative prompt constraint is omitted.

## Final Verification

- [ ] Run deleted-module and architecture-boundary tests:

```bash
python3 -m pytest tests/test_chapter_review_form_architecture_boundaries.py tests/test_large_module_boundaries.py -q
```

- [ ] Run all chapter-review-form tests:

```bash
python3 -m pytest tests/test_chapter_review_form_*.py tests/test_form_validators.py tests/test_form_llm_caller.py tests/test_form_builder.py tests/test_canon_projector.py -q
```

- [ ] Run full non-browser suite:

```bash
python3 -m pytest --ignore=tests/browser -q
```

- [ ] Confirm master is clean:

```bash
git status --short --branch
```

- [ ] Summarize per-phase evidence: commit hash, local tests, deployed commit, validation project id, task id, chapter number, review issue summary, and pass/fail conclusion.
