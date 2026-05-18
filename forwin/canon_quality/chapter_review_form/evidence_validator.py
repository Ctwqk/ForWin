from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from .form_schema import ChapterReviewAnswers, ChapterReviewForm, FormAnswer


PRONOUN_SUBJECTS = {
    "他",
    "她",
    "它",
    "他们",
    "她们",
    "它们",
    "其",
    "此人",
    "那人",
    "对方",
}

CHARACTER_BLOCKING_VALUES = {"dead", "wounded", "captured"}
COUNTDOWN_BLOCKING_VALUES = {"false", "reset", "reopened", "advanced"}
OBLIGATION_BLOCKING_VALUES = {"unaddressed", "partial"}
SIGNAL_BLOCKING_VALUES = {"persisting", "worsened"}
FINAL_BLOCKING_VALUES = {"left_dangling", "denied_or_avoided"}


class RejectedAnswer(BaseModel):
    path: str
    reason: str
    message: str = ""
    blocking: bool = False


class ValidationReport(BaseModel):
    validated: list[str] = Field(default_factory=list)
    rejected: list[RejectedAnswer] = Field(default_factory=list)
    blocking_paths: list[str] = Field(default_factory=list)

    @property
    def has_rejections(self) -> bool:
        return bool(self.rejected)


def validate_answers(
    *,
    form: ChapterReviewForm,
    answers: ChapterReviewAnswers,
    chapter_text: str,
    min_blocking_confidence: float = 0.8,
) -> ValidationReport:
    report = ValidationReport()
    known_character_subjects = {
        ask.name: {ask.name, *[alias for alias in ask.aliases if alias]}
        for ask in form.characters
    }
    chapter_norm = _normalize_text(chapter_text)

    for index, answer in enumerate(answers.characters):
        allowed_subjects = known_character_subjects.get(answer.name, {answer.name})
        _validate_form_answer(
            report=report,
            path=f"characters[{index}].life_state",
            answer=answer.life_state,
            chapter_norm=chapter_norm,
            allowed_subjects=allowed_subjects,
            binding_values=CHARACTER_BLOCKING_VALUES | {"alive"},
            blocking_values=CHARACTER_BLOCKING_VALUES,
            min_blocking_confidence=min_blocking_confidence,
        )
        _validate_form_answer(
            report=report,
            path=f"characters[{index}].custody_state",
            answer=answer.custody_state,
            chapter_norm=chapter_norm,
            allowed_subjects=allowed_subjects,
            binding_values={"free", "captured"},
            blocking_values={"captured"},
            min_blocking_confidence=min_blocking_confidence,
        )
        _validate_form_answer(
            report=report,
            path=f"characters[{index}].participation",
            answer=answer.participation,
            chapter_norm=chapter_norm,
            allowed_subjects=allowed_subjects,
            binding_values={"present_acting", "mentioned_only"},
            blocking_values=set(),
            min_blocking_confidence=min_blocking_confidence,
        )
        for event_index, event in enumerate(answer.bridge_events):
            _validate_quote_only(
                report=report,
                path=f"characters[{index}].bridge_events[{event_index}]",
                quote=event.evidence_quote,
                chapter_norm=chapter_norm,
                min_blocking_confidence=min_blocking_confidence,
                confidence=event.confidence,
            )

    for index, answer in enumerate(answers.countdowns):
        _validate_form_answer(
            report=report,
            path=f"countdowns[{index}].status_in_this_chapter",
            answer=answer.status_in_this_chapter,
            chapter_norm=chapter_norm,
            blocking_values=COUNTDOWN_BLOCKING_VALUES,
            min_blocking_confidence=min_blocking_confidence,
        )
        if answer.new_value_evidence is not None:
            _validate_form_answer(
                report=report,
                path=f"countdowns[{index}].new_value_evidence",
                answer=answer.new_value_evidence,
                chapter_norm=chapter_norm,
                min_blocking_confidence=min_blocking_confidence,
            )
        _validate_form_answer(
            report=report,
            path=f"countdowns[{index}].consistent_with_prior",
            answer=answer.consistent_with_prior,
            chapter_norm=chapter_norm,
            blocking_values={"false"},
            min_blocking_confidence=min_blocking_confidence,
        )

    for index, answer in enumerate(answers.obligations):
        _validate_form_answer(
            report=report,
            path=f"obligations[{index}].addressed",
            answer=answer.addressed,
            chapter_norm=chapter_norm,
            blocking_values=OBLIGATION_BLOCKING_VALUES,
            min_blocking_confidence=min_blocking_confidence,
        )
        if answer.payoff_evidence is not None:
            _validate_form_answer(
                report=report,
                path=f"obligations[{index}].payoff_evidence",
                answer=answer.payoff_evidence,
                chapter_norm=chapter_norm,
                min_blocking_confidence=min_blocking_confidence,
            )

    for index, answer in enumerate(answers.open_signals):
        _validate_form_answer(
            report=report,
            path=f"open_signals[{index}].status",
            answer=answer.status,
            chapter_norm=chapter_norm,
            blocking_values=SIGNAL_BLOCKING_VALUES,
            min_blocking_confidence=min_blocking_confidence,
        )
        if answer.resolution_evidence is not None:
            _validate_form_answer(
                report=report,
                path=f"open_signals[{index}].resolution_evidence",
                answer=answer.resolution_evidence,
                chapter_norm=chapter_norm,
                min_blocking_confidence=min_blocking_confidence,
            )

    _validate_observation_quotes(report, answers.new_observations.model_dump(mode="json"), chapter_norm)
    if answers.final_chapter is not None:
        _validate_form_answer(
            report=report,
            path="final_chapter.main_crisis_status",
            answer=answers.final_chapter.main_crisis_status,
            chapter_norm=chapter_norm,
            blocking_values=FINAL_BLOCKING_VALUES,
            min_blocking_confidence=min_blocking_confidence,
        )
        if answers.final_chapter.closure_evidence is not None:
            _validate_form_answer(
                report=report,
                path="final_chapter.closure_evidence",
                answer=answers.final_chapter.closure_evidence,
                chapter_norm=chapter_norm,
                min_blocking_confidence=min_blocking_confidence,
            )
    return report


def _validate_form_answer(
    *,
    report: ValidationReport,
    path: str,
    answer: FormAnswer,
    chapter_norm: str,
    allowed_subjects: set[str] | None = None,
    binding_values: set[str] | None = None,
    blocking_values: set[str] | None = None,
    min_blocking_confidence: float,
) -> None:
    quote = answer.evidence_quote.strip()
    value = str(answer.value or "").strip()
    blocking = value in (blocking_values or set()) and answer.is_bindable(min_blocking_confidence)
    if quote:
        if _normalize_text(quote) not in chapter_norm:
            report.rejected.append(
                RejectedAnswer(path=path, reason="quote_not_found", message="evidence_quote is absent", blocking=blocking)
            )
            return
    if allowed_subjects is not None and value in (binding_values or set()) and quote:
        subject = answer.subject_of_quote.strip()
        if subject in PRONOUN_SUBJECTS:
            report.rejected.append(
                RejectedAnswer(path=path, reason="pronoun_subject", message="subject_of_quote is a pronoun", blocking=blocking)
            )
            return
        if subject not in allowed_subjects:
            report.rejected.append(
                RejectedAnswer(
                    path=path,
                    reason="subject_mismatch",
                    message="subject_of_quote does not match judged entity",
                    blocking=blocking,
                )
            )
            return
    report.validated.append(path)
    if blocking:
        report.blocking_paths.append(path)


def _validate_quote_only(
    *,
    report: ValidationReport,
    path: str,
    quote: str,
    chapter_norm: str,
    confidence: float,
    min_blocking_confidence: float,
) -> None:
    if quote and _normalize_text(quote) in chapter_norm:
        report.validated.append(path)
        return
    report.rejected.append(
        RejectedAnswer(
            path=path,
            reason="quote_not_found",
            message="bridge event evidence_quote is absent",
            blocking=confidence >= min_blocking_confidence,
        )
    )


def _validate_observation_quotes(report: ValidationReport, payload: Any, chapter_norm: str, path: str = "new_observations") -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            child_path = f"{path}.{key}"
            if key.endswith("_quote") and isinstance(value, str) and value.strip():
                if _normalize_text(value) not in chapter_norm:
                    report.rejected.append(
                        RejectedAnswer(path=child_path, reason="quote_not_found", message="observation quote is absent")
                    )
                else:
                    report.validated.append(child_path)
                continue
            _validate_observation_quotes(report, value, chapter_norm, child_path)
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            _validate_observation_quotes(report, item, chapter_norm, f"{path}[{index}]")


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or ""))
