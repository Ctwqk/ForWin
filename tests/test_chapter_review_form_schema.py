from __future__ import annotations

from forwin.canon_quality.chapter_review_form import FORM_SCHEMA_VERSION
from forwin.canon_quality.chapter_review_form.form_schema import ChapterReviewAnswers, FormAnswer


def test_form_answer_requires_quote_to_bind() -> None:
    assert FORM_SCHEMA_VERSION
    assert FormAnswer(value="dead", confidence=0.99).is_bindable(0.8) is False
    assert (
        FormAnswer(
            value="dead",
            evidence_quote="林青倒下",
            subject_of_quote="林青",
            confidence=0.99,
        ).is_bindable(0.8)
        is True
    )


def test_answers_round_trip_with_sections() -> None:
    answers = ChapterReviewAnswers.model_validate(
        {
            "project_id": "p1",
            "chapter_number": 3,
            "form_schema_version": FORM_SCHEMA_VERSION,
            "characters": [],
            "countdowns": [],
            "obligations": [],
            "open_signals": [],
            "new_observations": {},
        }
    )

    assert answers.model_dump(mode="json")["project_id"] == "p1"
