from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from forwin.book_state.cognition import CognitionView
from forwin.book_state.knowledge import KNOWLEDGE_CHANNELS, KNOWLEDGE_STATES
from forwin.book_state.lexicon import BookLexicon, TimeUnitAlias
from forwin.book_state.map_graph import MapGraph
from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
from forwin.book_state.schema import AGENDA_STATUSES, IMPACT_LEVELS, INTENT_ACTION_TYPES
from forwin.book_state.story_clock import CalendarUnit, StoryCalendar
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.protocol.book_state import (
    CognitionOverlay, FactNode, MapEdge, MapNode, WORLD_EDGE_TYPES, WorldEdge, WorldNode,
)
from forwin.runtime.policy import WorldPolicy
from forwin.world_engine.constraints import ConstraintIssue
from forwin.world_engine.types import ActorTurnBatch, TickContext


NOW = 600
CONTEXT = TickContext(project_id="p", chapter_number=3, now=NOW, window_start=480, seed=77)
CALENDAR = StoryCalendar("epoch", 0, (CalendarUnit("cycle", 120),))
LEXICON = BookLexicon(time_unit_aliases=(TimeUnitAlias("watch", 30),))
POLICY = WorldPolicy(actor_turn_batch_size=1, max_new_intents_per_actor=2)


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


def _node(ref, kind="character", *, name="", profile=None, is_active=True, **state):
    return WorldNode(id=ref, project_id="p", node_type=kind, name=name, profile=profile or {},
                     state=state, is_active=is_active)


def _agenda(ref, owner, goal, *, secrecy_refs=(), means_refs=(), **state):
    return _node(ref, "agenda", name=goal, profile={"owner_ref": owner, "goal": goal, "motivation": f"{goal}-why",
                                                    "horizon": "long"},
                 status="active", priority=3, means_refs=list(means_refs), constraints=[],
                 secrecy_refs=list(secrecy_refs), **state)


def _clock(ref, label, *, owner="", scope=None, status="running", due_at=900):
    profile = {"label": label, "owner_ref": owner, "on_expire": f"{label}-effect"}
    if scope is not None:
        profile["scope"] = scope
    return _node(ref, "clock", profile=profile, due_at=due_at, status=status)


def _fact(ref, proposition, *, sensitivity="secret", truth="true", related=(), chapter=1, kind=""):
    return FactNode(id=ref, project_id="p", proposition=proposition, sensitivity_level=sensitivity,
                    truth_value=truth, related_node_refs=list(related), created_at_chapter=chapter, fact_type=kind)


def _overlay(observer_type, observer_id, **fields):
    return CognitionView(CognitionOverlay(id=f"overlay-{observer_id}", project_id="p", observer_type=observer_type,
                                          observer_id=observer_id, **fields))


def _place(ref):
    return MapNode(id=ref, project_id="p", node_type="site", name=f"{ref}-name")


def _route(ref, source, target, hours, **fields):
    return MapEdge(id=ref, project_id="p", from_node_id=source, to_node_id=target, edge_type="path",
                   bidirectional=True, travel_time=hours, **fields)


def _runtime():
    return BookStateRuntime(
        project_id="p", as_of_chapter=3,
        world=ObjectiveWorldGraph(nodes=[
            _node("hero", name="hero-name", actor_tier="major", location_id="home", next_turn_at=500,
                  alive_status="alive", resources=["coin-pouch"]),
            _node("rival", name="rival-name", actor_tier="minor", location_id="home", next_turn_at=550),
            _node("ally", name="ally-name", actor_tier="minor", location_id="near", next_turn_at=590),
            _node("hidden-agent", name="hidden-agent-name", actor_tier="background", location_id="far"),
            _agenda("hero/agenda", "hero", "hero-goal", secrecy_refs=["ally"], means_refs=["blade"]),
            _agenda("ally/agenda", "ally", "ally-goal", secrecy_refs=["hero"]),
            _agenda("rival/agenda", "rival", "rival-goal"),
            _node("blade", "item", name="blade-name", holder_id="hero"),
            _node("rule-1", "rule", profile={"hardness": "hard", "statement": "rule-statement"}),
            _node("hero-old-intent", "intent", profile={"owner_ref": "hero", "summary": "vetoed-intent-summary"},
                  status="vetoed"),
            _clock("public-clock", "public-deadline", owner="rival", scope="public", due_at=900),
            _clock("own-clock", "own-deadline", owner="hero", scope="secret", due_at=700),
            _clock("confidant-clock", "confidant-deadline", owner="ally", scope="secret", due_at=800),
            _clock("rival-clock", "rival-deadline", owner="rival", scope="secret", due_at=800),
            _clock("unscoped-clock", "unscoped-deadline", owner="rival", due_at=800),
            _clock("expired-clock", "expired-deadline", owner="rival", scope="public", status="expired"),
        ], facts=[
            _fact("public-news", "public-proposition", sensitivity="public", related=["rival"], chapter=2),
            _fact("public-heard", "public-heard-proposition", sensitivity="public"),
            _fact("hero-secret", "hero-known-secret"),
            _fact("rival-secret", "rival-private-truth", related=["hidden-agent"]),
            _fact("false-rumor", "false-proposition", sensitivity="hidden", truth="false"),
            _fact("false-certainty", "certain-false-proposition", truth="false"),
            _fact("heard-rumor", "heard-proposition"),
            _fact("disbelieved", "disbelieved-proposition"),
        ]),
        map_graph=MapGraph(
            nodes=[_place(ref) for ref in ("home", "near", "far", "hidden-place")],
            edges=[
                _route("home-near", "home", "near", 1),
                _route("near-far", "near", "far", 3),
                _route("home-hidden", "home", "hidden-place", 0.5, discovered_by_default=False),
            ],
        ),
        cognition_by_observer={
            ("character", "hero"): _overlay("character", "hero", knowledge_by_ref={
                "fact:hero-secret": "knows", "fact:false-rumor": "believes", "fact:false-certainty": "knows",
                "fact:heard-rumor": "heard", "fact:disbelieved": "disbelieves", "fact:public-heard": "heard",
            }),
            ("character", "rival"): _overlay("character", "rival", knowledge_by_ref={"fact:rival-secret": "knows"}),
            ("reader", "reader"): _overlay("reader", "reader", knowledge_by_ref={"fact:rival-secret": "knows"},
                                           field_overrides={"reader-only-sentinel": "reader-private-cognition"}),
        },
    )


def _veto(ref, *, actor="hero", status="resolved", option="veto_world", story_time=500, intent_ref="",
          proposed=None):
    return WorldPlanConflictRow(
        id=ref, project_id="p", source="world_tick", detected_after_chapter=2, story_time=story_time,
        violated="protection", impact_level="minor", required_decider="auto", blocks_from_chapter=3,
        status=status, summary=f"{ref}-summary", intent_ref=intent_ref, actor_ref=actor,
        decision_json=json.dumps({"option": option}) if status == "resolved" else "{}",
        proposed_change_json=json.dumps(proposed or {}),
    )


def _view(actor_id="hero", runtime=None, *, vetoes=(), boundary_refs=()):
    from forwin.world_engine.actor_turns import build_actor_view
    return build_actor_view(runtime or _runtime(), LEXICON, actor_id=actor_id, now=NOW, vetoes=vetoes,
                            boundary_refs=boundary_refs)


def _intent(**overrides):
    return {
        "agenda_ref": "hero/agenda", "action_type": "negotiate", "summary": "intent-summary",
        "target_refs": ["rival"], "location_id": "near", "scheduled_in": {"amount": 2, "unit": "watch"},
        "requires_onstage": False, "impact_level": "minor", "expected_effects": {}, **overrides,
    }


def _turn(actor="hero", **overrides):
    return {"actor_id": actor, "new_intents": [], "agenda_changes": [],
            "next_turn_in": {"amount": 1, "unit": "cycle"}, **overrides}


def _run(client, views=None, *, policy=POLICY, feedback=None, **kwargs):
    from forwin.world_engine.actor_turns import ActorTurnRunner
    return ActorTurnRunner(client, **kwargs).run(
        context=CONTEXT, views=views if views is not None else [_view()], policy=policy, calendar=CALENDAR,
        lexicon=LEXICON, feedback=feedback,
    )


def _user_payload(call):
    return json.loads(call[0][-1]["content"].split("\n\n")[0])


def _retry_feedback(call):
    return call[0][-1]["content"].split("Retry validation feedback:\n")[1]


# ranking


def test_rank_actors_by_due_tier_relevance_and_id():
    from forwin.world_engine.actor_turns import rank_actors
    due = NOW - 10
    nodes = [
        _node("lead", actor_tier="major", location_id="home", next_turn_at=NOW + 100),
        _node("z-major", actor_tier="major", location_id="elsewhere", next_turn_at=due),
        _node("a-faction", "faction", actor_tier="major", next_turn_at=due),
        _node("minor-high", actor_tier="minor", location_id="home", next_turn_at=due),
        _node("minor-mid", actor_tier="minor", next_turn_at=NOW),
        _node("minor-alpha", actor_tier="minor", next_turn_at=due),
        _node("minor-low", actor_tier="minor", next_turn_at=due),
        _node("background", actor_tier="background", location_id="home", next_turn_at=due),
        _node("not-due", actor_tier="major", next_turn_at=NOW + 1),
        _node("never-due", actor_tier="major"),
        _node("dead", actor_tier="major", next_turn_at=due, alive_status="dead"),
        _node("retired", actor_tier="major", next_turn_at=due, is_active=False),
        _node("thing", "item", next_turn_at=due),
    ]
    edges = [
        ("lead", "a-faction", True), ("minor-high", "lead", True), ("lead", "minor-mid", True),
        ("lead", "minor-low", False), ("background", "lead", True), ("lead", "background", True),
        ("minor-alpha", "a-faction", True),
    ]
    world = ObjectiveWorldGraph(nodes=nodes, edges=[
        WorldEdge(id=f"edge-{index}", project_id="p", source_id=source, target_id=target, edge_family="social",
                  edge_type="trusts", is_active=active)
        for index, (source, target, active) in enumerate(edges)
    ])
    runtime = BookStateRuntime(project_id="p", as_of_chapter=3, world=world, map_graph=MapGraph())
    ranked = [node.id for node in rank_actors(runtime, now=NOW, limit=10)]
    assert ranked == ["a-faction", "z-major", "minor-high", "minor-mid", "minor-alpha", "minor-low", "background"]
    assert [node.id for node in rank_actors(runtime, now=NOW, limit=3)] == ["a-faction", "z-major", "minor-high"]


# views


def test_actor_view_hides_facts_the_actor_does_not_know():
    view = _view()
    assert [fact["ref"] for fact in view.facts] == [
        "fact:false-certainty", "fact:false-rumor", "fact:heard-rumor", "fact:hero-secret", "fact:public-heard",
    ]
    assert [fact["ref"] for fact in view.public_facts] == ["fact:public-news"]
    assert view.public_facts[0] == {"ref": "fact:public-news", "proposition": "public-proposition",
                                    "related_refs": ["rival"]}
    dumped = json.dumps(view.model_dump(mode="json"))
    for absent in ("rival-private-truth", "fact:rival-secret", "hidden-agent", "disbelieved-proposition",
                   "reader-only-sentinel", "reader-private-cognition"):
        assert absent not in dumped
    assert {entity["id"] for entity in view.entities} == {"rival", "ally"}
    assert view.holdings == [{"id": "blade", "node_type": "item", "name": "blade-name"}]
    assert [agenda["id"] for agenda in view.agendas] == ["hero/agenda"]
    assert view.location_id == "home" and view.resources == ["coin-pouch"]
    assert view.rules == [{"id": "rule-1", "hardness": "hard", "statement": "rule-statement"}]

    client = StubLLM({"turns": [_turn()]})
    [call] = _run(client, [view])
    trace = json.dumps(call.prompt_trace, ensure_ascii=False)
    for absent in ("rival-private-truth", "fact:rival-secret", "hidden-agent", "reader-only-sentinel",
                   "reader-private-cognition", "overlay-reader"):
        assert absent not in trace
    assert "hero-known-secret" in trace


def test_actor_view_presents_false_belief_as_believed():
    view = _view()
    facts = {fact["ref"]: fact for fact in view.facts}
    assert facts["fact:false-rumor"] == {"ref": "fact:false-rumor", "proposition": "false-proposition",
                                         "state": "believes"}
    assert facts["fact:false-certainty"]["state"] == "knows"
    assert facts["fact:heard-rumor"]["state"] == "heard"
    assert all("truth_value" not in fact for fact in [*view.facts, *view.public_facts])
    assert "truth_value" not in json.dumps(view.model_dump(mode="json"))


def test_actor_view_lists_only_known_clocks_and_vetoes():
    vetoes = [
        _veto("own-veto", intent_ref="hero-old-intent", story_time=520),
        _veto("proposed-veto", proposed={"new_intents": [{"summary": "proposed-intent-summary"}]}, story_time=510),
        _veto("other-option", option="plot_responds"),
        _veto("other-actor", actor="rival"),
        _veto("still-open", status="open"),
    ]
    view = _view(vetoes=vetoes, boundary_refs=["rival", "fact:rival-secret", "rival"])
    assert [clock["id"] for clock in view.clocks] == ["own-clock", "confidant-clock", "public-clock"]
    clocks = {clock["id"]: clock for clock in view.clocks}
    assert clocks["own-clock"] == {
        "id": "own-clock", "label": "own-deadline", "scope": "secret", "due_at": 700, "remaining_minutes": 100,
        "owner_ref": "hero", "on_expire": "own-deadline-effect",
    }
    assert clocks["confidant-clock"]["owner_ref"] == "ally"
    assert "owner_ref" not in clocks["public-clock"] and "on_expire" not in clocks["public-clock"]
    assert view.vetoes == [
        {"summary": "own-veto-summary", "intent_summaries": ["vetoed-intent-summary"]},
        {"summary": "proposed-veto-summary", "intent_summaries": ["proposed-intent-summary"]},
    ]
    assert view.boundary_refs == ["fact:rival-secret", "rival"]
    dumped = json.dumps(view.model_dump(mode="json"))
    for absent in ("rival-deadline", "unscoped-deadline", "expired-deadline", "other-option", "other-actor",
                   "still-open"):
        assert absent not in dumped


def test_actor_view_keeps_the_most_recent_vetoes():
    from forwin.world_engine.actor_turns import MAX_RECENT_VETOES
    vetoes = [_veto(f"veto-{index}", story_time=500 + index) for index in range(MAX_RECENT_VETOES + 2)]
    view = _view(vetoes=vetoes)
    assert [veto["summary"] for veto in view.vetoes] == [
        f"veto-{index}-summary" for index in reversed(range(2, MAX_RECENT_VETOES + 2))
    ]


def test_actor_view_lists_nearest_places_with_known_routes():
    from forwin.world_engine.actor_turns import MAX_REACHABLE_PLACES
    assert MAX_REACHABLE_PLACES == 12
    runtime = _runtime()
    view = _view(runtime=runtime)
    assert view.reachable_places == [
        {"location_id": "near", "name": "near-name", "minutes": 60},
        {"location_id": "far", "name": "far-name", "minutes": 240},
    ]
    assert view.location_name == "home-name"
    # Without a cognition overlay the objective map holds, hidden routes included.
    ally = _view("ally", runtime)
    assert [place["location_id"] for place in ally.reachable_places] == ["home", "hidden-place", "far"]

    for index in range(14):
        runtime.map.add_node(_place(f"spoke-{index:02d}"))
        runtime.map.add_edge(_route(f"to-spoke-{index}", "home", f"spoke-{index:02d}", 2 + index / 10))
    places = _view(runtime=runtime).reachable_places
    assert len(places) == 12
    assert [place["location_id"] for place in places][:3] == ["near", "spoke-00", "spoke-01"]
    assert all(place["location_id"] != "hidden-place" for place in places)
    assert [place["minutes"] for place in places] == sorted(place["minutes"] for place in places)


# runner


def test_runner_batches_and_caps_new_intents():
    runtime = _runtime()
    views = [_view(actor, runtime) for actor in ("hero", "rival", "ally")]
    client = StubLLM(
        {"turns": [_turn(new_intents=[_intent()] * 3), _turn("rival", next_turn_in={"amount": 2, "unit": "watch"})]},
        {"turns": [_turn(new_intents=[_intent(), _intent(summary="second-intent")])]},
        {"turns": [_turn("ally")]},
    )
    calls = _run(client, views, policy=WorldPolicy(actor_turn_batch_size=2, max_new_intents_per_actor=2))
    assert [call.actor_id for call in calls] == ["hero", "rival", "ally"]
    assert all(call.result is not None and call.issues == [] for call in calls)
    assert [intent.summary for intent in calls[0].result.new_intents] == ["intent-summary", "second-intent"]
    assert len(client.calls) == 3
    assert [[actor["actor_id"] for actor in _user_payload(call)["actors"]] for call in client.calls] == [
        ["hero", "rival"], ["hero"], ["ally"],
    ]
    retry = _retry_feedback(client.calls[1])
    assert "hero" in retry and "at most 2" in retry and "rival" not in retry
    assert _user_payload(client.calls[0])["max_new_intents_per_actor"] == 2
    assert calls[0].prompt_trace is calls[1].prompt_trace
    assert calls[0].prompt_trace["input_snapshot"]["actor_ids"] == ["hero", "rival"]
    assert len(calls[0].prompt_trace["attempts"]) == 2
    assert len(calls[2].prompt_trace["attempts"]) == 1
    assert calls[2].prompt_trace["input_snapshot"]["actor_ids"] == ["ally"]

    over = {"turns": [_turn(new_intents=[_intent()] * 3)]}
    [capped] = _run(StubLLM(over, over), [views[0]])
    assert capped.result is None
    assert any("at most 2" in issue for issue in capped.issues)


@pytest.mark.parametrize("overrides,token", [
    ({"new_intents": [_intent(scheduled_in={"amount": 1, "unit": "fortnight"})]}, "fortnight"),
    ({"next_turn_in": {"amount": 1, "unit": "fortnight"}}, "fortnight"),
    ({"new_intents": [_intent(target_refs=["hidden-agent"])]}, "hidden-agent"),
    ({"new_intents": [_intent(target_refs=["fact:rival-secret"])]}, "fact:rival-secret"),
    ({"new_intents": [_intent(location_id="hidden-place")]}, "hidden-place"),
    ({"new_intents": [_intent(agenda_ref="rival/agenda")]}, "rival/agenda"),
    ({"new_intents": [_intent(expected_effects={"knowledge": [{
        "observer_ref": "hidden-agent", "fact_ref": "fact:hero-secret", "state": "knows", "channel": "told",
    }]})]}, "hidden-agent"),
    ({"new_intents": [_intent(expected_effects={"movements": [{
        "entity_ref": "hero", "to_location_id": "hidden-place", "depart_at": 600, "arrive_at": 630,
    }]})]}, "hidden-place"),
    ({"agenda_changes": [{"agenda_ref": "rival/agenda", "status": "abandoned"}]}, "rival/agenda"),
])
def test_unknown_unit_or_ref_retries_with_feedback(overrides, token):
    good = {"turns": [_turn(new_intents=[_intent()])]}
    client = StubLLM({"turns": [_turn(**overrides)]}, good)
    [call] = _run(client)
    assert call.result is not None and call.issues == []
    assert len(client.calls) == 2
    retry = _retry_feedback(client.calls[1])
    assert retry.startswith("hero:") and token in retry
    assert len(call.prompt_trace["output_summary"]["business_retry_events"]) == 1

    bad = {"turns": [_turn(**overrides)]}
    [dropped] = _run(StubLLM(bad, bad))
    assert dropped.result is None
    assert any(token in issue for issue in dropped.issues)


@pytest.mark.parametrize("overrides,token", [
    ({"new_intents": [_intent(agenda_ref="new")]}, "found 0"),
    ({"new_intents": [_intent(agenda_ref="new")],
      "agenda_changes": [{"agenda_ref": "new", "goal": "goal-a"}, {"agenda_ref": "new", "goal": "goal-b"}]}, "found 2"),
    ({"agenda_changes": [{"agenda_ref": "hero/agenda", "priority": 6}]}, "priority"),
    ({"agenda_changes": [{"agenda_ref": "new", "goal": "goal-a", "priority": 0}]}, "priority"),
])
def test_new_agenda_and_priority_rules_retry_with_feedback(overrides, token):
    client = StubLLM({"turns": [_turn(**overrides)]}, {"turns": [_turn()]})
    [call] = _run(client)
    assert call.result is not None and len(client.calls) == 2
    assert token in _retry_feedback(client.calls[1])


def test_new_agenda_turn_builds_into_one_agenda_and_its_intent():
    from forwin.world_engine.actor_turns import turn_to_candidate
    from forwin.world_engine.delta_builder import _TickBuilder
    runtime = _runtime()
    turn = _turn(new_intents=[_intent(agenda_ref="new")],
                 agenda_changes=[{"agenda_ref": "new", "goal": "fresh-goal", "priority": 5}])
    [call] = _run(StubLLM({"turns": [turn]}), [_view(runtime=runtime)])
    assert call.issues == []
    candidate = turn_to_candidate(call, now=NOW, calendar=CALENDAR, lexicon=LEXICON)
    build = _TickBuilder(context=CONTEXT, tick_id="tick-1", runtime=runtime, calendar=CALENDAR,
                         lexicon=LEXICON).build([candidate], [])
    assert build.issues == []
    created = {patch.node_type: patch.new_value for patch in build.delta.node_patches if patch.op == "create"}
    assert created["agenda"]["profile"]["goal"] == "fresh-goal"
    assert created["intent"]["state"]["scheduled_at"] == NOW + 60
    agenda_id = next(patch.node_id for patch in build.delta.node_patches if patch.node_type == "agenda")
    assert created["intent"]["profile"]["agenda_ref"] == agenda_id


def test_missing_or_foreign_turn_retries_and_duplicate_keeps_first():
    client = StubLLM({"turns": [_turn("rival")]}, {"turns": [_turn(), _turn(next_turn_in={"amount": 9, "unit": "cycle"})]})
    [call] = _run(client)
    assert "missing" in _retry_feedback(client.calls[1]).lower()
    assert call.result.next_turn_in.amount == 1
    assert any("duplicate" in issue for issue in call.issues)


def test_runner_call_kwargs_trace_and_allowed_values():
    client = StubLLM({"turns": [_turn()]})
    client.last_call_result = SimpleNamespace(
        backend="codex_bridge", actual_model="test-actual-model", fallback_used=False, trace={"codex_job_id": "job-1"},
    )
    [call] = _run(client, max_tokens_per_actor=4321)
    messages, kwargs = client.calls[0]
    assert kwargs["stage_key"] == "world.actor_turn"
    assert kwargs["task_family"] == "world_engine"
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["output_schema"] == ActorTurnBatch.model_json_schema()
    assert kwargs["max_tokens"] == 4321
    trace = call.prompt_trace
    assert trace["trace_scope"] == "world_engine" and trace["stage_key"] == "world.actor_turn"
    assert trace["template_id"] == "world_engine:actor_turn"
    assert trace["backend"] == "codex_bridge" and trace["codex_job_id"] == "job-1"
    assert trace["model_profile"]["actual_model"] == "test-actual-model"
    assert trace["input_snapshot"]["seed"] == 77
    assert trace["attempts"][0]["provider_request_id"] == "1"
    assert trace["output_summary"]["actor_turn_ok"] is True
    assert client.llm_attempt_events == []
    allowed = json.loads(messages[0]["content"].split("Allowed values:\n")[1])
    assert allowed == {
        "action_type": list(INTENT_ACTION_TYPES), "impact_level": list(IMPACT_LEVELS),
        "agenda_status": list(AGENDA_STATUSES), "agenda_priority": [1, 5],
        "knowledge_state": list(KNOWLEDGE_STATES), "knowledge_channel": list(KNOWLEDGE_CHANNELS),
        "edge_type": sorted(WORLD_EDGE_TYPES), "new_agenda_ref": "new", "new_fact_ref_prefix": "fact:new:",
    }
    payload = _user_payload(client.calls[0])
    assert payload["calendar"]["units"] == [{"name": "cycle", "minutes": 120}]
    assert payload["lexicon"]["time_unit_aliases"] == [{"name": "watch", "minutes": 30}]
    [actor] = payload["actors"]
    assert actor["actor_id"] == "hero" and actor["reachable_places"][0]["minutes"] == 60
    assert all(value not in (None, "", [], {}) for value in actor.values())


def test_feedback_is_rendered_only_for_actors_in_the_batch():
    feedback = {"hero": [ConstraintIssue("dead_owner_acts", "hero-feedback", "hero")],
                "rival": [ConstraintIssue("time_not_monotonic", "rival-feedback", "rival")]}
    client = StubLLM({"turns": [_turn()]})
    _run(client, feedback=feedback)
    user = client.calls[0][0][-1]["content"]
    section = json.loads(user.split("Constraint feedback by actor:\n")[1].split("\n\n")[0])
    assert section == {"hero": [{"code": "dead_owner_acts", "message": "hero-feedback", "ref": "hero"}]}
    client = StubLLM({"turns": [_turn()]})
    _run(client, feedback={"hero": []})
    assert "Constraint feedback by actor:" not in client.calls[0][0][-1]["content"]


def test_transport_error_retries_without_model_feedback():
    client = StubLLM(TimeoutError("https://private.invalid/?token=secret"), {"turns": [_turn()]})
    [call] = _run(client)
    assert call.result is not None and call.issues == []
    assert client.calls[0][0] == client.calls[1][0]
    assert call.prompt_trace["attempts"][0]["error_message"] == "TimeoutError"
    assert "private.invalid" not in json.dumps(call.prompt_trace)


def test_unparseable_output_exhausts_attempts():
    client = StubLLM("not JSON", "still not JSON")
    [call] = _run(client)
    assert call.result is None
    assert any("failed after 2 attempts" in issue for issue in call.issues)
    attempts = call.prompt_trace["attempts"]
    assert [item["workflow_attempt_no"] for item in attempts] == [1, 2]
    assert all(item["parse_ok"] is False for item in attempts)
    assert attempts[0]["parse_error"] in client.calls[1][0][-1]["content"]
    assert call.prompt_trace["output_summary"]["actor_turn_ok"] is False
    assert call.prompt_trace["output_summary"]["missing_actor_ids"] == ["hero"]


def test_public_facts_are_dropped_before_the_size_guard():
    runtime = _runtime()
    # Even capped, these public facts overflow the guard, so they go as a last resort.
    for index in range(40):
        runtime.world.add_fact(_fact(f"bulk-{index:03d}", "bulk-public-proposition-" * 100, sensitivity="public"))
    client = StubLLM({"turns": [_turn()]})
    [call] = _run(client, [_view(runtime=runtime)])
    assert call.result is not None and len(client.calls) == 1
    user = client.calls[0][0][-1]["content"]
    assert len(user) <= 60000
    assert "bulk-public-proposition" not in user and "hero-known-secret" in user


def test_oversized_prompt_returns_failure_without_llm_call():
    runtime = _runtime()
    runtime.world.states_by_node_id["hero"]["resources"] = ["x" * 60001]
    client = StubLLM({"turns": [_turn()]})
    [call] = _run(client, [_view(runtime=runtime)])
    assert client.calls == []
    assert call.result is None and call.issues == ["prompt_too_large"]
    assert call.prompt_trace["attempts"] == []
    assert call.prompt_trace["output_summary"]["failure_reason"] == "prompt_too_large"


def test_no_views_make_no_call():
    client = StubLLM()
    assert _run(client, []) == []
    assert client.calls == []


def test_repeated_actor_views_are_rejected():
    view = _view()
    client = StubLLM()
    with pytest.raises(ValueError):
        _run(client, [view, view], policy=WorldPolicy(actor_turn_batch_size=2))
    assert client.calls == []


# candidates


@pytest.mark.parametrize("wait,expected", [
    ({"amount": 2, "unit": "watch"}, NOW + 60),
    ({"amount": 0, "unit": "cycle"}, NOW + 1),
    ({"amount": -3, "unit": "cycle"}, NOW + 1),
])
def test_turn_to_candidate_schedules_next_turn_after_now(wait, expected):
    from forwin.world_engine.actor_turns import turn_to_candidate
    turn = _turn(new_intents=[_intent(), _intent(summary="second-intent")], next_turn_in=wait,
                 agenda_changes=[{"agenda_ref": "hero/agenda", "priority": 2}])
    [call] = _run(StubLLM({"turns": [turn]}))
    candidate = turn_to_candidate(call, now=NOW, calendar=CALENDAR, lexicon=LEXICON)
    assert candidate.origin == "actor_turn"
    assert candidate.candidate_id == "actor_turn:hero" and candidate.actor_id == "hero"
    assert candidate.next_turn_at == {"hero": expected}
    assert candidate.new_intents == call.result.new_intents
    assert candidate.agenda_changes == call.result.agenda_changes
    assert candidate.summary == "intent-summary\nsecond-intent"
    assert candidate.effects.model_dump(exclude_defaults=True) == {}
    assert candidate.impact_level == "minor"


def test_turn_to_candidate_requires_a_result():
    from forwin.world_engine.actor_turns import ActorTurnCall, turn_to_candidate
    with pytest.raises(ValueError):
        turn_to_candidate(ActorTurnCall("hero", None, {}, ["dropped"]), now=NOW, calendar=CALENDAR, lexicon=LEXICON)


# fix round 1


def test_unrecorded_public_event_facts_wait_for_news():
    runtime = _runtime()
    runtime.world.add_fact(_fact("public-event", "event-news-proposition", sensitivity="public", kind="event"))
    view = _view(runtime=runtime)
    assert "event-news-proposition" not in json.dumps(view.model_dump(mode="json"))
    assert "fact:public-event" not in view.references()
    # Static public facts keep the default.
    assert [fact["ref"] for fact in view.public_facts] == ["fact:public-news"]

    runtime.cognition_by_observer[("character", "hero")].knowledge_by_ref["fact:public-event"] = "heard"
    heard = {fact["ref"]: fact for fact in _view(runtime=runtime).facts}
    assert heard["fact:public-event"]["state"] == "heard"


def test_actor_view_lists_own_pending_intents():
    from forwin.world_engine.actor_turns import MAX_PENDING_INTENTS
    assert MAX_PENDING_INTENTS == 10
    runtime = _runtime()
    runtime.world.add_node(_node("warden", name="warden-name", location_id="far"))

    def intent(ref, owner="hero", status="planned", scheduled_at=700, **profile):
        runtime.world.add_node(_node(ref, "intent", profile={
            "owner_ref": owner, "summary": f"{ref}-summary", "action_type": "investigate",
            "target_refs": ["warden"], "location_id": "far", **profile,
        }, status=status, scheduled_at=scheduled_at))

    intent("due-intent", status="due", scheduled_at=650)
    intent("held-intent", status="held", scheduled_at=660)
    intent("resolved-intent", status="resolved", scheduled_at=600)
    intent("rival-intent", owner="rival", scheduled_at=600)
    for index in range(10):
        intent(f"planned-{index:02d}", scheduled_at=700 + index)
    view = _view(runtime=runtime)
    assert [item["id"] for item in view.pending_intents] == [
        "due-intent", "held-intent", *[f"planned-{index:02d}" for index in range(8)],
    ]
    assert view.pending_intents[0] == {
        "id": "due-intent", "summary": "due-intent-summary", "action_type": "investigate", "location_id": "far",
        "scheduled_at": 650, "status": "due", "target_refs": ["warden"],
    }
    assert "warden" in {entity["id"] for entity in view.entities}
    assert {"warden", "due-intent"} <= view.references()

    client = StubLLM({"turns": [_turn(new_intents=[_intent(target_refs=["warden"])])]})
    [call] = _run(client, [view])
    assert call.issues == []
    system, user = (message["content"] for message in client.calls[0][0])
    assert "pending_intents" in system
    assert "due-intent-summary" in user
    assert "rival-intent-summary" not in user and "resolved-intent-summary" not in user


def test_fact_lists_are_capped_by_recency():
    from forwin.world_engine.actor_turns import MAX_KNOWN_FACTS, MAX_PUBLIC_FACTS
    runtime = _runtime()
    overlay = runtime.cognition_by_observer[("character", "hero")]
    for index in range(500):
        runtime.world.add_fact(_fact(f"news-{index:03d}", f"recorded-news-{index:03d}-" * 4, chapter=index))
        overlay.knowledge_by_ref[f"fact:news-{index:03d}"] = "heard"
    for index in range(100):
        runtime.world.add_fact(_fact(f"lore-{index:03d}", f"public-lore-{index:03d}-" * 4, sensitivity="public",
                                     chapter=index))
    view = _view(runtime=runtime)
    assert len(view.facts) == MAX_KNOWN_FACTS
    assert view.facts[0]["ref"] == "fact:news-499"
    assert view.facts[-1]["ref"] == f"fact:news-{500 - MAX_KNOWN_FACTS:03d}"
    assert len(view.public_facts) == MAX_PUBLIC_FACTS
    assert view.public_facts[0]["ref"] == "fact:lore-099"

    client = StubLLM({"turns": [_turn()]})
    [call] = _run(client, [view])
    assert len(client.calls) == 1 and call.result is not None
    user = client.calls[0][0][-1]["content"]
    assert len(user) <= 60000
    assert "recorded-news-499" in user and "public-lore-099" in user
    assert "recorded-news-000" not in user


def test_actor_view_lists_only_active_agendas():
    runtime = _runtime()
    runtime.world.add_node(_agenda("hero/abandoned", "hero", "abandoned-goal"))
    runtime.world.states_by_node_id["hero/abandoned"]["status"] = "abandoned"
    runtime.world.add_node(_node("hero/retired", "agenda", is_active=False,
                                 profile={"owner_ref": "hero", "goal": "retired-goal"}, status="active"))
    view = _view(runtime=runtime)
    assert [agenda["id"] for agenda in view.agendas] == ["hero/agenda"]
    bad = {"turns": [_turn(new_intents=[_intent(agenda_ref="hero/abandoned")])]}
    client = StubLLM(bad, {"turns": [_turn()]})
    _run(client, [view])
    assert "hero/abandoned" in _retry_feedback(client.calls[1])


def test_restraint_captor_and_clock_expiry_node_are_usable_refs():
    runtime = _runtime()
    runtime.world.add_node(_node("warden", name="warden-name"))
    runtime.world.add_node(_node("trigger", "event", name="trigger-name"))
    runtime.world.states_by_node_id["hero"]["restraint"] = {"state": "restrained", "by_ref": "warden", "since": 1}
    runtime.world.get_node("own-clock").profile["on_expire"] = "trigger"
    view = _view(runtime=runtime)
    assert view.restraint["by_ref"] == "warden"
    assert {"warden", "trigger"} <= {entity["id"] for entity in view.entities}
    client = StubLLM({"turns": [_turn(new_intents=[_intent(target_refs=["warden", "trigger"])])]})
    [call] = _run(client, [view])
    assert call.issues == [] and len(client.calls) == 1


def test_max_tokens_scale_with_the_actors_asked():
    runtime = _runtime()
    views = [_view(actor, runtime) for actor in ("hero", "rival", "ally")]
    client = StubLLM(
        {"turns": [_turn(new_intents=[_intent()] * 3), _turn("rival")]},
        {"turns": [_turn()]},
        {"turns": [_turn("ally")]},
    )
    _run(client, views, policy=WorldPolicy(actor_turn_batch_size=2), max_tokens_per_actor=3000)
    assert [kwargs["max_tokens"] for _, kwargs in client.calls] == [6000, 3000, 3000]
    client = StubLLM({"turns": [_turn(), _turn("rival"), _turn("ally")]})
    _run(client, views, policy=WorldPolicy(actor_turn_batch_size=3), max_tokens_per_actor=6000, max_tokens=16000)
    assert client.calls[0][1]["max_tokens"] == 16000


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field", ["scheduled_in", "next_turn_in"])
def test_non_finite_duration_retries_with_feedback(amount, field):
    wait = {"amount": amount, "unit": "watch"}
    bad = _turn(new_intents=[_intent(scheduled_in=wait)]) if field == "scheduled_in" else _turn(next_turn_in=wait)
    client = StubLLM({"turns": [bad]}, {"turns": [_turn()]})
    [call] = _run(client)
    assert call.result is not None and len(client.calls) == 2
    retry = _retry_feedback(client.calls[1])
    assert "invalid_duration" in retry and field in retry and "finite" in retry


def test_seed_knowledge_outlasts_newer_event_news_under_the_cap():
    from forwin.world_engine.actor_turns import MAX_KNOWN_FACTS
    runtime = _runtime()
    overlay = runtime.cognition_by_observer[("character", "hero")]
    runtime.world.add_fact(_fact("seed-secret", "seed-secret-proposition", chapter=0))
    overlay.knowledge_by_ref["fact:seed-secret"] = "knows"
    for index in range(MAX_KNOWN_FACTS + 5):
        runtime.world.add_fact(_fact(f"event-{index:03d}", f"event-news-{index:03d}", sensitivity="public",
                                     chapter=index + 1, kind="event"))
        overlay.knowledge_by_ref[f"fact:event-{index:03d}"] = "heard"
    refs = [fact["ref"] for fact in _view(runtime=runtime).facts]
    assert len(refs) == MAX_KNOWN_FACTS
    # Non-event knowledge first, newest first; then event news, newest first.
    assert refs[:6] == ["fact:false-certainty", "fact:false-rumor", "fact:heard-rumor", "fact:hero-secret",
                        "fact:public-heard", "fact:seed-secret"]
    assert refs[6] == f"fact:event-{MAX_KNOWN_FACTS + 4:03d}"
    assert all(f"fact:event-{index:03d}" not in refs for index in range(11))
