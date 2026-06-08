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


class MissingEnvelopeClient:
    def complete_json(self, **kwargs):  # noqa: ANN001, ANN201
        return {
            "characters": [],
            "countdowns": [],
            "obligations": [],
            "open_signals": [],
            "new_observations": {},
            "chapter_summary": "ok",
        }


class RepairingClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def complete_json(self, **kwargs):  # noqa: ANN001, ANN201
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return {
                "characters": [
                    {
                        "name": "林青",
                        "evidence_quote": "林青站在门口。",
                        "confidence": 1.0,
                    }
                ],
                "countdowns": [],
                "obligations": [],
                "open_signals": [],
                "new_observations": {},
                "chapter_summary": "bad shape",
            }
        return {
            "characters": [
                {
                    "name": "林青",
                    "appears_in_chapter": True,
                    "life_state": {
                        "value": "alive",
                        "evidence_quote": "林青站在门口。",
                        "subject_of_quote": "林青",
                        "confidence": 0.95,
                    },
                    "custody_state": {"value": "free", "evidence_quote": "林青站在门口。", "subject_of_quote": "林青", "confidence": 0.8},
                    "participation": {"value": "present", "evidence_quote": "林青站在门口。", "subject_of_quote": "林青", "confidence": 0.95},
                }
            ],
            "countdowns": [],
            "obligations": [],
            "open_signals": [],
            "new_observations": {},
            "chapter_summary": "ok after repair",
        }


class FlatAnswerClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def complete_json(self, **kwargs):  # noqa: ANN001, ANN201
        self.calls.append(kwargs)
        return {
            "characters": [
                {
                    "name": "林青",
                    "appears_in_chapter": True,
                    "life_state": "alive",
                    "custody_state": "free",
                    "participation": "major",
                    "evidence_quote": "林青站在门口。",
                    "confidence": 0.91,
                }
            ],
            "countdowns": [
                {
                    "key": "main",
                    "mentioned_in_chapter": True,
                    "status_in_this_chapter": "running",
                    "new_value_minutes": 50,
                    "new_value_evidence": "50",
                    "consistent_with_prior": True,
                    "evidence_quote": "倒计时剩余五十分钟。",
                    "confidence": 0.88,
                }
            ],
            "obligations": [],
            "open_signals": [],
            "new_observations": {},
            "chapter_summary": "flat but recoverable",
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


def test_call_form_default_timeout_can_be_overridden_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORWIN_CHAPTER_REVIEW_FORM_TIMEOUT_SECONDS", "123")
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

    call_form(form=form, chapter_text="正文", prior_canon_summary="既有 canon", llm_client=client)

    assert client.calls[0]["timeout_seconds"] == 123.0


def test_call_form_fills_schema_envelope_from_form() -> None:
    form = ChapterReviewForm(
        project_id="p1",
        chapter_number=7,
        form_schema_version=FORM_SCHEMA_VERSION,
        characters=[],
        countdowns=[],
        obligations=[],
        open_signals=[],
    )

    answers = call_form(form=form, chapter_text="正文", prior_canon_summary="", llm_client=MissingEnvelopeClient())

    assert answers.project_id == "p1"
    assert answers.chapter_number == 7
    assert answers.form_schema_version == FORM_SCHEMA_VERSION


def test_call_form_repairs_schema_invalid_payload_once() -> None:
    client = RepairingClient()
    form = ChapterReviewForm(
        project_id="p1",
        chapter_number=7,
        form_schema_version=FORM_SCHEMA_VERSION,
        characters=[],
        countdowns=[],
        obligations=[],
        open_signals=[],
    )

    answers = call_form(form=form, chapter_text="林青站在门口。", prior_canon_summary="", llm_client=client)

    assert answers.chapter_summary == "ok after repair"
    assert answers.characters[0].appears_in_chapter is True
    assert len(client.calls) == 2
    assert "previous JSON did not match" in client.calls[1]["messages"][-1]["content"]


def test_call_form_accepts_flat_form_answer_shapes() -> None:
    client = FlatAnswerClient()
    form = ChapterReviewForm(
        project_id="p1",
        chapter_number=7,
        form_schema_version=FORM_SCHEMA_VERSION,
        characters=[],
        countdowns=[],
        obligations=[],
        open_signals=[],
    )

    answers = call_form(form=form, chapter_text="林青站在门口。倒计时剩余五十分钟。", prior_canon_summary="", llm_client=client)

    assert len(client.calls) == 1
    assert answers.characters[0].life_state.value == "alive"
    assert answers.characters[0].life_state.evidence_quote == "林青站在门口。"
    assert answers.characters[0].life_state.subject_of_quote == "林青"
    assert answers.countdowns[0].consistent_with_prior.value == "true"
    assert answers.countdowns[0].new_value_evidence
    assert answers.countdowns[0].new_value_evidence.value == "50"


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
