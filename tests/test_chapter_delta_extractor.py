from __future__ import annotations

import hashlib
import json

import pytest

from forwin.book_state.extraction.chapter_delta_extractor import (
    ChapterDeltaExtractor,
    elapsed_minutes,
)
from forwin.book_state.extraction.chapter_delta_schema import ChapterDeltaExtraction
from forwin.book_state.extraction.chapter_input import (
    ChapterExtractionInput, EntityRow, FulfillmentTarget, IntentRow, LocationRow, SecretRow,
)
from forwin.book_state.lexicon import BookLexicon, TimeUnitAlias
from forwin.book_state.story_clock import CalendarUnit, StoryCalendar, UnknownTimeUnit
from forwin.writer.prompt_core.chapter_delta import build_chapter_delta_messages


BODY = "Ari waited one day. Ari opened the gate."
CALENDAR = StoryCalendar("epoch", 0, (CalendarUnit("日", 1440),))
LEXICON = BookLexicon(time_unit_aliases=(TimeUnitAlias("watch", 30),))
INPUT = ChapterExtractionInput(
    project_id="project", chapter_number=2,
    entities=(EntityRow("person-1", "character", "Ari", ("A",)),),
    locations=(LocationRow("site-1", "Gate", "region-1", "world-1"),),
    calendar_units=("日",), time_aliases=("watch",),
    fulfillment_targets=(FulfillmentTarget("goal:1", "Open the gate"),),
    intents=(IntentRow("intent-1", "person-1", "Enter", "planned"),),
    secrets=(SecretRow("fact:secret-1", "The gate hides a passage", "secret"),),
    travel_modes=("walk",),
)


def payload(*, unit="日", evidence=BODY):
    return {
        "body_sha256": "untrusted-model-hash",
        "time": {"elapsed": {"amount": 1, "unit": unit}, "evidence": evidence},
        "entity_mentions": [{"name": "Ari", "kind": "character", "evidence": BODY}],
    }


class StubLLM:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []
        self.llm_attempt_events = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        self.llm_attempt_events.append({
            "attempt_no": 1, "stage_key": kwargs["stage_key"], "provider_request_id": str(len(self.calls)),
        })
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, str) else json.dumps(response)

    def drain_llm_attempt_events(self):
        events, self.llm_attempt_events = self.llm_attempt_events, []
        return events


def extract(client, *, max_attempts=3, calendar=CALENDAR):
    return ChapterDeltaExtractor(client, max_attempts=max_attempts).extract(
        extraction_input=INPUT, chapter_title="Gate", chapter_body=BODY,
        chapter_summary="Ari enters", calendar=calendar, lexicon=LEXICON,
    )


def test_valid_extraction_passes_through_with_body_hash():
    client = StubLLM(payload())
    result = extract(client)
    assert result.extraction.body_sha256 == hashlib.sha256(BODY.encode()).hexdigest()
    assert result.extraction.entity_mentions[0].name == "Ari"
    assert result.issues == ()
    assert result.attempts == 1
    assert elapsed_minutes(result.extraction, CALENDAR, LEXICON) == 1440
    kwargs = client.calls[0][1]
    assert kwargs["stage_key"] == "chapter_delta_extraction"
    assert kwargs["task_family"] == "writer"
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["output_schema"] == ChapterDeltaExtraction.model_json_schema()
    trace = result.prompt_trace
    assert trace["template_id"] == "extraction:chapter_delta"
    assert trace["trace_scope"] == "writer"
    assert trace["input_snapshot"]["chapter_number"] == 2
    assert trace["prompt_layers"] and trace["effective_system_prompt"]
    assert trace["attempts"][0]["provider_request_id"] == "1"
    assert client.llm_attempt_events == []


@pytest.mark.parametrize("invalid", [{}, "not JSON"])
def test_schema_error_retries_with_feedback(invalid):
    client = StubLLM(invalid, payload())
    result = extract(client)
    assert result.extraction is not None
    assert result.attempts == 2
    assert result.issues == ()
    trace = result.prompt_trace
    assert len(trace["attempts"]) == 2
    assert trace["attempts"][0]["parse_ok"] is False
    error = trace["attempts"][0]["parse_error"]
    assert error in client.calls[1][0][-1]["content"]
    assert trace["output_summary"]["business_retry_events"][0]["attempt_no"] == 1


def test_paraphrased_evidence_is_dropped_reported_and_retried():
    first = payload()
    first["entity_mentions"] *= 4
    first["entity_mentions"] = [dict(item, evidence=f"invented {i}") if i else item
                                for i, item in enumerate(first["entity_mentions"])]
    second = payload()
    second["entity_mentions"].append({"name": "Ghost", "kind": "character", "evidence": "not present"})
    client = StubLLM(first, second)
    result = extract(client)
    assert result.attempts == 2
    assert len(result.extraction.entity_mentions) == 1
    assert [(issue.code, issue.path) for issue in result.issues] == [
        ("evidence_not_in_body", "entity_mentions[1]"),
    ]
    assert "evidence_not_in_body" in client.calls[1][0][-1]["content"]


def test_unknown_elapsed_unit_retries_then_degrades_to_zero():
    client = StubLLM(payload(unit="天"), payload(unit="天"))
    result = extract(client)
    assert result.extraction is not None
    assert result.attempts == 2
    assert result.extraction.time.elapsed is None
    assert elapsed_minutes(result.extraction, CALENDAR, LEXICON) is None
    assert [(issue.severity, issue.code) for issue in result.issues] == [("warning", "elapsed_unknown")]
    assert "天" in client.calls[1][0][-1]["content"]


def test_unknown_elapsed_unit_can_be_corrected():
    result = extract(StubLLM(payload(unit="天"), payload(unit="watch")))
    assert result.attempts == 2
    assert result.issues == ()
    assert elapsed_minutes(result.extraction, CALENDAR, LEXICON) == 30


@pytest.mark.parametrize("response", ["not JSON", {}, RuntimeError("provider unavailable")])
def test_all_attempts_fail_returns_extraction_failed(response):
    client = StubLLM(response, response, response)
    result = extract(client)
    assert result.extraction is None
    assert result.attempts == 3
    assert any(issue.code == "extraction_failed" and issue.severity == "error" for issue in result.issues)
    assert len(result.prompt_trace["attempts"]) == 3


def test_prompt_carries_tables_and_secret_list_but_marks_it_label_only():
    messages = build_chapter_delta_messages(
        extraction_input=INPUT, chapter_title="Gate", chapter_body=BODY,
        chapter_summary="Summary only", feedback=("fix evidence",),
    )
    assert [item["role"] for item in messages] == ["system", "user"]
    assert "只用于打标签" in messages[0]["content"]
    user = messages[1]["content"]
    values = ["person-1", "site-1", "日", "walk", "goal:1", "intent-1",
              "The gate hides a passage", BODY, "Summary only", "fix evidence"]
    assert all(value in user for value in values)
    assert [user.index(value) for value in values] == sorted(user.index(value) for value in values)
    assert all(name in messages[0]["content"] for name in ChapterDeltaExtraction.model_fields)


def test_elapsed_minutes_without_calendar_uses_only_lexicon_aliases():
    extraction = ChapterDeltaExtraction.model_validate(payload(unit="watch"))
    assert elapsed_minutes(extraction, None, LEXICON) == 30
    extraction.time.elapsed.unit = "unknown"
    with pytest.raises(UnknownTimeUnit):
        elapsed_minutes(extraction, None, LEXICON)


def test_invalid_time_evidence_does_not_advance_clock():
    result = extract(StubLLM(payload(evidence="invented")))
    assert result.attempts == 1
    assert result.extraction.time.elapsed is None
    assert result.issues[0].path == "time"


def test_retry_budget_degrades_unknown_time_on_last_attempt():
    result = extract(StubLLM(payload(unit="unknown")), max_attempts=1)
    assert result.attempts == 1
    assert result.extraction.time.elapsed is None
    assert result.issues[0].code == "elapsed_unknown"


def test_evidence_retry_is_bounded_even_if_second_result_is_bad():
    bad = payload()
    bad["entity_mentions"][0]["evidence"] = "invented"
    result = extract(StubLLM(bad, bad))
    assert result.attempts == 2
    assert result.extraction.entity_mentions == []
    assert result.issues[0].code == "evidence_not_in_body"


def test_client_without_output_schema_or_attempt_events_is_supported():
    class LegacyLLM:
        def chat(self, messages, *, temperature, max_tokens, response_format, task_family, stage_key):
            return json.dumps(payload())

    result = extract(LegacyLLM())
    assert result.extraction is not None
    assert len(result.prompt_trace["attempts"]) == 1
