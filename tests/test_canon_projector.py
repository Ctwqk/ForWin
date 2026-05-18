from __future__ import annotations

from forwin.canon_quality.chapter_review_form import FORM_SCHEMA_VERSION
from forwin.config import FormBlockingPolicy
from forwin.canon_quality.chapter_review_form.canon_projector import project_validated_answers
from forwin.canon_quality.chapter_review_form.evidence_validator import RejectedAnswer, ValidationReport
from forwin.canon_quality.chapter_review_form.form_schema import (
    CharacterReviewAnswer,
    ChapterReviewAnswers,
    CountdownReviewAnswer,
    FormAnswer,
    NewObservations,
    ObligationReviewAnswer,
)


def test_projector_writes_only_validated_character_death() -> None:
    answer = CharacterReviewAnswer(
        name="林青",
        appears_in_chapter=True,
        life_state=FormAnswer(
            value="dead",
            evidence_quote="林青倒下，再无呼吸。",
            subject_of_quote="林青",
            confidence=0.95,
        ),
        custody_state=FormAnswer(value="unknown"),
        participation=FormAnswer(value="present_acting"),
    )
    answers = _answers(characters=[answer])

    projection = project_validated_answers(
        answers=answers,
        validation_report=ValidationReport(validated=["characters[0].life_state"], rejected=[]),
        draft_id="d1",
        min_blocking_confidence=0.8,
    )

    assert projection.character_transitions[0].character_name == "林青"
    assert projection.character_transitions[0].terminality == "hard_terminal"
    assert projection.character_transitions[0].payload["source"] == "chapter_review_form"
    assert not projection.signals


def test_projector_rejected_answer_does_not_write_state() -> None:
    answer = CharacterReviewAnswer(
        name="林青",
        appears_in_chapter=True,
        life_state=FormAnswer(
            value="dead",
            evidence_quote="家族成员死亡。",
            subject_of_quote="家族成员",
            confidence=0.95,
        ),
        custody_state=FormAnswer(value="unknown"),
        participation=FormAnswer(value="mentioned_only"),
    )

    projection = project_validated_answers(
        answers=_answers(characters=[answer]),
        validation_report=ValidationReport(
            rejected=[RejectedAnswer(path="characters[0].life_state", reason="subject_mismatch", blocking=True)]
        ),
        draft_id="d1",
        min_blocking_confidence=0.8,
    )

    assert projection.character_transitions == []
    assert projection.signals[0].signal_type == "form_answer_rejected"
    assert projection.signals[0].severity == "error"


def test_projector_does_not_write_state_without_evidence() -> None:
    answer = CharacterReviewAnswer(
        name="林青",
        appears_in_chapter=True,
        life_state=FormAnswer(value="dead", confidence=0.95),
        custody_state=FormAnswer(value="unknown"),
        participation=FormAnswer(value="mentioned_only"),
    )

    projection = project_validated_answers(
        answers=_answers(characters=[answer]),
        validation_report=ValidationReport(validated=["characters[0].life_state"], rejected=[]),
        draft_id="d1",
        min_blocking_confidence=0.8,
    )

    assert projection.character_transitions == []


def test_projector_blocks_closed_countdown_regression_without_bridge() -> None:
    countdown = CountdownReviewAnswer(
        key="main",
        mentioned_in_chapter=True,
        status_in_this_chapter=FormAnswer(
            value="reopened",
            evidence_quote="倒计时剩余五十分钟。",
            subject_of_quote="主倒计时",
            confidence=0.92,
        ),
        new_value_minutes=50,
        new_value_evidence=FormAnswer(
            value="50",
            evidence_quote="倒计时剩余五十分钟。",
            subject_of_quote="主倒计时",
            confidence=0.92,
        ),
        consistent_with_prior=FormAnswer(
            value="false",
            evidence_quote="倒计时剩余五十分钟。",
            subject_of_quote="主倒计时",
            confidence=0.92,
        ),
        inconsistency_kind="reopened_after_close",
    )

    projection = project_validated_answers(
        answers=_answers(countdowns=[countdown]),
        validation_report=ValidationReport(
            validated=[
                "countdowns[0].status_in_this_chapter",
                "countdowns[0].new_value_evidence",
                "countdowns[0].consistent_with_prior",
            ],
            rejected=[],
            blocking_paths=["countdowns[0].consistent_with_prior"],
        ),
        draft_id="d1",
        min_blocking_confidence=0.8,
    )

    assert projection.countdown_entries[0].normalized_remaining_minutes == 50
    assert projection.signals[0].signal_type == "form_countdown_inconsistency"
    assert projection.signals[0].severity == "error"


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


def _answers(
    *,
    characters: list[CharacterReviewAnswer] | None = None,
    countdowns: list[CountdownReviewAnswer] | None = None,
) -> ChapterReviewAnswers:
    return ChapterReviewAnswers(
        project_id="p1",
        chapter_number=2,
        form_schema_version=FORM_SCHEMA_VERSION,
        characters=characters or [],
        countdowns=countdowns or [],
        obligations=[],
        open_signals=[],
        new_observations=NewObservations(),
    )
