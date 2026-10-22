from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from forwin.config import FormBlockingPolicy
from forwin.canon_quality.signals import CanonQualitySignal, make_signal_id

from .form_schema import ChapterReviewAnswers, ChapterReviewForm, FormAnswer
from .evidence_validator import RejectedAnswer, ValidationReport


class ProjectionResult(BaseModel):
    signals: list[CanonQualitySignal] = Field(default_factory=list)
    review_issues: list[dict[str, Any]] = Field(default_factory=list)


def project_validated_answers(
    *,
    answers: ChapterReviewAnswers,
    validation_report: ValidationReport,
    form: ChapterReviewForm | None = None,
    draft_id: str = "",
    min_blocking_confidence: float = 0.8,
    blocking_policy: FormBlockingPolicy | None = None,
) -> ProjectionResult:
    result = ProjectionResult()
    policy = blocking_policy or FormBlockingPolicy()
    blocking_paths = set(validation_report.blocking_paths)
    countdowns_by_key = {item.key: item for item in form.countdowns} if form is not None else {}
    for index, rejected in enumerate(validation_report.rejected, start=1):
        result.signals.append(_rejection_signal(answers, rejected, draft_id=draft_id, index=index))

    for index, countdown in enumerate(answers.countdowns):
        status_path = f"countdowns[{index}].status_in_this_chapter"
        consistency_path = f"countdowns[{index}].consistent_with_prior"
        if consistency_path in blocking_paths or status_path in blocking_paths:
            ask = countdowns_by_key.get(countdown.key)
            description = "A countdown is inconsistent with prior canon."
            if ask is not None and ask.scope == "public" and ask.label and ask.label != ask.key:
                description = f"Countdown {ask.label} is inconsistent with prior canon."
            answer = countdown.consistent_with_prior if consistency_path in blocking_paths else countdown.status_in_this_chapter
            answer_path = consistency_path if consistency_path in blocking_paths else status_path
            signal = _blocking_signal(
                answers=answers,
                signal_type="form_countdown_inconsistency",
                subject_key=countdown.key,
                answer=answer,
                answer_path=answer_path,
                description=description,
                draft_id=draft_id,
                index=index + 1,
                severity=_severity_for_answer(
                    policy=policy,
                    signal_type="form_countdown_inconsistency",
                    answer=answer,
                ),
            )
            result.signals.append(signal)
            result.review_issues.append(_issue_from_signal(signal))

    _project_blocking_section_answers(
        result=result,
        answers=answers,
        blocking_paths=blocking_paths,
        draft_id=draft_id,
        policy=policy,
    )
    return result


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
            "value": rejected.value,
            "confidence": rejected.confidence,
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
    severity: str,
    patch_kind: str = "",
    suppression_key: str = "",
) -> CanonQualitySignal:
    plan_patchable = bool(patch_kind and suppression_key)
    payload = {
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
        "plan_patchable": plan_patchable,
        "patch_kind": patch_kind,
        "suppression_key": suppression_key,
    }
    return CanonQualitySignal(
        signal_id=make_signal_id(answers.project_id, answers.chapter_number, signal_type, subject_key, index),
        project_id=answers.project_id,
        chapter_number=answers.chapter_number,
        signal_type=signal_type,
        severity=severity,
        target_scope="chapter",
        subject_key=subject_key,
        description=description,
        evidence_refs=[answer.evidence_quote] if answer.evidence_quote else [],
        payload=payload,
    )


def _project_blocking_section_answers(
    *,
    result: ProjectionResult,
    answers: ChapterReviewAnswers,
    blocking_paths: set[str],
    draft_id: str,
    policy: FormBlockingPolicy,
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
                patch_kind="obligation_unresolved",
                suppression_key=f"obligation:{obligation.id}",
                severity=_severity_for_answer(
                    policy=policy,
                    signal_type="form_obligation_unresolved",
                    answer=obligation.addressed,
                ),
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
                patch_kind="signal_persisting",
                suppression_key=f"signal:{open_signal.id}",
                severity=_severity_for_answer(
                    policy=policy,
                    signal_type="form_open_signal_persisting",
                    answer=open_signal.status,
                ),
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
            patch_kind="final_dangling",
            suppression_key="final:main_crisis",
            severity=_severity_for_answer(
                policy=policy,
                signal_type="form_final_chapter_unresolved",
                answer=answers.final_chapter.main_crisis_status,
            ),
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
