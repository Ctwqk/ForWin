from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from forwin.book_state.cognition import CognitionView
from forwin.book_state.knowledge import KNOWLEDGE_CHANNELS, KNOWLEDGE_STATES
from forwin.book_state.lexicon import BookLexicon, TimeUnitAlias, TravelMode
from forwin.book_state.map_graph import MapGraph
from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
from forwin.book_state.story_clock import CalendarUnit, StoryCalendar
from forwin.protocol.book_state import CognitionOverlay, FactNode, MapEdge, MapNode, WORLD_EDGE_TYPES, WorldEdge, WorldNode
from forwin.world_engine.constraints import ENDED_STATUSES, ConstraintIssue
from forwin.world_engine.types import AdjudicationResult, TickContext, WorldEffects


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
            "attempt_group_id": str(len(self.calls)),
            "profile_id": "test-profile", "model": "test-model",
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
            FactNode(id="hidden", project_id="p", proposition="hidden-world-truth", sensitivity_level="secret",
                     related_node_refs=["owner"]),
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


def _adjudicate(client, *, refs=("intent-1",), runtime=None, feedback=None, max_new_actors=1, **kwargs):
    from forwin.world_engine.adjudicator import WorldAdjudicator
    runtime = runtime or _runtime()
    return WorldAdjudicator(client, **kwargs).adjudicate(
        context=CONTEXT, intents=[runtime.world.get_node(ref) for ref in refs], runtime=runtime,
        lexicon=LEXICON, calendar=CALENDAR, feedback=feedback, max_new_actors=max_new_actors,
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
    payload = json.loads(user)
    assert payload["context"]["seed"] == 9182
    assert payload["rules"] == [{"id": "rule", "hardness": "hard", "statement": "rule-statement"}]
    assert payload["calendar"]["units"] == [{"name": "cycle", "minutes": 120}]
    assert payload["lexicon"]["time_unit_aliases"] == [{"name": "watch", "minutes": 30}]
    assert all(value in user for value in (
        "hidden-world-truth",
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
    allowed = json.loads(system.split("Allowed values:\n")[1])
    assert allowed == {
        "knowledge_state": list(KNOWLEDGE_STATES), "knowledge_channel": list(KNOWLEDGE_CHANNELS),
        "edge_type": sorted(WORLD_EDGE_TYPES), "new_fact_ref_prefix": "fact:new:",
        "ended_status": sorted(ENDED_STATUSES),
    }


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


def test_retry_over_the_guard_keeps_the_first_attempts_outcomes(monkeypatch):
    from forwin.world_engine import adjudicator
    from forwin.world_engine.types import PROMPT_TOO_LARGE
    from forwin.writer.prompt_core.world_engine import build_adjudication_messages

    runtime = _runtime()
    intents = [runtime.world.get_node(ref) for ref in ("intent-1", "intent-2")]
    first = build_adjudication_messages(
        context=CONTEXT, intents=intents, runtime=runtime, lexicon=LEXICON, calendar=CALENDAR,
    )
    # The first prompt just fits; the retry's feedback for the missing intent puts it over the guard.
    monkeypatch.setattr(adjudicator, "MAX_ADJUDICATION_PROMPT_CHARS", len(first[-1]["content"]))
    client = StubLLM({"outcomes": [_outcome()]})

    call = _adjudicate(client, refs=("intent-1", "intent-2"), runtime=runtime)

    assert len(client.calls) == 1
    assert [item.intent_id for item in call.result.outcomes] == ["intent-1"]
    assert (call.missing_intent_ids, call.failure_reason) == (("intent-2",), PROMPT_TOO_LARGE)
    assert "prompt_too_large" in call.issues
    assert call.prompt_trace["output_summary"]["failure_reason"] == "prompt_too_large"
    assert call.prompt_trace["output_summary"]["outcome_count"] == 1
    # The trace records the prompt that was sent, not the retry that never was.
    assert call.prompt_trace["prompt_layers"][-1]["content"] == first[-1]["content"]
    assert "Retry validation feedback" not in json.dumps(call.prompt_trace, ensure_ascii=False)


def test_fits_measures_the_first_prompt_against_the_guard(monkeypatch):
    from forwin.world_engine import adjudicator
    from forwin.writer.prompt_core.world_engine import build_adjudication_messages

    runtime = _runtime()
    intents = [runtime.world.get_node(ref) for ref in ("intent-1", "intent-2")]
    size = len(build_adjudication_messages(
        context=CONTEXT, intents=intents[:1], runtime=runtime, lexicon=LEXICON, calendar=CALENDAR,
    )[-1]["content"])
    monkeypatch.setattr(adjudicator, "MAX_ADJUDICATION_PROMPT_CHARS", size)
    client = StubLLM()

    def fits(refs):
        return adjudicator.WorldAdjudicator(client).fits(
            context=CONTEXT, intents=[runtime.world.get_node(ref) for ref in refs], runtime=runtime,
            lexicon=LEXICON, calendar=CALENDAR,
        )

    assert fits(["intent-1"]) and not fits(["intent-1", "intent-2"])
    assert client.calls == []


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


def test_adjudication_new_actor_references_and_remaining_budget():
    from tests.test_world_engine_contracts import _new_actor

    response = {"outcomes": [_outcome(effects={
        "new_actors": [_new_actor()],
        "events": [{"summary": "Arrival", "participant_refs": ["actor:new:arrival"],
                    "location_id": "site", "visibility": "public", "at": 90}],
    })]}
    client = StubLLM(response)
    call = _adjudicate(client, max_new_actors=0)
    assert call.issues == [] and len(call.result.outcomes) == 1
    payload = json.JSONDecoder().raw_decode(client.calls[0][0][-1]["content"])[0]
    assert payload["max_new_actors"] == 0
    assert "actor:new:<key>" in client.calls[0][0][0]["content"]
    assert "lexicon" in payload


@pytest.mark.parametrize("changes,path", [
    ({"location_id": "owner"}, "location_id"),
    ({"affiliation_refs": ["owner"]}, "affiliation_refs"),
    ({"affiliation_refs": ["actor:new:arrival"]}, "affiliation_refs"),
])
def test_new_actor_requires_map_location_and_existing_group(changes, path):
    from tests.test_world_engine_contracts import _new_actor

    bad = {"outcomes": [_outcome(effects={"new_actors": [_new_actor(**changes)]})]}
    client = StubLLM(bad, bad)
    call = _adjudicate(client)
    assert call.result.outcomes == []
    assert len(client.calls) == 2
    assert any(path in issue for issue in call.issues)


def test_trace_uses_served_attempts_instead_of_legacy_router_result():
    client = StubLLM({"outcomes": [_outcome()]})
    client.last_call_result = SimpleNamespace(
        backend="deepseek_chain", actual_model="test-actual-model", fallback_used=True, trace={},
    )
    trace = _adjudicate(client).prompt_trace
    assert trace["backend"] == "openai_compatible"
    assert trace["fallback_used"] is False
    assert trace["model_profile"]["profile_id"] == "test-profile"
    assert trace["model_profile"]["model"] == "test-model"


def _prompt_payload(runtime, **kwargs):
    from forwin.writer.prompt_core.world_engine import build_adjudication_messages
    messages = build_adjudication_messages(
        context=CONTEXT, intents=[runtime.world.get_node("intent-1")], runtime=runtime,
        lexicon=LEXICON, calendar=CALENDAR, **kwargs,
    )
    return json.loads(messages[-1]["content"].split("\n\n")[0]), messages[-1]["content"]


def test_prompt_limits_full_bodies_and_scopes_edges_and_facts():
    runtime = _runtime()
    for index in range(100):
        runtime.world.add_node(_node(f"unrelated-{index}", profile={"detail": "unrelated-body" * 100}))
    runtime.world.add_node(_node("nearby", location_id="site"))
    runtime.world.add_node(_node("elsewhere", location_id="remote"))
    runtime.world.add_node(_node("target-item", "resource", holder_id="target"))
    runtime.world.add_node(_node("remote-item", "item", holder_id="elsewhere"))
    runtime.world.get_node("target").profile["fact_refs"] = ["fact:referenced-hidden"]
    for ref, source, target in (
        ("one-hop", "nearby", "elsewhere"), ("two-hop", "elsewhere", "unrelated-0"),
    ):
        runtime.world.add_edge(WorldEdge(id=ref, project_id="p", source_id=source, target_id=target,
                                        edge_family="social", edge_type="trusts"))
    runtime.world.add_fact(FactNode(id="referenced-hidden", project_id="p", proposition="referenced-truth",
                                  sensitivity_level="hidden", contradiction_refs=["fact:linked-hidden"]))
    runtime.world.add_fact(FactNode(id="linked-hidden", project_id="p", proposition="linked-truth",
                                  sensitivity_level="secret", contradiction_refs=["fact:referenced-hidden"]))
    runtime.world.add_fact(FactNode(id="edge-fact", project_id="p", proposition="edge-truth",
                                  related_edge_refs=["one-hop"]))
    runtime.world.add_fact(FactNode(id="unrelated-fact", project_id="p", proposition="unrelated-truth",
                                  related_node_refs=["elsewhere"], sensitivity_level="secret"))
    payload, user = _prompt_payload(runtime)
    assert {node["id"] for node in payload["world_nodes"]} == {"owner", "target", "item", "target-item", "nearby"}
    assert [node["id"] for node in payload["intents"]] == ["intent-1"]
    index = {node["id"]: node for node in payload["world_node_index"]}
    assert index["unrelated-99"] == {"id": "unrelated-99", "node_type": "character", "name": ""}
    assert "intent-1" not in index
    assert "unrelated-body" not in user and "unrelated-truth" not in user
    assert [edge["id"] for edge in payload["world_edges"]] == ["one-hop"]
    assert {fact["id"] for fact in payload["facts"]} == {"hidden", "referenced-hidden", "linked-hidden", "edge-fact"}
    for node in payload["world_nodes"] + payload["intents"]:
        assert not ({"aliases", "tags", "metadata", "retired_at_chapter", "importance", "is_active"} & node.keys())
        assert all(value not in (None, [], {}) for value in node.values())


def test_prompt_includes_nodes_at_intent_location_and_one_hop_map():
    runtime = _runtime()
    runtime.world.get_node("intent-1").profile["location_id"] = "destination"
    runtime.world.add_node(_node("at-destination", location_id="destination"))
    for ref in ("destination", "neighbor", "remote"):
        runtime.map.nodes_by_id[ref] = MapNode(id=ref, project_id="p", node_type="site", description="map-body-" + ref)
    for ref, source, target in (("near", "destination", "neighbor"), ("far", "neighbor", "remote")):
        runtime.map.edges_by_id[ref] = MapEdge(id=ref, project_id="p", from_node_id=source,
                                             to_node_id=target, edge_type="path", travel_time=2)
    payload, user = _prompt_payload(runtime)
    assert "at-destination" in {node["id"] for node in payload["world_nodes"]}
    assert [edge["id"] for edge in payload["map_edges"]] == ["near"]
    assert "map-body-remote" not in user


def test_prompt_drops_compact_index_before_size_guard():
    runtime = _runtime()
    for index in range(1200):
        runtime.world.add_node(_node(f"unrelated-{index}"))
    client = StubLLM({"outcomes": [_outcome()]})
    call = _adjudicate(client, runtime=runtime)
    assert len(client.calls) == 1 and call.result.outcomes
    user = client.calls[0][0][-1]["content"]
    assert len(user) <= 60000
    assert "unrelated-1199" not in user


def test_oversized_prompt_returns_failure_without_llm_call():
    runtime = _runtime()
    runtime.world.states_by_node_id["owner"]["resources"] = ["x" * 60001]
    client = StubLLM({"outcomes": [_outcome()]})
    call = _adjudicate(client, runtime=runtime)
    assert client.calls == []
    assert call.result is None
    assert call.missing_intent_ids == ("intent-1",)
    assert call.prompt_trace["attempts"] == []
    assert call.prompt_trace["output_summary"]["failure_reason"] == "prompt_too_large"
    assert call.prompt_trace["output_summary"]["adjudication_ok"] is False


def test_exhausted_empty_result_reports_missing_intents():
    call = _adjudicate(StubLLM({}), max_attempts=1)
    assert call.prompt_trace["output_summary"]["adjudication_ok"] is False
    assert call.missing_intent_ids == ("intent-1",)


def test_duplicate_outcomes_keep_first_valid_without_retry():
    client = StubLLM({"outcomes": [_outcome(summary="first"), _outcome(summary="second")]})
    call = _adjudicate(client)
    assert len(client.calls) == 1
    assert call.result.outcomes[0].summary == "first"
    assert any("duplicate" in issue for issue in call.issues)
    assert call.missing_intent_ids == ()
    assert call.prompt_trace["output_summary"]["adjudication_ok"] is True


@pytest.mark.parametrize("feedback", [
    {"intent-1": []}, {"other": [ConstraintIssue("test", "message", "owner")]},
    {"intent-1": [], "other": [ConstraintIssue("test", "message", "owner")]},
])
def test_feedback_section_absent_without_issues_for_input_intents(feedback):
    client = StubLLM({"outcomes": [_outcome()]})
    _adjudicate(client, feedback=feedback)
    assert "Constraint feedback by intent:" not in client.calls[0][0][-1]["content"]


def test_empty_intents_return_empty_call_without_llm():
    client = StubLLM({})
    call = _adjudicate(client, refs=())
    assert client.calls == []
    assert call.result == AdjudicationResult()
    assert call.issues == [] and call.missing_intent_ids == ()
    assert call.prompt_trace["attempts"] == []


@pytest.mark.parametrize("effects,path", [
    ({"entity_changes": [{"ref": "owner", "field": "location_id", "new_value": "unknown"}]}, "entity_changes[0].new_value"),
    ({"entity_changes": [{"ref": "owner", "field": "details", "new_value": {"target_refs": ["unknown"]}}]}, "entity_changes[0].new_value.target_refs[0]"),
    ({"relation_changes": [{"source_ref": "owner", "target_ref": "target", "edge_type": "trusts", "change": "set", "field": "owner_ref", "new_value": "unknown"}]}, "relation_changes[0].new_value"),
    ({"relation_changes": [{"source_ref": "owner", "target_ref": "target", "edge_type": "trusts", "change": "set", "field": "details", "new_value": {"target_ref": "unknown"}}]}, "relation_changes[0].new_value.target_ref"),
    ({"movements": [{"entity_ref": "owner", "to_location_id": "unknown", "depart_at": 70, "arrive_at": 80}]}, "movements[0].to_location_id"),
    ({"movements": [{"entity_ref": "owner", "to_location_id": "site", "travel_mode_id": "unknown", "depart_at": 70, "arrive_at": 80}]}, "movements[0].travel_mode_id"),
    ({"events": [{"summary": "event", "participant_refs": ["unknown"], "location_id": "site", "visibility": "public", "at": 70}]}, "events[0].participant_refs"),
    *[({"item_transfers": [{"item_ref": "item", "from_ref": "owner", "to_ref": "target", field: "unknown"}]}, f"item_transfers[0].{field}") for field in ("item_ref", "from_ref", "to_ref")],
    *[({"new_facts": [{"key": "new", "proposition": "truth", "truth_value": True, "sensitivity": "hidden", "known_by": [ref]}]}, "new_facts[0].known_by") for ref in ("unknown", "reader")],
])
def test_shared_reference_validator_rejects_remaining_reference_branches(effects, path):
    from forwin.world_engine.effect_validation import effect_reference_issues
    issues = effect_reference_issues(WorldEffects.model_validate(effects), _runtime(), LEXICON)
    assert any(path in issue for issue in issues)
    bad = {"outcomes": [_outcome(effects=effects)]}
    client = StubLLM(bad, bad)
    call = _adjudicate(client)
    assert len(client.calls) == 2 and call.result.outcomes == []
    assert any(path in issue for issue in call.issues)


def test_shared_reference_validator_accepts_known_refs_and_free_text():
    from forwin.world_engine.effect_validation import effect_reference_issues
    lexicon = BookLexicon(travel_modes=(TravelMode("walk", "walk", 1, ("path",)),))
    effects = WorldEffects.model_validate({
        "entity_changes": [{"ref": "owner", "field": "location_id", "new_value": "site"},
                           {"ref": "owner", "field": "resources", "new_value": ["free text"]}],
        "relation_changes": [{"source_ref": "owner", "target_ref": "target", "edge_type": "trusts",
                              "change": "set", "field": "details", "new_value": {"target_refs": ["item"]}}],
        "movements": [{"entity_ref": "owner", "to_location_id": "site", "travel_mode_id": "walk", "depart_at": 70, "arrive_at": 80}],
        "events": [{"summary": "event", "participant_refs": ["owner"], "location_id": "site", "visibility": "public", "at": 70}],
        "item_transfers": [{"item_ref": "item", "from_ref": "owner", "to_ref": "target"}],
        "new_facts": [{"key": "new", "proposition": "truth", "truth_value": True, "sensitivity": "hidden", "known_by": ["owner"]}],
    })
    assert effect_reference_issues(effects, _runtime(), lexicon) == []


def test_prompt_leaves_past_events_and_settled_intents_out_of_full_bodies_and_the_index():
    runtime = _runtime()
    # The book's history at the owner's site: events and intents settled long ago, and a live bystander.
    for index in range(3):
        runtime.world.add_node(_node(f"event-{index}", "event", profile={"location_id": "site"},
                                     location_id="site", participant_ids=["owner"]))
    runtime.world.add_node(_node("settled-intent", "intent", profile={"owner_ref": "target", "location_id": "site"},
                                 status="resolved"))
    runtime.world.add_node(_node("gone", location_id="site").model_copy(update={"is_active": False}))
    payload, _user = _prompt_payload(runtime)
    assert {node["id"] for node in payload["world_nodes"]} == {"owner", "target", "item"}
    index = {node["id"] for node in payload["world_node_index"]}
    # Intents still to happen stay in the index; events, settled intents and rules leave it.
    assert "intent-2" in index and "gone" in index
    assert index.isdisjoint({"event-0", "event-1", "event-2", "settled-intent", "rule", "intent-1"})


def test_prompt_shows_every_standing_fact_and_only_the_newest_event_facts():
    from forwin.writer.prompt_core.world_engine import MAX_ADJUDICATION_EVENT_FACTS

    runtime = _runtime()
    for index in range(MAX_ADJUDICATION_EVENT_FACTS + 5):
        runtime.world.add_fact(FactNode(id=f"event-fact-{index:02d}", project_id="p", proposition="event-truth",
                                        fact_type="event", related_node_refs=["owner"], created_at_chapter=index))
    runtime.world.add_fact(FactNode(id="old-standing", project_id="p", proposition="standing-truth",
                                    related_node_refs=["owner"], created_at_chapter=0))
    payload, _user = _prompt_payload(runtime)
    ids = {fact["id"] for fact in payload["facts"]}
    assert {"hidden", "old-standing"} <= ids
    assert sorted(ref for ref in ids if ref.startswith("event-fact-")) == [
        f"event-fact-{index:02d}" for index in range(5, MAX_ADJUDICATION_EVENT_FACTS + 5)
    ]


def _loner_intents(runtime, count: int) -> list[str]:
    """``count`` more due intents, each of its own owner standing alone at its own place."""
    refs = []
    for index in range(count):
        owner, intent = f"loner-{index}", f"loner-intent-{index}"
        runtime.world.add_node(_node(owner, location_id=f"far-{index}"))
        runtime.world.add_node(_node(intent, "intent", profile={
            "owner_ref": owner, "action_type": "negotiate", "summary": "loner-summary",
        }, status="due", scheduled_at=90, location_id=f"far-{index}"))
        refs.append(intent)
    return refs


def _event_facts(runtime, actor: str, count: int, *, newest: int) -> None:
    for index in range(count):
        runtime.world.add_fact(FactNode(
            id=f"event-{actor}-{index:02d}", project_id="p", proposition="event-truth", fact_type="event",
            related_node_refs=[actor], created_at_chapter=newest - index,
        ))


def _shown_events(runtime, refs) -> list[str]:
    from forwin.writer.prompt_core.world_engine import build_adjudication_messages
    messages = build_adjudication_messages(
        context=CONTEXT, intents=[runtime.world.get_node(ref) for ref in refs], runtime=runtime,
        lexicon=LEXICON, calendar=CALENDAR,
    )
    payload = json.loads(messages[-1]["content"].split("\n\n")[0])
    return sorted(fact["id"] for fact in payload["facts"] if fact.get("fact_type") == "event")


def test_a_busy_intent_does_not_crowd_out_another_intents_event_facts():
    runtime = _runtime()
    [loner] = _loner_intents(runtime, 1)
    # The owner's recent history is long; the loner's is short and old.
    _event_facts(runtime, "owner", 25, newest=30)
    _event_facts(runtime, "loner-0", 3, newest=1)

    shown = _shown_events(runtime, ["intent-1", loner])

    # The loner keeps its whole history; the owner's newest events take the spare room.
    assert shown == sorted([
        *(f"event-owner-{index:02d}" for index in range(17)),
        *(f"event-loner-0-{index:02d}" for index in range(3)),
    ])


def test_an_event_a_shown_node_names_outranks_spare_history_but_not_the_guaranteed_share():
    from forwin.writer.prompt_core.world_engine import (
        MAX_ADJUDICATION_EVENT_FACTS, MAX_ADJUDICATION_EVENT_FACTS_PER_INTENT,
    )

    runtime = _runtime()
    _event_facts(runtime, "owner", 25, newest=30)
    # An old event elsewhere that the owner's token names: no intent's own full bodies take part in it.
    runtime.world.add_node(_node("token", "item", holder_id="owner", cause_ref="fact:named-event"))
    runtime.world.add_fact(FactNode(id="named-event", project_id="p", proposition="event-truth", fact_type="event",
                                    related_node_refs=["elsewhere"], created_at_chapter=0))

    shown = _shown_events(runtime, ["intent-1"])

    spare = MAX_ADJUDICATION_EVENT_FACTS - MAX_ADJUDICATION_EVENT_FACTS_PER_INTENT - 1
    assert shown == sorted([
        "named-event",
        *(f"event-owner-{index:02d}" for index in range(MAX_ADJUDICATION_EVENT_FACTS_PER_INTENT + spare)),
    ])


def test_event_facts_are_shared_out_within_the_total_cap():
    from forwin.writer.prompt_core.world_engine import MAX_ADJUDICATION_EVENT_FACTS

    runtime = _runtime()
    loners = _loner_intents(runtime, 6)
    # Each loner's history is older than the one before it.
    for index in range(6):
        _event_facts(runtime, f"loner-{index}", 5, newest=30 - 5 * index)

    shown = _shown_events(runtime, loners)

    # Six intents of five events each would show thirty; each shows its newest few, round by round, so the
    # intents with the oldest history still show theirs.
    assert len(shown) == MAX_ADJUDICATION_EVENT_FACTS
    per_intent = [sum(ref.startswith(f"event-loner-{index}-") for ref in shown) for index in range(6)]
    assert sorted(per_intent) == [3, 3, 3, 3, 4, 4]
    assert all(f"event-loner-{index}-00" in shown for index in range(6))
