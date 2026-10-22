from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from forwin.book_state.cognition import CognitionView
from forwin.book_state.knowledge import KNOWLEDGE_CHANNELS, KNOWLEDGE_STATES
from forwin.book_state.lexicon import BookLexicon, TimeUnitAlias
from forwin.book_state.map_graph import MapGraph
from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
from forwin.book_state.schema import INTENT_ACTION_TYPES
from forwin.book_state.story_clock import CalendarUnit, StoryCalendar
from forwin.protocol.book_state import CognitionOverlay, FactNode, MapNode, WORLD_EDGE_TYPES, WorldNode
from forwin.world_engine.constraints import ConstraintIssue
from forwin.world_engine.types import AdjudicationResult, TickContext


CONTEXT = TickContext(project_id="p", chapter_number=3, now=120, window_start=60, seed=9182)
CALENDAR = StoryCalendar("epoch", 0, (CalendarUnit("cycle", 120),))
LEXICON = BookLexicon(time_unit_aliases=(TimeUnitAlias("watch", 30),))


class StubLLM:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []
        self.llm_attempt_events = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        self.llm_attempt_events.append({
            "attempt_no": 1, "stage_key": kwargs["stage_key"],
            "provider_request_id": str(len(self.calls)),
        })
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, str) else json.dumps(response)

    def drain_llm_attempt_events(self):
        events, self.llm_attempt_events = self.llm_attempt_events, []
        return events


def _node(ref, kind="character", *, profile=None, **state):
    return WorldNode(id=ref, project_id="p", node_type=kind, profile=profile or {}, state=state)


def _runtime():
    return BookStateRuntime(
        project_id="p", as_of_chapter=3,
        world=ObjectiveWorldGraph(nodes=[
            _node("owner", resources=["stale-resource"], location_id="site"),
            _node("target", resources=["target-resource"], location_id="site"),
            _node("item", "item", holder_id="owner"),
            _node("rule", "rule", profile={"hardness": "hard", "statement": "rule-statement"}),
            *[_node(ref, "intent", profile={
                "owner_ref": "owner", "target_refs": ["target"], "action_type": "negotiate",
                "summary": "intent-summary",
            }, status="due", scheduled_at=90) for ref in ("intent-1", "intent-2")],
        ], states_by_node_id={"owner": {"resources": ["current-resource"], "location_id": "site"}}, facts=[
            FactNode(id="hidden", project_id="p", proposition="hidden-world-truth", sensitivity_level="secret"),
        ]),
        map_graph=MapGraph(nodes=[MapNode(id="site", project_id="p", node_type="site", name="site-name")]),
        cognition_by_observer={
            ("reader", "reader"): CognitionView(CognitionOverlay(
                id="reader-overlay-sentinel", project_id="p", observer_type="reader", observer_id="reader",
                field_overrides={"reader-only-sentinel": "reader-private-cognition"},
            )),
        },
    )


def _outcome(ref="intent-1", **kwargs):
    return {"intent_id": ref, "result": "resolved", "summary": "settled", "effects": {}, **kwargs}


def _adjudicate(client, *, refs=("intent-1",), runtime=None, feedback=None, **kwargs):
    from forwin.world_engine.adjudicator import WorldAdjudicator
    runtime = runtime or _runtime()
    return WorldAdjudicator(client, **kwargs).adjudicate(
        context=CONTEXT, intents=[runtime.world.get_node(ref) for ref in refs], runtime=runtime,
        lexicon=LEXICON, calendar=CALENDAR, feedback=feedback,
    )


def test_adjudicate_returns_outcomes_for_input_intents_only():
    effects = {"item_transfers": [{"item_ref": "item", "from_ref": "owner", "to_ref": "target"}]}
    client = StubLLM({"outcomes": [_outcome(effects=effects), _outcome("unrequested")]})
    call = _adjudicate(client)
    assert [item.intent_id for item in call.result.outcomes] == ["intent-1"]
    assert call.result.outcomes[0].effects.item_transfers[0].to_ref == "target"
    assert len(client.calls) == 1
    kwargs = client.calls[0][1]
    assert kwargs["stage_key"] == "world.adjudicate"
    assert kwargs["task_family"] == "world_engine"
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["output_schema"] == AdjudicationResult.model_json_schema()
    assert kwargs["max_tokens"] == 6000
    trace = call.prompt_trace
    assert trace["trace_scope"] == "world_engine"
    assert trace["template_id"] == "world_engine:adjudicate"
    assert trace["input_snapshot"]["seed"] == 9182
    assert trace["stage_key"] == "world.adjudicate"
    assert trace["prompt_layers"] and trace["effective_system_prompt"]
    assert trace["attempts"][0]["provider_request_id"] == "1"
    assert client.llm_attempt_events == []


def test_invalid_json_retries_then_returns_none_with_trace():
    client = StubLLM("not JSON", "still not JSON")
    call = _adjudicate(client, max_tokens=4321)
    assert call.result is None and call.issues
    attempts = call.prompt_trace["attempts"]
    assert len(attempts) == len(client.calls) == 2
    assert [item["workflow_attempt_no"] for item in attempts] == [1, 2]
    assert all(item["parse_ok"] is False for item in attempts)
    assert attempts[0]["parse_error"] in client.calls[1][0][-1]["content"]
    assert [kwargs["max_tokens"] for _, kwargs in client.calls] == [4321, 4321]
    assert len(call.prompt_trace["output_summary"]["business_retry_events"]) == 1
    assert client.llm_attempt_events == []


def test_feedback_issues_are_rendered_per_intent():
    feedback = {
        "intent-1": [ConstraintIssue("resource_not_held", "holder-mismatch", "item")],
        "intent-2": [ConstraintIssue("time_not_monotonic", "bad-time-window", "target")],
    }
    client = StubLLM("not JSON", {"outcomes": [_outcome(), _outcome("intent-2", result="disrupted")]})
    call = _adjudicate(client, refs=("intent-1", "intent-2"), feedback=feedback)
    assert call.result.outcomes[1].result == "disrupted"
    for messages, _ in client.calls:
        user = messages[-1]["content"]
        section = user.split("Constraint feedback by intent:\n")[1].split("\n\n")[0]
        parsed = json.loads(section)
        assert parsed["intent-1"][0] == {"code": "resource_not_held", "message": "holder-mismatch", "ref": "item"}
        assert parsed["intent-2"][0]["message"] == "bad-time-window"


def test_prompt_carries_seed_rules_and_calendar_units_but_no_reader_knowledge():
    runtime = _runtime()
    before = runtime.world.get_state("owner")
    client = StubLLM({"outcomes": [_outcome()]})
    call = _adjudicate(client, runtime=runtime)
    user = client.calls[0][0][-1]["content"]
    assert all(value in user for value in (
        "9182", "rule-statement", "hard", "cycle", "120", "watch", "30", "hidden-world-truth",
        "current-resource", "target-resource", "site-name", "holder_id", "owner_ref", "scheduled_at",
    ))
    assert "stale-resource" not in user
    trace = json.dumps(call.prompt_trace)
    assert "reader-only-sentinel" not in trace and "reader-private-cognition" not in trace
    assert "reader-overlay-sentinel" not in trace
    assert runtime.world.get_state("owner") == before


def test_prompt_lists_canonical_enum_values_missing_from_json_schema():
    client = StubLLM({"outcomes": [_outcome()]})
    _adjudicate(client)
    system = client.calls[0][0][0]["content"]
    for allowed in (KNOWLEDGE_CHANNELS, KNOWLEDGE_STATES, WORLD_EDGE_TYPES, INTENT_ACTION_TYPES):
        assert all(json.dumps(value) in system for value in allowed)


@pytest.mark.parametrize("first", [{}, {"outcomes": []}, {"outcomes": [_outcome("intent-2")]}])
def test_missing_due_intent_retries_with_named_feedback(first):
    client = StubLLM(first, {"outcomes": [_outcome()]})
    call = _adjudicate(client)
    assert [item.intent_id for item in call.result.outcomes] == ["intent-1"]
    assert call.issues == []
    assert len(client.calls) == 2
    retry = client.calls[1][0][-1]["content"].split("Retry validation feedback:\n")[1]
    assert "intent-1" in retry and "missing" in retry.lower()


@pytest.mark.parametrize("last", [{}, "not JSON", RuntimeError("provider-private-detail")])
@pytest.mark.parametrize("better_first", [True, False])
def test_bounded_retry_keeps_best_candidate(last, better_first):
    partial = {"outcomes": [_outcome()]}
    client = StubLLM(*((partial, last) if better_first else (last, partial)))
    call = _adjudicate(client, refs=("intent-1", "intent-2"))
    assert len(client.calls) == 2
    assert [item.intent_id for item in call.result.outcomes] == ["intent-1"]
    assert any("intent-2" in issue for issue in call.issues)


def test_transport_error_never_becomes_model_feedback():
    client = StubLLM(TimeoutError("https://private.invalid/?token=secret"), {"outcomes": [_outcome()]})
    call = _adjudicate(client)
    assert call.result is not None and call.issues == []
    assert client.calls[0][0] == client.calls[1][0]
    assert call.prompt_trace["attempts"][0]["error_message"] == "TimeoutError"
    assert "private.invalid" not in json.dumps(call.prompt_trace)


def test_schema_error_retries_and_recovers():
    client = StubLLM({"outcomes": [_outcome(result="invented-status")]}, {"outcomes": [_outcome()]})
    call = _adjudicate(client)
    assert call.result.outcomes[0].result == "resolved"
    assert call.prompt_trace["attempts"][0]["parse_ok"] is False
    assert call.issues == []


@pytest.mark.parametrize("ref", ["unknown-actor", "reader"])
def test_invalid_effect_reference_retries_then_drops_outcome(ref):
    bad = {"outcomes": [_outcome(effects={"knowledge": [{
        "observer_ref": ref, "fact_ref": "fact:hidden", "state": "knows", "channel": "told",
    }]})]}
    client = StubLLM(bad, bad)
    call = _adjudicate(client)
    assert call.result.outcomes == []
    assert any(ref in issue for issue in call.issues)
    assert ref in client.calls[1][0][-1]["content"].split("Retry validation feedback:\n")[1]


def test_new_fact_reference_is_valid_within_its_outcome():
    client = StubLLM({"outcomes": [_outcome(effects={
        "new_facts": [{"key": "created", "proposition": "new-truth", "truth_value": True,
                       "sensitivity": "hidden", "known_by": ["owner"]}],
        "knowledge": [{"observer_ref": "target", "fact_ref": "fact:new:created", "state": "knows", "channel": "told"}],
    })]})
    call = _adjudicate(client)
    assert call.issues == [] and len(call.result.outcomes) == 1


def test_trace_preserves_backend_and_actual_model():
    client = StubLLM({"outcomes": [_outcome()]})
    client.last_call_result = SimpleNamespace(
        backend="codex_bridge", actual_model="test-actual-model", fallback_used=True, trace={"codex_job_id": "job-1"},
    )
    trace = _adjudicate(client).prompt_trace
    assert trace["backend"] == "codex_bridge"
    assert trace["codex_job_id"] == "job-1"
    assert trace["fallback_used"] is True
    assert trace["model_profile"]["actual_model"] == "test-actual-model"
