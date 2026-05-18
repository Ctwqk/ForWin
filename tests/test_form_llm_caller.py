from __future__ import annotations

import pytest

from forwin.canon_quality.chapter_review_form import FORM_SCHEMA_VERSION
from forwin.canon_quality.chapter_review_form.errors import ChapterReviewFormUnavailable
from forwin.canon_quality.chapter_review_form.form_schema import ChapterReviewForm
from forwin.canon_quality.chapter_review_form.llm_caller import call_form


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def complete_json(self, **kwargs):  # noqa: ANN001, ANN201
        self.calls.append(kwargs)
        return {
            "project_id": "p1",
            "chapter_number": 1,
            "form_schema_version": FORM_SCHEMA_VERSION,
            "characters": [],
            "countdowns": [],
            "obligations": [],
            "open_signals": [],
            "new_observations": {},
            "chapter_summary": "ok",
        }


def test_call_form_uses_single_structured_json_call() -> None:
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

    answers = call_form(form=form, chapter_text="正文", prior_canon_summary="既有 canon", llm_client=client)

    assert answers.chapter_summary == "ok"
    assert len(client.calls) == 1
    assert client.calls[0]["output_schema"]["title"] == "ChapterReviewAnswers"


def test_call_form_requires_compatible_client() -> None:
    form = ChapterReviewForm(
        project_id="p1",
        chapter_number=1,
        form_schema_version=FORM_SCHEMA_VERSION,
        characters=[],
        countdowns=[],
        obligations=[],
        open_signals=[],
    )

    with pytest.raises(ChapterReviewFormUnavailable):
        call_form(form=form, chapter_text="正文", prior_canon_summary="", llm_client=object())
