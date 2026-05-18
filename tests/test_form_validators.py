from __future__ import annotations

from forwin.canon_quality.chapter_review_form import FORM_SCHEMA_VERSION
from forwin.canon_quality.chapter_review_form.evidence_validator import validate_answers
from forwin.canon_quality.chapter_review_form.form_schema import (
    CharacterReviewAnswer,
    CharacterReviewAsk,
    ChapterReviewAnswers,
    ChapterReviewForm,
    FormAnswer,
    NewObservations,
)


def test_validator_rejects_quote_not_in_chapter() -> None:
    form = _form_for_character("林青")
    answers = _answers_for_character(
        "林青",
        life_state=FormAnswer(
            value="dead",
            evidence_quote="林青死亡",
            subject_of_quote="林青",
            confidence=0.99,
        ),
    )

    report = validate_answers(form=form, answers=answers, chapter_text="林青只是回头看了一眼。")

    assert report.rejected
    assert report.rejected[0].reason == "quote_not_found"


def test_validator_rejects_group_subject_for_singleton_state() -> None:
    form = _form_for_character("林青")
    quote = "林青和委员会高层的合谋导致家族成员死亡。"
    answers = _answers_for_character(
        "林青",
        life_state=FormAnswer(
            value="dead",
            evidence_quote=quote,
            subject_of_quote="家族成员",
            confidence=0.99,
        ),
        participation=FormAnswer(value="mentioned_only"),
    )

    report = validate_answers(form=form, answers=answers, chapter_text=quote)

    assert report.rejected
    assert report.rejected[0].reason == "subject_mismatch"


def test_validator_degrades_low_confidence_blocking_answer() -> None:
    form = _form_for_character("林青")
    quote = "林青倒下，再无呼吸。"
    answers = _answers_for_character(
        "林青",
        life_state=FormAnswer(
            value="dead",
            evidence_quote=quote,
            subject_of_quote="林青",
            confidence=0.4,
        ),
    )

    report = validate_answers(form=form, answers=answers, chapter_text=quote, min_blocking_confidence=0.8)

    assert "characters[0].life_state" in report.validated
    assert report.blocking_paths == []


def _form_for_character(name: str) -> ChapterReviewForm:
    return ChapterReviewForm(
        project_id="p1",
        chapter_number=1,
        form_schema_version=FORM_SCHEMA_VERSION,
        characters=[
            CharacterReviewAsk(
                name=name,
                aliases=[],
                prior_life_state="alive",
                prior_custody_state="free",
                last_seen_chapter=0,
            )
        ],
        countdowns=[],
        obligations=[],
        open_signals=[],
    )


def _answers_for_character(
    name: str,
    *,
    life_state: FormAnswer,
    custody_state: FormAnswer | None = None,
    participation: FormAnswer | None = None,
) -> ChapterReviewAnswers:
    return ChapterReviewAnswers(
        project_id="p1",
        chapter_number=1,
        form_schema_version=FORM_SCHEMA_VERSION,
        characters=[
            CharacterReviewAnswer(
                name=name,
                appears_in_chapter=True,
                life_state=life_state,
                custody_state=custody_state or FormAnswer(value="free"),
                participation=participation or FormAnswer(value="present_acting"),
            )
        ],
        countdowns=[],
        obligations=[],
        open_signals=[],
        new_observations=NewObservations(),
    )
