from __future__ import annotations

import hashlib
import json
from dataclasses import replace

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
        "plan_fulfillment": [{"target_id": "goal:1", "status": "done", "evidence": "Ari opened the gate."}],
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


def extract(client, *, max_attempts=3, max_tokens=None, calendar=CALENDAR, extraction_input=INPUT):
    options = {} if max_tokens is None else {"max_tokens": max_tokens}
    return ChapterDeltaExtractor(client, max_attempts=max_attempts, **options).extract(
        extraction_input=extraction_input, chapter_title="Gate", chapter_body=BODY,
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
    assert kwargs["max_tokens"] == 8000
    assert kwargs["output_schema"] == ChapterDeltaExtraction.model_json_schema()
    trace = result.prompt_trace
    assert trace["template_id"] == "extraction:chapter_delta"
    assert trace["trace_scope"] == "writer"
    assert trace["input_snapshot"]["chapter_number"] == 2
    assert trace["prompt_layers"] and trace["effective_system_prompt"]
    assert trace["attempts"][0]["provider_request_id"] == "1"
    assert client.llm_attempt_events == []


def test_configured_max_tokens_is_used_on_every_attempt():
    client = StubLLM("not JSON", payload())
    result = extract(client, max_tokens=12000)
    assert result.extraction is not None
    assert result.attempts == 2
    assert [kwargs["max_tokens"] for _, kwargs in client.calls] == [12000, 12000]


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


@pytest.mark.parametrize("target_id", ["goal:1", "obligation:open"])
def test_dropped_required_fulfillment_retries_with_verbatim_evidence(target_id):
    extraction_input = replace(INPUT, fulfillment_targets=(FulfillmentTarget(target_id, "Open the gate"),))
    first = payload()
    first["plan_fulfillment"] = [
        {"target_id": target_id, "status": "done", "evidence": "Ari unlocked the gate."},
    ]
    second = payload()
    second["plan_fulfillment"] = [
        {"target_id": target_id, "status": "done", "evidence": "Ari opened the gate."},
    ]
    client = StubLLM(first, second)

    result = extract(client, extraction_input=extraction_input)

    assert [(item.target_id, item.status, item.evidence) for item in result.extraction.plan_fulfillment] == [
        (target_id, "done", "Ari opened the gate."),
    ]
    assert result.attempts == len(client.calls) == 2
    assert result.issues == ()
    feedback = client.calls[1][0][-1]["content"].split("上一轮错误反馈：\n")[1]
    assert target_id in feedback
    assert all(value in feedback for value in ("verbatim", "done", "partial", "missing"))


@pytest.mark.parametrize("better_first", [True, False])
def test_required_fulfillment_retry_is_bounded_and_keeps_best_candidate(better_first):
    extraction_input = replace(INPUT, fulfillment_targets=(
        FulfillmentTarget("goal:1", "Open the gate"),
        FulfillmentTarget("obligation:open", "Enter"),
    ))
    better = payload()
    worse = payload()
    worse["plan_fulfillment"] = []
    responses = (better, worse) if better_first else (worse, better)
    client = StubLLM(*responses)

    result = extract(client, extraction_input=extraction_input)

    assert result.attempts == len(client.calls) == 2
    assert [(item.target_id, item.status) for item in result.extraction.plan_fulfillment] == [
        ("goal:1", "done"),
    ]
    feedback = client.calls[1][0][-1]["content"].split("上一轮错误反馈：\n")[1]
    assert "obligation:open" in feedback
    assert ("goal:1" in feedback) is not better_first
    assert len(result.prompt_trace["output_summary"]["business_retry_events"]) == 1


def test_absent_reward_targets_do_not_retry():
    extraction_input = replace(INPUT, fulfillment_targets=INPUT.fulfillment_targets + (
        FulfillmentTarget("reward:power", "power (optional)"),
    ))
    client = StubLLM(payload())

    result = extract(client, extraction_input=extraction_input)

    assert result.attempts == len(client.calls) == 1
    assert result.issues == ()
    assert [(item.target_id, item.status) for item in result.extraction.plan_fulfillment] == [
        ("goal:1", "done"),
    ]


def test_explicit_missing_required_fulfillment_does_not_retry():
    response = payload()
    response["plan_fulfillment"] = [{"target_id": "goal:1", "status": "missing"}]
    client = StubLLM(response)

    result = extract(client)

    assert result.attempts == len(client.calls) == 1
    assert result.issues == ()
    assert result.extraction.plan_fulfillment[0].status == "missing"


def test_required_fulfillment_retry_respects_attempt_budget():
    response = payload()
    response["plan_fulfillment"] = []
    client = StubLLM(response)

    result = extract(client, max_attempts=1)

    assert result.attempts == len(client.calls) == 1
    assert result.extraction.plan_fulfillment == []


TWO_SITES = replace(INPUT, locations=INPUT.locations + (LocationRow("site-2", "Hall", "region-1", "world-1"),))


def moving_to(to_ref, *, participants=()):
    response = payload()
    response["movements"] = [
        {"entity_ref": "person-1", "from_ref": "site-1", "to_ref": to_ref, "evidence": "Ari opened the gate."},
    ]
    if participants:
        response["events"] = [{
            "summary": "Ari opens the gate", "participant_refs": list(participants), "significance": "minor",
            "visibility": "public", "evidence": "Ari opened the gate.",
        }]
    return response


def test_unknown_ref_retries_once_naming_the_ref_and_its_path():
    client = StubLLM(moving_to("Hall"), moving_to("site-2"))

    result = extract(client, extraction_input=TWO_SITES)

    assert result.attempts == len(client.calls) == 2
    assert result.extraction.movements[0].to_ref == "site-2"
    assert result.issues == ()
    feedback = client.calls[1][0][-1]["content"].split("上一轮错误反馈：\n")[1]
    assert "movements[0].to_ref" in feedback
    assert "'Hall'" in feedback and "'site-2'" in feedback
    assert len(result.prompt_trace["output_summary"]["business_retry_events"]) == 1


def test_persistent_unknown_ref_gets_one_targeted_retry():
    client = StubLLM(moving_to("site-9"), moving_to("site-9"), moving_to("site-2"))

    result = extract(client, extraction_input=TWO_SITES)

    assert result.attempts == len(client.calls) == 2
    # The translator still reports the ref.
    assert result.extraction.movements[0].to_ref == "site-9"


@pytest.mark.parametrize("better_first", [True, False])
def test_best_candidate_has_the_fewest_unknown_refs(better_first):
    one = moving_to("site-9")
    two = moving_to("site-9", participants=("Nobody",))
    client = StubLLM(*((one, two) if better_first else (two, one)))

    result = extract(client, extraction_input=TWO_SITES)

    assert result.attempts == len(client.calls) == 2
    assert result.extraction.events == []


def test_unknown_refs_outrank_absent_targets_in_the_best_candidate():
    absent_target = moving_to("site-2")
    absent_target["plan_fulfillment"] = []
    client = StubLLM(absent_target, moving_to("site-9"))

    result = extract(client, max_attempts=2, extraction_input=TWO_SITES)

    assert result.attempts == len(client.calls) == 2
    assert result.extraction.movements[0].to_ref == "site-2"
    assert result.extraction.plan_fulfillment == []


@pytest.mark.parametrize("failure", ["not JSON", {}, RuntimeError("provider unavailable")])
def test_evidence_candidate_survives_failed_retries(failure):
    first = payload()
    first["entity_mentions"].extend(
        {"name": f"Ghost {i}", "kind": "character", "evidence": f"invented {i}"}
        for i in range(3)
    )
    result = extract(StubLLM(first, failure, failure))
    assert result.attempts == 3
    assert result.extraction is not None
    assert result.extraction.body_sha256 == hashlib.sha256(BODY.encode()).hexdigest()
    assert [item.name for item in result.extraction.entity_mentions] == ["Ari"]
    assert elapsed_minutes(result.extraction, CALENDAR, LEXICON) == 1440
    assert [(issue.severity, issue.code, issue.path) for issue in result.issues] == [
        ("warning", "evidence_not_in_body", f"entity_mentions[{i}]") for i in range(1, 4)
    ]
    assert len(result.prompt_trace["attempts"]) == 3
    assert result.prompt_trace["output_summary"]["extraction_ok"] is True


@pytest.mark.parametrize("failure", ["not JSON", {}, TimeoutError("provider timed out")])
def test_unknown_elapsed_candidate_survives_failed_retries(failure):
    result = extract(StubLLM(payload(unit="unknown"), failure, failure))
    assert result.attempts == 3
    assert result.extraction is not None
    assert result.extraction.body_sha256 == hashlib.sha256(BODY.encode()).hexdigest()
    assert [item.name for item in result.extraction.entity_mentions] == ["Ari"]
    assert result.extraction.time.elapsed is None
    assert elapsed_minutes(result.extraction, CALENDAR, LEXICON) is None
    assert [(issue.severity, issue.code, issue.path) for issue in result.issues] == [
        ("warning", "elapsed_unknown", "time.elapsed"),
    ]
    assert "unknown" in result.issues[0].message
    assert len(result.prompt_trace["attempts"]) == 3
    assert result.prompt_trace["output_summary"]["extraction_ok"] is True


def test_last_valid_candidate_replaces_earlier_candidate():
    first = payload()
    first["entity_mentions"][0]["evidence"] = "invented"
    first["entity_mentions"] *= 2
    result = extract(StubLLM(first, payload(unit="unknown"), "not JSON"))
    assert result.attempts == 3
    assert result.extraction is not None
    assert [item.name for item in result.extraction.entity_mentions] == ["Ari"]
    assert result.extraction.time.elapsed is None
    assert [issue.code for issue in result.issues] == ["elapsed_unknown"]


@pytest.mark.parametrize("error_type", [RuntimeError, TimeoutError])
@pytest.mark.parametrize("recovers", [True, False])
def test_provider_errors_are_traced_without_becoming_model_feedback(error_type, recovers):
    error = error_type("provider unavailable at https://provider.invalid/v1?token=test-token")
    client = StubLLM(error, error, payload() if recovers else error)
    result = extract(client)
    assert result.attempts == 3
    assert len(client.calls) == 3
    assert all(messages == client.calls[0][0] for messages, _ in client.calls[1:])
    failed_attempts = result.prompt_trace["attempts"][:2 if recovers else 3]
    assert all(attempt["error_class"] == error_type.__name__ for attempt in failed_attempts)
    assert all(attempt["error_message"] == error_type.__name__ for attempt in failed_attempts)
    retries = result.prompt_trace["output_summary"]["business_retry_events"]
    assert len(retries) == 2
    assert all(event["reason_class"] == error_type.__name__ for event in retries)
    assert all(event["reason"] == error_type.__name__ for event in retries)
    if recovers:
        assert result.extraction is not None
        assert result.issues == ()
    else:
        assert result.extraction is None
        assert len(result.issues) == 1
        issue = result.issues[0]
        assert issue.code == "extraction_failed"
        assert error_type.__name__ in issue.message
        assert "https://" not in issue.message
        assert "provider.invalid" not in issue.message
        assert "test-token" not in issue.message


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


def test_prompt_limits_participants_and_movers_to_entities_acting_by_themselves():
    system = build_chapter_delta_messages(
        extraction_input=INPUT, chapter_title="Gate", chapter_body=BODY, chapter_summary="Summary only",
    )[0]["content"]
    assert "participant_refs 与 movements 的 entity_ref 只填自己行动或自己移动的实体" in system
    assert "被哀悼、埋葬、发现或搬运的死者写进事件 summary" in system


def test_prompt_explains_planned_and_optional_reward_targets():
    system = build_chapter_delta_messages(
        extraction_input=INPUT, chapter_title="Gate", chapter_body=BODY, chapter_summary="Summary only",
    )[0]["content"]
    assert "optional 的 reward 目标只在正文确实兑现时记录" in system


@pytest.mark.parametrize("feedback", [(), ("fix evidence",)])
def test_prompt_feedback_header_is_conditional(feedback):
    messages = build_chapter_delta_messages(
        extraction_input=INPUT, chapter_title="Gate", chapter_body=BODY,
        chapter_summary="Summary only", feedback=feedback,
    )
    assert ("上一轮错误反馈：" in messages[1]["content"]) is bool(feedback)
    if feedback:
        assert messages[1]["content"].endswith("上一轮错误反馈：\nfix evidence")


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
    bad["entity_mentions"] *= 2
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


@pytest.mark.parametrize("field,value", [
    ("goals_json", '["changed"]'), ("experience_plan_json", '{"planned_reward_tags":["mystery"]}'),
    ("band_plan_id", "band-new"), ("pov_ref", "actor-new"),
    ("onstage_refs_json", '["actor-new"]'), ("location_refs_json", '["site-new"]'),
    ("expected_span_minutes", 60),
    ("world_bindings_json", '[{"kind":"intent","ref":"intent-1","how":"advance"}]'),
    ("reveal_directives_json", '[{"id":"r","fact_ref":"fact:secret-1","observer":"reader","target_state":"knows"}]'),
    ("hint_directives_json", '[{"id":"h","fact_ref":"fact:secret-1","hint_text":"clue"}]'),
    ("intended_changes_json", '[{"ref":"actor-new","field":"state.status","value":"active"}]'),
    ("milestone_ids_json", '["milestone-new"]'), ("hook", "A new hook"),
])
def test_plan_fingerprint_changes_with_new_columns_and_invalidates_extraction_cache(monkeypatch, field, value):
    from types import SimpleNamespace
    from forwin.book_state.extraction import chapter_delta_extractor as module
    from forwin.models.project import ChapterPlan
    from forwin.candidate_drafts import candidate_plan_revision
    from forwin.protocol.writer import WriterOutput

    plan = ChapterPlan(id="plan", project_id="project", chapter_number=2, goals_json='["Open the gate"]')
    session = SimpleNamespace(scalar=lambda query: plan)
    monkeypatch.setattr(module, "build_chapter_extraction_input", lambda *args, **kwargs: INPUT)
    monkeypatch.setattr(module.StoryCalendarRepository, "get", lambda *args: CALENDAR)
    monkeypatch.setattr(module.BookLexiconRepository, "get", lambda *args: LEXICON)
    client = StubLLM(payload(), payload())
    output = WriterOutput(chapter_number=2, title="Gate", body=BODY, end_of_chapter_summary="Summary")
    revision = candidate_plan_revision(plan)
    first, result = module.ensure_chapter_delta(session, llm_client=client, project_id="project", chapter_number=2, writer_output=output)
    reused, result = module.ensure_chapter_delta(session, llm_client=client, project_id="project", chapter_number=2, writer_output=first)
    assert reused is first and result is None
    setattr(plan, field, value)
    second, result = module.ensure_chapter_delta(session, llm_client=client, project_id="project", chapter_number=2, writer_output=first)
    assert result is not None, "An unchanged body must be re-extracted when its plan changes"
    assert len(client.calls) == 2
    assert candidate_plan_revision(plan) != revision
    assert first.chapter_delta.plan_fingerprint != second.chapter_delta.plan_fingerprint


def test_bindings_are_required_but_directives_are_optional():
    extraction_input = replace(INPUT, fulfillment_targets=(
        *INPUT.fulfillment_targets, FulfillmentTarget("bind:0", "Advance intent-1"),
        FulfillmentTarget("reveal:r", "Reveal secret"), FulfillmentTarget("hint:h", "Hint secret"),
    ))
    complete = payload()
    complete["plan_fulfillment"].append({"target_id": "bind:0", "status": "missing"})
    client = StubLLM(payload(), complete)
    result = extract(client, extraction_input=extraction_input)
    assert result.attempts == 2
    assert "bind:0" in client.calls[1][0][-1]["content"]
    assert {row.target_id for row in result.extraction.plan_fulfillment} == {"goal:1", "bind:0"}


def test_prompt_marks_scene_locations_as_non_evidence():
    extraction_input = replace(INPUT, scene_location_hints=("site-1", "new:Harbor"))
    messages = build_chapter_delta_messages(extraction_input=extraction_input, chapter_title="Gate", chapter_body=BODY, chapter_summary="Summary")
    user = messages[-1]["content"]
    assert "写作者声明的场景地点，只辅助定位，不是证据" in user
    assert "new:Harbor" in user


def test_fingerprint_is_canonical_and_shared_with_quality_cache():
    from types import SimpleNamespace
    from forwin.candidate_drafts import candidate_plan_revision
    from forwin.canon_quality.cache import build_quality_analysis_cache_key
    from forwin.config import InfrastructureConfig
    from forwin.models.project import ChapterPlan
    from forwin.planning.band_planner.persistence import plan_fingerprint
    from forwin.protocol.writer import WriterOutput

    plan = ChapterPlan(id="plan", chapter_number=2, goals_json='["Open the gate"]',
                       world_bindings_json='[{"kind":"intent","ref":"intent-1","how":"Advance"}]')
    output = WriterOutput(chapter_number=2, title="Gate", body=BODY, end_of_chapter_summary="Summary")
    session = SimpleNamespace(execute=lambda query: SimpleNamespace(scalar_one_or_none=lambda: plan))
    def key():
        return build_quality_analysis_cache_key(session=session, project_id="project", chapter_number=2,
            writer_output=output, mode="primary", llm_client=None, config=InfrastructureConfig(), quality_context={})
    original = plan_fingerprint(plan)
    assert candidate_plan_revision(plan) == key()["plan_fingerprint"] == original
    plan.world_bindings_json = '[ {"how": "Advance", "ref": "intent-1", "kind": "intent"} ]'
    assert plan_fingerprint(plan) == original
    plan.hook = "A new hook"
    assert candidate_plan_revision(plan) == key()["plan_fingerprint"] != original


def test_canon_preparation_preserves_quality_order_for_current_plan(monkeypatch):
    from types import SimpleNamespace
    from forwin.canon import preparation
    from forwin.candidate_drafts import candidate_body_hash, candidate_plan_revision
    from forwin.models.project import ChapterPlan
    from forwin.naming.types import EntityAdmissionPlan
    from forwin.protocol.review import ReviewVerdict
    from forwin.protocol.writer import WriterOutput

    plan = ChapterPlan(id="plan", chapter_number=2)
    delta = ChapterDeltaExtraction.model_validate(payload())
    delta.body_sha256 = candidate_body_hash(BODY)
    delta.plan_fingerprint = candidate_plan_revision(plan)
    output = WriterOutput(chapter_number=2, title="Gate", body=BODY, end_of_chapter_summary="Summary", chapter_delta=delta)
    candidate = SimpleNamespace(id="candidate", project_id="project", chapter_number=2, chapter_plan_id="plan",
                                body_hash=candidate_body_hash(BODY), policy_version=1)
    monkeypatch.setattr(preparation.CandidateDraftRepository, "get", lambda *args: candidate)
    monkeypatch.setattr(preparation, "_mark_candidate_needs_review", lambda *args, **kwargs: None)
    monkeypatch.setattr(preparation, "_verify_admission", lambda *args, **kwargs: EntityAdmissionPlan(
        project_id="project", chapter_number=2, candidate_fingerprint=""))
    events = []
    def quality(**kwargs):
        events.append("quality")
        return SimpleNamespace(blocked=True, blocked_path="quality-stop", gate_result=None)
    service = preparation.CanonPreparationService(quality_evaluator=quality)
    def world(**kwargs):
        events.append("world")
        return preparation.BookStatePreparationOutcome(extraction=delta)
    monkeypatch.setattr(service, "_prepare_book_state", world)
    result = service.prepare(
        context=SimpleNamespace(_record_decision_event=lambda **kwargs: None),
        session=SimpleNamespace(get=lambda *args: plan), repo=None, updater=None,
        candidate_id="candidate", project_id="project", chapter_number=2, writer_output=output,
        verdict=ReviewVerdict(verdict="pass"), acceptance_mode="auto", repair_attempt_count=0,
        residual_review_issues=[], canon_risk_level="",
    )
    assert result.blocked_path == "quality-stop"
    assert events == ["quality"]


def _exercise_persisted_plan_cache(engine):
    from sqlalchemy.orm import Session
    from forwin.book_state.extraction.chapter_delta_extractor import ensure_chapter_delta
    from forwin.candidate_drafts import candidate_plan_revision
    from forwin.canon_quality.cache import build_quality_analysis_cache_key
    from forwin.config import InfrastructureConfig
    from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
    from forwin.protocol.writer import WriterOutput

    with Session(engine) as session:
        project = Project(title="Cache", premise="test", genre="test")
        session.add(project)
        session.flush()
        arc = ArcPlanVersion(project_id=project.id, arc_synopsis="arc")
        session.add(arc)
        session.flush()
        plan = ChapterPlan(project_id=project.id, arc_plan_id=arc.id, chapter_number=1,
                           goals_json='["Open the gate"]')
        session.add(plan)
        session.flush()
        response = {"body_sha256": "untrusted", "plan_fingerprint": "untrusted",
                    "time": {"elapsed": None, "evidence": ""}, "plan_fulfillment": [
                        {"target_id": "goal:1", "status": "done", "evidence": BODY},
                    ]}
        changed = {**response, "plan_fulfillment": [*response["plan_fulfillment"],
                    {"target_id": "bind:0", "status": "missing"}]}
        client = StubLLM(response, changed)
        output = WriterOutput(project_id=project.id, chapter_number=1, title="Gate", body=BODY,
                              end_of_chapter_summary="Summary", scene_location_hints=["new:Harbor", "invalid-location-hint"])
        def ensure(current):
            return ensure_chapter_delta(session, llm_client=client, project_id=project.id,
                                        chapter_number=1, writer_output=current)
        def quality_key():
            return build_quality_analysis_cache_key(session=session, project_id=project.id, chapter_number=1,
                writer_output=output, mode="primary", llm_client=None, config=InfrastructureConfig(), quality_context={})
        first, result = ensure(output)
        assert result is not None
        original_key = quality_key()
        assert first.chapter_delta.plan_fingerprint == candidate_plan_revision(plan) == original_key["plan_fingerprint"]
        assert "new:Harbor" in client.calls[0][0][-1]["content"]
        assert "invalid-location-hint" not in client.calls[0][0][-1]["content"]
        reused, result = ensure(first)
        assert reused is first and result is None
        plan.world_bindings_json = '[{"kind":"event","ref":"event-1","how":"Witness"}]'
        session.flush()
        second, result = ensure(first)
        assert result is not None and len(client.calls) == 2
        assert second.chapter_delta.plan_fingerprint != first.chapter_delta.plan_fingerprint
        assert quality_key()["plan_fingerprint"] != original_key["plan_fingerprint"]
        assert {item.target_id for item in second.chapter_delta.plan_fulfillment} == {"goal:1", "bind:0"}


def test_extraction_reloads_persisted_plan_and_scene_hints_sqlite():
    from sqlalchemy import create_engine
    from forwin.models.base import Base

    engine = create_engine("sqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        _exercise_persisted_plan_cache(engine)
    finally:
        engine.dispose()


def test_extraction_reloads_persisted_plan_and_scene_hints_postgres():
    from forwin.models.base import get_engine, init_db
    from tests.postgres import postgres_test_url

    engine = get_engine(postgres_test_url("task13-extraction-cache"))
    try:
        init_db(engine)
        _exercise_persisted_plan_cache(engine)
    finally:
        engine.dispose()


def test_no_plan_reuses_legacy_extraction_with_empty_fingerprint():
    from types import SimpleNamespace
    from forwin.book_state.extraction.chapter_delta_extractor import ensure_chapter_delta
    from forwin.protocol.writer import WriterOutput

    delta = ChapterDeltaExtraction.model_validate(payload())
    delta.body_sha256 = hashlib.sha256(BODY.encode()).hexdigest()
    output = WriterOutput(chapter_number=2, title="Gate", body=BODY, end_of_chapter_summary="Summary", chapter_delta=delta)
    reused, result = ensure_chapter_delta(SimpleNamespace(scalar=lambda query: None), llm_client=None,
        project_id="project", chapter_number=2, writer_output=output)
    assert reused is output and result is None
    assert delta.plan_fingerprint == ""


def test_plan_change_without_client_discards_stale_extraction():
    from types import SimpleNamespace
    from forwin.book_state.extraction.chapter_delta_extractor import ensure_chapter_delta
    from forwin.models.project import ChapterPlan
    from forwin.protocol.writer import WriterOutput

    delta = ChapterDeltaExtraction.model_validate(payload())
    delta.body_sha256 = hashlib.sha256(BODY.encode()).hexdigest()
    output = WriterOutput(chapter_number=2, title="Gate", body=BODY, end_of_chapter_summary="Summary", chapter_delta=delta)
    plan = ChapterPlan(id="plan", chapter_number=2, goals_json='["Open the gate"]')
    refreshed, result = ensure_chapter_delta(SimpleNamespace(scalar=lambda query: plan), llm_client=None,
        project_id="project", chapter_number=2, writer_output=output)
    assert refreshed.chapter_delta is None
    assert [issue.code for issue in result.issues] == ["extraction_unavailable"]
