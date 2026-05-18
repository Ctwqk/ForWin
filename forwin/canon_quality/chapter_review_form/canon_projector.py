from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from forwin.canon_quality.signals import (
    CanonQualitySignal,
    CharacterStateTransition,
    CountdownLedgerEntry,
    make_signal_id,
)

from .form_schema import ChapterReviewAnswers, FormAnswer
from .evidence_validator import RejectedAnswer, ValidationReport


class ProjectionResult(BaseModel):
    signals: list[CanonQualitySignal] = Field(default_factory=list)
    character_transitions: list[CharacterStateTransition] = Field(default_factory=list)
    countdown_entries: list[CountdownLedgerEntry] = Field(default_factory=list)
    review_issues: list[dict[str, Any]] = Field(default_factory=list)


def project_validated_answers(
    *,
    answers: ChapterReviewAnswers,
    validation_report: ValidationReport,
    draft_id: str = "",
    min_blocking_confidence: float = 0.8,
) -> ProjectionResult:
    result = ProjectionResult()
    validated = set(validation_report.validated)
    blocking_paths = set(validation_report.blocking_paths)
    for index, rejected in enumerate(validation_report.rejected, start=1):
        result.signals.append(_rejection_signal(answers, rejected, draft_id=draft_id, index=index))

    for index, character in enumerate(answers.characters):
        life_path = f"characters[{index}].life_state"
        if life_path in validated and character.life_state.value in {"alive", "wounded", "dead"}:
            result.character_transitions.append(
                _character_transition(
                    answers=answers,
                    name=character.name,
                    answer=character.life_state,
                    transition_type="life_state",
                    draft_id=draft_id,
                )
            )
        custody_path = f"characters[{index}].custody_state"
        if custody_path in validated and character.custody_state.value in {"free", "captured"}:
            result.character_transitions.append(
                _character_transition(
                    answers=answers,
                    name=character.name,
                    answer=character.custody_state,
                    transition_type="custody_state",
                    draft_id=draft_id,
                )
            )

    for index, countdown in enumerate(answers.countdowns):
        status_path = f"countdowns[{index}].status_in_this_chapter"
        value_path = f"countdowns[{index}].new_value_evidence"
        consistency_path = f"countdowns[{index}].consistent_with_prior"
        if countdown.new_value_minutes is not None and (status_path in validated or value_path in validated):
            result.countdown_entries.append(
                CountdownLedgerEntry(
                    project_id=answers.project_id,
                    countdown_key=countdown.key,
                    label=countdown.key,
                    chapter_number=answers.chapter_number,
                    normalized_remaining_minutes=int(countdown.new_value_minutes),
                    raw_mention=_evidence_quote(countdown.new_value_evidence) or _evidence_quote(countdown.status_in_this_chapter),
                    is_reset_event=countdown.status_in_this_chapter.value in {"reset", "reopened"},
                    is_resolution_event=countdown.status_in_this_chapter.value in {"fulfilled", "closed"},
                    status="conflict" if countdown.consistent_with_prior.value == "false" else "consistent",
                    evidence_refs=[_evidence_quote(countdown.new_value_evidence) or _evidence_quote(countdown.status_in_this_chapter)],
                    payload={
                        "source": "chapter_review_form",
                        "form_schema_version": answers.form_schema_version,
                        "draft_id": draft_id,
                        "answer_path": status_path,
                        "inconsistency_kind": countdown.inconsistency_kind,
                    },
                )
            )
        if consistency_path in blocking_paths:
            signal = _blocking_signal(
                answers=answers,
                signal_type="form_countdown_inconsistency",
                subject_key=countdown.key,
                answer=countdown.consistent_with_prior,
                answer_path=consistency_path,
                description=f"Countdown {countdown.key} is inconsistent with prior canon.",
                draft_id=draft_id,
                index=index + 1,
            )
            result.signals.append(signal)
            result.review_issues.append(_issue_from_signal(signal))

    _project_blocking_section_answers(
        result=result,
        answers=answers,
        blocking_paths=blocking_paths,
        draft_id=draft_id,
    )
    return result


def _character_transition(
    *,
    answers: ChapterReviewAnswers,
    name: str,
    answer: FormAnswer,
    transition_type: str,
    draft_id: str,
) -> CharacterStateTransition:
    to_state = str(answer.value or "unknown")
    terminality = "hard_terminal" if to_state == "dead" else "none"
    return CharacterStateTransition(
        project_id=answers.project_id,
        character_name=name,
        chapter_number=answers.chapter_number,
        transition_type=transition_type,
        to_state=to_state,
        terminality=terminality,
        can_participate=to_state != "dead",
        evidence_refs=[answer.evidence_quote] if answer.evidence_quote else [],
        payload={
            "source": "chapter_review_form",
            "form_schema_version": answers.form_schema_version,
            "draft_id": draft_id,
            "confidence": answer.confidence,
            "evidence_quote": answer.evidence_quote,
            "subject_of_quote": answer.subject_of_quote,
            "explanation": answer.explanation,
        },
    )


def _rejection_signal(
    answers: ChapterReviewAnswers,
    rejected: RejectedAnswer,
    *,
    draft_id: str,
    index: int,
) -> CanonQualitySignal:
    return CanonQualitySignal(
        signal_id=make_signal_id(answers.project_id, answers.chapter_number, "form_answer_rejected", rejected.path, index),
        project_id=answers.project_id,
        chapter_number=answers.chapter_number,
        signal_type="form_answer_rejected",
        severity="error" if rejected.blocking else "warning",
        target_scope="chapter",
        subject_key=rejected.path,
        description=f"Chapter review form answer rejected: {rejected.reason}",
        payload={
            "source": "chapter_review_form",
            "form_schema_version": answers.form_schema_version,
            "draft_id": draft_id,
            "answer_path": rejected.path,
            "validation_status": "rejected",
            "reason": rejected.reason,
            "message": rejected.message,
        },
    )


def _blocking_signal(
    *,
    answers: ChapterReviewAnswers,
    signal_type: str,
    subject_key: str,
    answer: FormAnswer,
    answer_path: str,
    description: str,
    draft_id: str,
    index: int,
) -> CanonQualitySignal:
    return CanonQualitySignal(
        signal_id=make_signal_id(answers.project_id, answers.chapter_number, signal_type, subject_key, index),
        project_id=answers.project_id,
        chapter_number=answers.chapter_number,
        signal_type=signal_type,
        severity="error",
        target_scope="chapter",
        subject_key=subject_key,
        description=description,
        evidence_refs=[answer.evidence_quote] if answer.evidence_quote else [],
        payload={
            "source_layer": "canon_quality",
            "source_mode": "chapter_review_form",
            "source": "chapter_review_form",
            "form_schema_version": answers.form_schema_version,
            "answer_path": answer_path,
            "validation_status": "validated",
            "evidence_quote": answer.evidence_quote,
            "subject_of_quote": answer.subject_of_quote,
            "original_verdict": answer.value,
            "original_confidence": answer.confidence,
            "blocking_origin": "chapter_review_form",
            "draft_id": draft_id,
        },
    )


def _project_blocking_section_answers(
    *,
    result: ProjectionResult,
    answers: ChapterReviewAnswers,
    blocking_paths: set[str],
    draft_id: str,
) -> None:
    for index, obligation in enumerate(answers.obligations):
        path = f"obligations[{index}].addressed"
        if path in blocking_paths:
            signal = _blocking_signal(
                answers=answers,
                signal_type="form_obligation_unresolved",
                subject_key=obligation.id,
                answer=obligation.addressed,
                answer_path=path,
                description=f"Obligation {obligation.id} is unresolved.",
                draft_id=draft_id,
                index=index + 1,
            )
            result.signals.append(signal)
            result.review_issues.append(_issue_from_signal(signal))
    for index, open_signal in enumerate(answers.open_signals):
        path = f"open_signals[{index}].status"
        if path in blocking_paths:
            signal = _blocking_signal(
                answers=answers,
                signal_type="form_open_signal_persisting",
                subject_key=open_signal.id,
                answer=open_signal.status,
                answer_path=path,
                description=f"Open signal {open_signal.id} is still unresolved.",
                draft_id=draft_id,
                index=index + 1,
            )
            result.signals.append(signal)
            result.review_issues.append(_issue_from_signal(signal))
    if "final_chapter.main_crisis_status" in blocking_paths and answers.final_chapter is not None:
        signal = _blocking_signal(
            answers=answers,
            signal_type="form_final_chapter_unresolved",
            subject_key="final_chapter",
            answer=answers.final_chapter.main_crisis_status,
            answer_path="final_chapter.main_crisis_status",
            description="Final chapter did not close the main crisis.",
            draft_id=draft_id,
            index=1,
        )
        result.signals.append(signal)
        result.review_issues.append(_issue_from_signal(signal))


def _issue_from_signal(signal: CanonQualitySignal) -> dict[str, Any]:
    payload = dict(signal.payload or {})
    return {
        "issue_id": signal.signal_id,
        "rule_name": signal.signal_type,
        "type": signal.signal_type,
        "severity": signal.severity,
        "description": signal.description,
        "source_layer": payload.get("source_layer", "canon_quality"),
        "source_mode": payload.get("source_mode", "chapter_review_form"),
        "source_analyzer": "ChapterReviewForm",
        "form_schema_version": payload.get("form_schema_version", ""),
        "answer_path": payload.get("answer_path", ""),
        "validation_status": payload.get("validation_status", ""),
        "evidence_quote": payload.get("evidence_quote", ""),
        "subject_of_quote": payload.get("subject_of_quote", ""),
        "blocking_origin": payload.get("blocking_origin", "chapter_review_form"),
        "confidence": payload.get("original_confidence", 0.0),
    }


def _evidence_quote(answer: FormAnswer | None) -> str:
    return str(getattr(answer, "evidence_quote", "") or "")
