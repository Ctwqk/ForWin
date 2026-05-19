from __future__ import annotations

from forwin.canon_quality.chapter_review_form import FORM_SCHEMA_VERSION
from forwin.canon_quality.chapter_review_form.form_schema import ChapterReviewForm
from forwin.canon_quality.chapter_review_form.llm_caller import _normalize_answer_payload


def _form() -> ChapterReviewForm:
    return ChapterReviewForm(
        project_id="p1",
        chapter_number=18,
        form_schema_version=FORM_SCHEMA_VERSION,
        characters=[],
        countdowns=[],
        obligations=[],
        open_signals=[],
    )


def test_nested_dict_bool_values_are_coerced_to_schema_strings() -> None:
    payload = {
        "countdowns": [
            {
                "key": "main",
                "mentioned_in_chapter": True,
                "status_in_this_chapter": {"value": True, "evidence_quote": "主倒计时仍在跳。"},
                "new_value_minutes": 57,
                "new_value_evidence": {"value": 57, "evidence_quote": "还剩五十七分钟。"},
                "consistent_with_prior": {"value": False, "evidence_quote": "前文还剩五十七分钟。"},
            }
        ],
        "characters": [],
        "obligations": [],
        "open_signals": [],
        "new_observations": {},
    }

    normalized = _normalize_answer_payload(payload, form=_form())

    countdown = normalized["countdowns"][0]
    assert countdown["status_in_this_chapter"]["value"] == "true"
    assert countdown["consistent_with_prior"]["value"] == "false"
    assert countdown["new_value_evidence"]["value"] == "57"


def test_nested_dict_none_value_is_coerced_to_empty_schema_string() -> None:
    payload = {
        "characters": [
            {
                "name": "守仓阙微阑",
                "appears_in_chapter": True,
                "life_state": {"value": None, "evidence_quote": "她仍在仓阙。"},
                "custody_state": {"value": "free", "evidence_quote": "她仍在仓阙。"},
                "participation": {"value": "present", "evidence_quote": "她仍在仓阙。"},
            }
        ],
        "countdowns": [],
        "obligations": [],
        "open_signals": [],
        "new_observations": {},
    }

    normalized = _normalize_answer_payload(payload, form=_form())

    assert normalized["characters"][0]["life_state"]["value"] == ""
