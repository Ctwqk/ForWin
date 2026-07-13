from __future__ import annotations

import pytest

from forwin.canon_quality.chapter_review_form import FORM_SCHEMA_VERSION
from forwin.canon_quality.chapter_review_form.errors import ChapterReviewFormUnavailable
from forwin.canon_quality.chapter_review_form.form_schema import (
    ChapterReviewForm,
    CharacterReviewAsk,
    CountdownReviewAsk,
)
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


class RepairingUnaskedClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def complete_json(self, **kwargs):  # noqa: ANN001, ANN201
        self.calls.append(kwargs)
        if len(self.calls) > 1:
            return {
                "characters": [],
                "countdowns": [],
                "obligations": [],
                "open_signals": [],
                "new_observations": {},
                "chapter_summary": "tracked noise repaired",
            }
        return {
            "characters": [{"name": "伪角色", "unexpected": "malformed"}],
            "countdowns": [{"key": "伪倒计时", "unexpected": "malformed"}],
            "obligations": [{"id": "伪义务", "unexpected": "malformed"}],
            "open_signals": [{"id": "伪信号", "unexpected": "malformed"}],
            "new_observations": {},
            "chapter_summary": "tracked noise removed",
        }


class EvidenceRepairingClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def complete_json(self, **kwargs):  # noqa: ANN001, ANN201
        self.calls.append(kwargs)
        quote = "墙上的规则要求三分钟内提交一致性标记。"
        if len(self.calls) > 1:
            quote = "三分钟内未提交一致性标记，触发迟滞罚则。"
        return {
            "characters": [],
            "countdowns": [],
            "obligations": [],
            "open_signals": [],
            "new_observations": {
                "new_world_facts": [
                    {
                        "fact": "一致性标记有三分钟提交时限。",
                        "evidence_quote": quote,
                        "category": "rule",
                    }
                ]
            },
            "chapter_summary": "规则首次出现。",
        }


class AttemptRepairingClient(RepairingClient):
    def __init__(self) -> None:
        super().__init__()
        self.llm_attempt_events: list[dict[str, object]] = []

    def complete_json(self, **kwargs):  # noqa: ANN001, ANN201
        self.llm_attempt_events.append(
            {
                "attempt_group_id": f"schema-call-{len(self.calls) + 1}",
                "attempt_no": 1,
                "stage_key": "chapter_review_form",
                "http_status": 200,
                "output_chars": 20,
            }
        )
        return super().complete_json(**kwargs)


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
        characters=[
            CharacterReviewAsk(
                name="林青",
                prior_life_state="alive",
                prior_custody_state="free",
                last_seen_chapter=6,
            )
        ],
        countdowns=[],
        obligations=[],
        open_signals=[],
    )

    answers = call_form(form=form, chapter_text="林青站在门口。", prior_canon_summary="", llm_client=client)

    assert answers.chapter_summary == "ok after repair"
    assert answers.characters[0].appears_in_chapter is True
    assert len(client.calls) == 2
    assert "previous JSON did not satisfy" in client.calls[1]["messages"][-1]["content"]


def test_call_form_marks_schema_failure_and_cross_call_workflow_retry() -> None:
    client = AttemptRepairingClient()
    form = ChapterReviewForm(
        project_id="p1",
        chapter_number=7,
        form_schema_version=FORM_SCHEMA_VERSION,
        characters=[
            CharacterReviewAsk(
                name="林青",
                prior_life_state="alive",
                prior_custody_state="free",
                last_seen_chapter=6,
            )
        ],
        countdowns=[],
        obligations=[],
        open_signals=[],
    )

    call_form(
        form=form,
        chapter_text="林青站在门口。",
        prior_canon_summary="",
        llm_client=client,
    )

    first, second = client.llm_attempt_events
    assert first["workflow_attempt_no"] == 1
    assert first["workflow_retry"] is False
    assert first["parse_error"]
    assert second["workflow_attempt_no"] == 2
    assert second["workflow_retry"] is True
    assert not second.get("parse_error")


def test_call_form_accepts_flat_form_answer_shapes() -> None:
    client = FlatAnswerClient()
    form = ChapterReviewForm(
        project_id="p1",
        chapter_number=7,
        form_schema_version=FORM_SCHEMA_VERSION,
        characters=[
            CharacterReviewAsk(
                name="林青",
                prior_life_state="alive",
                prior_custody_state="free",
                last_seen_chapter=6,
            )
        ],
        countdowns=[
            CountdownReviewAsk(
                key="main",
                label="主倒计时",
                prior_value_minutes=60,
                prior_status="active",
                last_updated_chapter=6,
            )
        ],
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


def test_call_form_repairs_unasked_tracked_items_instead_of_discarding_them() -> None:
    client = RepairingUnaskedClient()
    form = ChapterReviewForm(
        project_id="p1",
        chapter_number=1,
        form_schema_version=FORM_SCHEMA_VERSION,
        characters=[],
        countdowns=[],
        obligations=[],
        open_signals=[],
    )

    answers = call_form(
        form=form,
        chapter_text="正文没有需要追踪的既有对象。",
        prior_canon_summary="",
        llm_client=client,
    )

    assert len(client.calls) == 2
    assert "previous JSON did not satisfy" in client.calls[1]["messages"][-1]["content"]
    assert answers.characters == []
    assert answers.countdowns == []
    assert answers.obligations == []
    assert answers.open_signals == []


def test_call_form_repairs_non_verbatim_evidence_quote_once() -> None:
    client = EvidenceRepairingClient()
    form = ChapterReviewForm(
        project_id="p1",
        chapter_number=1,
        form_schema_version=FORM_SCHEMA_VERSION,
        characters=[],
        countdowns=[],
        obligations=[],
        open_signals=[],
    )
    chapter_text = "墙上写着：三分钟内未提交一致性标记，触发迟滞罚则。"

    answers = call_form(
        form=form,
        chapter_text=chapter_text,
        prior_canon_summary="",
        llm_client=client,
    )

    assert len(client.calls) == 2
    repair_prompt = client.calls[1]["messages"][-1]["content"]
    assert "quote_not_found" in repair_prompt
    assert "contiguous verbatim substring" in repair_prompt
    assert (
        answers.new_observations.new_world_facts[0].evidence_quote
        == "三分钟内未提交一致性标记，触发迟滞罚则。"
    )


def test_call_form_preserves_evidence_warnings_when_retry_budget_is_zero() -> None:
    client = EvidenceRepairingClient()
    form = ChapterReviewForm(
        project_id="p1",
        chapter_number=1,
        form_schema_version=FORM_SCHEMA_VERSION,
        characters=[],
        countdowns=[],
        obligations=[],
        open_signals=[],
    )

    answers = call_form(
        form=form,
        chapter_text="墙上写着：三分钟内未提交一致性标记，触发迟滞罚则。",
        prior_canon_summary="",
        llm_client=client,
        max_schema_retries=0,
    )

    assert len(client.calls) == 1
    assert (
        answers.new_observations.new_world_facts[0].evidence_quote
        == "墙上的规则要求三分钟内提交一致性标记。"
    )


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
