from __future__ import annotations

from forwin.canon_quality.chapter_review_form import FORM_SCHEMA_VERSION
from forwin.canon_quality.chapter_review_form.service import review_chapter_with_form
from forwin.protocol.writer import WriterOutput


class FakeClient:
    def complete_json(self, **kwargs):  # noqa: ANN001, ANN201
        quote = "林青倒下，再无呼吸。"
        return {
            "project_id": "p1",
            "chapter_number": 2,
            "form_schema_version": FORM_SCHEMA_VERSION,
            "characters": [
                {
                    "name": "林青",
                    "appears_in_chapter": True,
                    "life_state": {
                        "value": "dead",
                        "evidence_quote": quote,
                        "subject_of_quote": "林青",
                        "confidence": 0.95,
                    },
                    "custody_state": {"value": "unknown"},
                    "participation": {"value": "present_acting"},
                }
            ],
            "countdowns": [],
            "obligations": [],
            "open_signals": [],
            "new_observations": {},
            "chapter_summary": "林青死亡。",
        }


class RepairingClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete_json(self, **kwargs):  # noqa: ANN001, ANN201
        self.calls += 1
        if self.calls == 1:
            return {
                "characters": [{"name": "林青", "evidence_quote": "林青倒下，再无呼吸。", "confidence": 1.0}],
                "countdowns": [],
                "obligations": [],
                "open_signals": [],
                "new_observations": {},
                "chapter_summary": "bad shape",
            }
        return FakeClient().complete_json(**kwargs)


class FlatClient:
    def complete_json(self, **kwargs):  # noqa: ANN001, ANN201
        return {
            "characters": [
                {
                    "name": "林青",
                    "appears_in_chapter": True,
                    "life_state": "dead",
                    "custody_state": "free",
                    "participation": "major",
                    "evidence_quote": "林青倒下，再无呼吸。",
                    "confidence": 0.95,
                }
            ],
            "countdowns": [],
            "obligations": [],
            "open_signals": [],
            "new_observations": {},
            "chapter_summary": "flat but usable",
        }


class NonFinalChapterClient:
    def complete_json(self, **kwargs):  # noqa: ANN001, ANN201
        payload = FakeClient().complete_json(**kwargs)
        payload["final_chapter"] = {
            "is_final_chapter": False,
            "resolution_status": "unresolved",
        }
        return payload


class DecreasedCountdownKindClient:
    def complete_json(self, **kwargs):  # noqa: ANN001, ANN201
        quote = "主线倒计时还剩74分钟。"
        return {
            "project_id": "p1",
            "chapter_number": 7,
            "form_schema_version": FORM_SCHEMA_VERSION,
            "characters": [],
            "countdowns": [
                {
                    "key": "main",
                    "mentioned_in_chapter": True,
                    "status_in_this_chapter": {
                        "value": "active",
                        "evidence_quote": quote,
                        "subject_of_quote": "main",
                        "confidence": 0.95,
                    },
                    "new_value_minutes": 74,
                    "new_value_evidence": {
                        "value": "74",
                        "evidence_quote": quote,
                        "subject_of_quote": "main",
                        "confidence": 0.95,
                    },
                    "consistent_with_prior": {
                        "value": "true",
                        "evidence_quote": quote,
                        "subject_of_quote": "main",
                        "confidence": 0.95,
                    },
                    "inconsistency_kind": "decreased",
                }
            ],
            "obligations": [],
            "open_signals": [],
            "new_observations": {},
            "chapter_summary": "倒计时继续推进。",
        }


def test_form_service_projects_validated_answer() -> None:
    result = review_chapter_with_form(
        session=None,
        project_id="p1",
        chapter_number=2,
        writer_output=WriterOutput(
            project_id="p1",
            chapter_number=2,
            title="二",
            body="林青倒下，再无呼吸。",
            end_of_chapter_summary="",
        ),
        draft_id="d1",
        llm_client=FakeClient(),
        character_rows=[{"character_name": "林青", "to_state": "alive", "chapter_number": 1}],
    )

    assert result.mode == "chapter_review_form"
    assert result.character_transitions[0].character_name == "林青"
    assert result.blocking is False


def test_form_service_retries_schema_invalid_answer() -> None:
    client = RepairingClient()
    result = review_chapter_with_form(
        session=None,
        project_id="p1",
        chapter_number=2,
        writer_output=WriterOutput(
            project_id="p1",
            chapter_number=2,
            title="二",
            body="林青倒下，再无呼吸。",
            end_of_chapter_summary="",
        ),
        draft_id="d1",
        llm_client=client,
        character_rows=[{"character_name": "林青", "to_state": "alive", "chapter_number": 1}],
    )

    assert client.calls == 2
    assert result.summary == "林青死亡。"
    assert result.character_transitions[0].to_state == "dead"


def test_form_service_accepts_flat_answer_shapes() -> None:
    result = review_chapter_with_form(
        session=None,
        project_id="p1",
        chapter_number=2,
        writer_output=WriterOutput(
            project_id="p1",
            chapter_number=2,
            title="二",
            body="林青倒下，再无呼吸。",
            end_of_chapter_summary="",
        ),
        draft_id="d1",
        llm_client=FlatClient(),
        character_rows=[{"character_name": "林青", "to_state": "alive", "chapter_number": 1}],
    )

    assert result.summary == "flat but usable"
    assert result.character_transitions[0].to_state == "dead"
    assert not any(issue["type"] == "form_schema_invalid" for issue in result.review_issues)


def test_form_service_drops_unrequested_final_chapter_payload() -> None:
    result = review_chapter_with_form(
        session=None,
        project_id="p1",
        chapter_number=2,
        writer_output=WriterOutput(
            project_id="p1",
            chapter_number=2,
            title="二",
            body="林青倒下，再无呼吸。",
            end_of_chapter_summary="",
        ),
        draft_id="d1",
        llm_client=NonFinalChapterClient(),
        character_rows=[{"character_name": "林青", "to_state": "alive", "chapter_number": 1}],
    )

    assert result.summary == "林青死亡。"
    assert not any(issue["type"] == "form_schema_invalid" for issue in result.review_issues)


def test_form_service_normalizes_decreased_countdown_kind() -> None:
    result = review_chapter_with_form(
        session=None,
        project_id="p1",
        chapter_number=7,
        writer_output=WriterOutput(
            project_id="p1",
            chapter_number=7,
            title="七",
            body="主线倒计时还剩74分钟。",
            end_of_chapter_summary="倒计时继续推进。",
        ),
        draft_id="d7",
        llm_client=DecreasedCountdownKindClient(),
        countdown_rows=[
            {
                "countdown_key": "main",
                "label": "main",
                "normalized_remaining_minutes": 79,
                "status": "active",
                "chapter_number": 6,
            }
        ],
    )

    assert result.blocking is False
    assert result.countdown_entries[0].normalized_remaining_minutes == 74
    assert result.countdown_entries[0].payload["inconsistency_kind"] == "none"
    assert not any(issue["type"] == "form_schema_invalid" for issue in result.review_issues)


def test_form_service_llm_unavailable_blocks_without_writes() -> None:
    result = review_chapter_with_form(
        session=None,
        project_id="p1",
        chapter_number=2,
        writer_output=WriterOutput(
            project_id="p1",
            chapter_number=2,
            title="二",
            body="林青出现。",
            end_of_chapter_summary="",
        ),
        draft_id="d1",
        llm_client=object(),
        character_rows=[{"character_name": "林青", "to_state": "alive", "chapter_number": 1}],
    )

    assert result.blocking is True
    assert result.character_transitions == []
    assert result.signals[0].signal_type == "form_llm_unavailable"
    assert result.review_issues[0]["blocking"] is True
