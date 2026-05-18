from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from forwin.canon_quality.repository import CanonQualityRepository
from forwin.canon_quality.signals import (
    CanonQualitySignal,
    CharacterStateTransition,
    CountdownLedgerEntry,
    make_signal_id,
)
from forwin.models.project import Project
from forwin.protocol.writer import WriterOutput

from . import FORM_SCHEMA_VERSION
from .canon_projector import ProjectionResult, project_validated_answers
from .errors import ChapterReviewFormSchemaInvalid, ChapterReviewFormUnavailable
from .evidence_validator import ValidationReport, validate_answers
from .form_builder import build_form
from .form_schema import ChapterReviewAnswers, ChapterReviewForm
from .llm_caller import call_form


class ChapterReviewFormResult(BaseModel):
    project_id: str
    chapter_number: int
    draft_id: str = ""
    mode: str = "chapter_review_form"
    form: ChapterReviewForm | None = None
    answers: ChapterReviewAnswers | None = None
    validation_report: ValidationReport = Field(default_factory=ValidationReport)
    signals: list[CanonQualitySignal] = Field(default_factory=list)
    character_transitions: list[CharacterStateTransition] = Field(default_factory=list)
    countdown_entries: list[CountdownLedgerEntry] = Field(default_factory=list)
    review_issues: list[dict[str, Any]] = Field(default_factory=list)
    raw_analyzer_results: list[dict[str, Any]] = Field(default_factory=list)
    blocking: bool = False
    confidence: float = 0.0
    summary: str = ""


def review_chapter_with_form(
    *,
    session: Session | None,
    project_id: str,
    chapter_number: int,
    writer_output: WriterOutput,
    draft_id: str = "",
    llm_client: object | None,
    character_rows: list[Any] | None = None,
    countdown_rows: list[Any] | None = None,
    open_signal_rows: list[Any] | None = None,
    obligations: list[Any] | None = None,
    target_total_chapters: int = 0,
    min_blocking_confidence: float = 0.8,
    token_budget_chars: int = 8000,
) -> ChapterReviewFormResult:
    repo = CanonQualityRepository(session) if session is not None else None
    project = session.get(Project, project_id) if session is not None else None
    resolved_target_total = int(target_total_chapters or getattr(project, "target_total_chapters", 0) or 0)
    body = str(writer_output.body or "")
    form = build_form(
        project_id=project_id,
        chapter_number=chapter_number,
        chapter_text=body,
        character_rows=character_rows if character_rows is not None else _safe_list(repo, "list_character_transitions", project_id, before_chapter=chapter_number),
        countdown_rows=countdown_rows if countdown_rows is not None else _safe_list(repo, "list_countdown_entries", project_id, before_chapter=chapter_number, include_details=True),
        open_signal_rows=open_signal_rows if open_signal_rows is not None else _safe_list(repo, "list_open_signals", project_id, before_chapter=chapter_number, limit=20),
        obligations=obligations or [],
        target_total_chapters=resolved_target_total,
        token_budget_chars=token_budget_chars,
    )
    if llm_client is None:
        return _failure_result(
            project_id=project_id,
            chapter_number=chapter_number,
            draft_id=draft_id,
            form=form,
            signal_type="form_llm_unavailable",
            reason="No LLM client configured for chapter review form.",
        )
    try:
        answers = call_form(
            form=form,
            chapter_text=body,
            prior_canon_summary=_prior_canon_summary(form),
            llm_client=llm_client,
        )
    except ChapterReviewFormSchemaInvalid as exc:
        return _failure_result(
            project_id=project_id,
            chapter_number=chapter_number,
            draft_id=draft_id,
            form=form,
            signal_type="form_schema_invalid",
            reason=str(exc),
        )
    except ChapterReviewFormUnavailable as exc:
        return _failure_result(
            project_id=project_id,
            chapter_number=chapter_number,
            draft_id=draft_id,
            form=form,
            signal_type="form_llm_unavailable",
            reason=str(exc),
        )

    validation_report = validate_answers(
        form=form,
        answers=answers,
        chapter_text=body,
        min_blocking_confidence=min_blocking_confidence,
    )
    projection: ProjectionResult = project_validated_answers(
        answers=answers,
        validation_report=validation_report,
        draft_id=draft_id,
        min_blocking_confidence=min_blocking_confidence,
    )
    signals = list(projection.signals)
    return ChapterReviewFormResult(
        project_id=project_id,
        chapter_number=chapter_number,
        draft_id=draft_id,
        form=form,
        answers=answers,
        validation_report=validation_report,
        signals=signals,
        character_transitions=projection.character_transitions,
        countdown_entries=projection.countdown_entries,
        review_issues=projection.review_issues,
        raw_analyzer_results=[_raw_result(answers=answers, validation_report=validation_report, projection=projection)],
        blocking=any(signal.severity == "error" and signal.status == "open" for signal in signals),
        confidence=_max_confidence(answers),
        summary=answers.chapter_summary,
    )


def _failure_result(
    *,
    project_id: str,
    chapter_number: int,
    draft_id: str,
    form: ChapterReviewForm,
    signal_type: str,
    reason: str,
) -> ChapterReviewFormResult:
    signal = CanonQualitySignal(
        signal_id=make_signal_id(project_id, chapter_number, signal_type, "chapter_review_form"),
        project_id=project_id,
        chapter_number=chapter_number,
        signal_type=signal_type,
        severity="error",
        target_scope="chapter",
        subject_key="chapter_review_form",
        description=reason or signal_type,
        payload={
            "source_layer": "canon_quality",
            "source_mode": "chapter_review_form",
            "source": "chapter_review_form",
            "form_schema_version": FORM_SCHEMA_VERSION,
            "validation_status": "unverified",
            "blocking_origin": "chapter_review_form",
            "draft_id": draft_id,
            "reason": reason,
        },
    )
    issue = {
        "issue_id": signal.signal_id,
        "rule_name": signal.signal_type,
        "type": signal.signal_type,
        "severity": "error",
        "description": signal.description,
        "source_layer": "canon_quality",
        "source_mode": "chapter_review_form",
        "source_analyzer": "ChapterReviewForm",
        "form_schema_version": FORM_SCHEMA_VERSION,
        "validation_status": "unverified",
        "blocking_origin": "chapter_review_form",
    }
    return ChapterReviewFormResult(
        project_id=project_id,
        chapter_number=chapter_number,
        draft_id=draft_id,
        form=form,
        signals=[signal],
        review_issues=[issue],
        raw_analyzer_results=[
            {
                "analyzer": "ChapterReviewForm",
                "verdict": "fail",
                "blocking": True,
                "confidence": 1.0,
                "summary": reason,
                "issues": [issue],
                "metadata": signal.payload,
            }
        ],
        blocking=True,
        confidence=1.0,
        summary=reason,
    )


def _safe_list(repo: CanonQualityRepository | None, method_name: str, *args: Any, **kwargs: Any) -> list[Any]:
    if repo is None:
        return []
    method = getattr(repo, method_name)
    return list(method(*args, **kwargs))


def _prior_canon_summary(form: ChapterReviewForm) -> str:
    return json.dumps(
        {
            "characters": [item.model_dump(mode="json") for item in form.characters],
            "countdowns": [item.model_dump(mode="json") for item in form.countdowns],
            "obligations": [item.model_dump(mode="json") for item in form.obligations],
            "open_signals": [item.model_dump(mode="json") for item in form.open_signals],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _raw_result(
    *,
    answers: ChapterReviewAnswers,
    validation_report: ValidationReport,
    projection: ProjectionResult,
) -> dict[str, Any]:
    return {
        "analyzer": "ChapterReviewForm",
        "version": FORM_SCHEMA_VERSION,
        "verdict": "fail" if projection.signals else "pass",
        "blocking": any(signal.severity == "error" for signal in projection.signals),
        "confidence": _max_confidence(answers),
        "summary": answers.chapter_summary,
        "issues": list(projection.review_issues),
        "accepted_facts": [],
        "uncertainties": [],
        "metadata": {
            "source_layer": "canon_quality",
            "source_mode": "chapter_review_form",
            "form_schema_version": answers.form_schema_version,
            "validated_paths": list(validation_report.validated),
            "rejected_paths": [item.model_dump(mode="json") for item in validation_report.rejected],
        },
    }


def _max_confidence(answers: ChapterReviewAnswers) -> float:
    values: list[float] = []
    for character in answers.characters:
        values.extend([character.life_state.confidence, character.custody_state.confidence, character.participation.confidence])
    for countdown in answers.countdowns:
        values.extend([countdown.status_in_this_chapter.confidence, countdown.consistent_with_prior.confidence])
        if countdown.new_value_evidence is not None:
            values.append(countdown.new_value_evidence.confidence)
    for obligation in answers.obligations:
        values.append(obligation.addressed.confidence)
        if obligation.payoff_evidence is not None:
            values.append(obligation.payoff_evidence.confidence)
    for signal in answers.open_signals:
        values.append(signal.status.confidence)
        if signal.resolution_evidence is not None:
            values.append(signal.resolution_evidence.confidence)
    if answers.final_chapter is not None:
        values.append(answers.final_chapter.main_crisis_status.confidence)
        if answers.final_chapter.closure_evidence is not None:
            values.append(answers.final_chapter.closure_evidence.confidence)
    return max(values or [0.0])
