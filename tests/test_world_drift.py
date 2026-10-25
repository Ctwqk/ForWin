from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.map_graph import MapGraph
from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
from forwin.book_state.story_clock import CalendarUnit, StoryCalendar
from forwin.protocol.book_state import MapNode, WorldNode
from forwin.runtime.policy import RuntimePolicy
from forwin.world_engine.constraints import check_hard_rules, classify_impact
from forwin.world_engine.tick import _Tick
from forwin.world_engine.types import EffectEntityChange, TickContext
from tests.test_world_adjudicator import StubLLM


CONTEXT = TickContext(project_id="p", chapter_number=1, now=120, window_start=60, seed=42)
CALENDAR = StoryCalendar("epoch", 0, (CalendarUnit("minute", 1),))


def variable(ref="condition", last=0, **state):
    return WorldNode(id=ref, project_id="p", node_type="world_variable", profile={
        "label": "condition-label", "source_kind": "institution", "source_profile_id": "institution-1",
        "scale": ["low", "middle", "high", "peak"], "scope_refs": ["site"], "holder_refs": ["holder"],
        "visibility": "public", "drift_tendency": "stable", "summary": "condition-summary",
    }, state={"level": 1, "last_drift_at": last, "trend": "stable", "status": "active", **state})


def runtime(*variables):
    return BookStateRuntime(project_id="p", as_of_chapter=1, world=ObjectiveWorldGraph(nodes=[
        *variables,
        WorldNode(id="holder", project_id="p", node_type="character"),
        WorldNode(id="local", project_id="p", node_type="character", state={"location_id": "site"}),
        WorldNode(id="outsider", project_id="p", node_type="character"),
    ]), map_graph=MapGraph(nodes=[MapNode(id="site", project_id="p", node_type="site")]))


def change(**kwargs):
    return {"variable_id": "condition", "new_level": 2, "trend": "rising", "summary": "drift-summary", **kwargs}


def tick(client, world):
    from forwin.world_engine.drift import WorldDriftRunner
    traces, drops = [], []
    obj = object.__new__(_Tick)
    obj.runtime, obj.context, obj.calendar, obj.lexicon = world, CONTEXT, CALENDAR, BookLexicon()
    obj.policy = RuntimePolicy.for_profile("standard")
    obj.service = SimpleNamespace(llm_client=client, drift_runner=WorldDriftRunner(client))
    obj._check_snapshot = lambda: None
    obj._save_trace = lambda trace: traces.append(trace) or "trace"
    obj._drop = lambda candidate_id, **kw: drops.append((candidate_id, kw))
    obj._issues = lambda candidate: check_hard_rules(
        world, obj.lexicon, candidate, now=120, calendar=CALENDAR, window_start=60)
    return obj, traces, drops


def test_due_selection_and_disabled_at_zero():
    from forwin.world_engine.drift import due_world_variables
    world = runtime(variable("b", 60), variable("a", 0), variable("future", 61),
                    variable("inactive", 0, status="inactive"))
    world.world.states_by_node_id["a"]["last_drift_at"] = 10
    assert [node.id for node in due_world_variables(world, now=120, every=60, limit=2)] == ["a", "b"]
    assert [node.id for node in due_world_variables(world, now=120, every=60, limit=1)] == ["a"]
    assert due_world_variables(world, now=120, every=0, limit=3) == []
    assert due_world_variables(world, now=120, every=60, limit=0) == []


@pytest.mark.parametrize("bad,code", [
    ({"new_level": 3}, "drift_step_too_large"),
    ({"new_level": -1}, "drift_level_out_of_range"),
    ({"events": [{"summary": "event", "participant_refs": ["outsider"], "location_id": "site",
                  "visibility": "public", "at": 120}]}, "drift_participant_out_of_scope"),
])
def test_drift_step_too_large_retries_then_drops(bad, code):
    client = StubLLM({"changes": [change(**bad)]}, {"changes": [change(**bad)]})
    obj, traces, drops = tick(client, runtime(variable()))
    assert obj._check(obj._settled(obj._drift([obj.runtime.world.get_node("condition")]))) == []
    assert len(client.calls) == 2
    assert code in client.calls[1][0][-1]["content"]
    assert drops[0][0] == "drift:condition"
    assert code in {issue.code for issue in drops[0][1]["issues"]}
    assert len(traces) == 2
    assert all(trace["stage_key"] == "world.drift" and trace["attempts"] for trace in traces)


def test_llm_outage_keeps_last_drift_at():
    world = runtime(variable())
    obj, traces, drops = tick(StubLLM(ConnectionError(), ConnectionError()), world)
    assert obj._settled(obj._drift([world.world.get_node("condition")])) == []
    assert world.world.get_state("condition")["last_drift_at"] == 0
    assert drops[0][1]["reason"] == "llm_unavailable"
    assert len(traces[0]["attempts"]) == 2


def test_drift_retry_can_repair_and_uses_current_state():
    world = runtime(variable())
    obj, traces, drops = tick(StubLLM({"changes": [change(new_level=3)]}, {"changes": [change()]}), world)
    candidates = obj._check(obj._settled(obj._drift([world.world.get_node("condition")])))
    assert not drops and len(candidates) == 1
    assert {item.field: item.new_value for item in candidates[0].effects.entity_changes} == {
        "level": 2, "trend": "rising", "last_drift_at": 120}
    assert classify_impact(world, candidates[0]) == "moderate"
    candidates[0].effects.entity_changes.append(EffectEntityChange(
        ref="condition", field="holder_refs", new_value=["local"]))
    assert classify_impact(world, candidates[0]) == "major"
    payload = json.JSONDecoder().raw_decode(traces[0]["prompt_layers"][-1]["content"])[0]
    assert payload["variables"][0]["state"]["level"] == 1


def test_drift_events_allow_holder_and_actor_in_scope():
    from forwin.world_engine.drift import DriftChange, drift_candidate
    world = runtime(variable())
    candidate = drift_candidate(world.world.get_node("condition"), DriftChange(**change(events=[{
        "summary": "event", "participant_refs": ["holder", "local"], "location_id": "site",
        "visibility": "public", "at": 120,
    }])), now=120)
    assert check_hard_rules(world, BookLexicon(), candidate, now=120, calendar=CALENDAR, window_start=60) == []
    assert not candidate.new_intents


def test_drift_prompt_includes_recent_public_holder_events():
    from forwin.writer.prompt_core.world_engine import build_drift_messages

    world = runtime(variable())
    for ref, visibility in [("public-event", "public"), ("secret-event", "secret")]:
        node = WorldNode(id=ref, project_id="p", node_type="event", profile={"location_id": "elsewhere"},
                         state={"participant_ids": ["holder"], "visibility_level": visibility},
                         created_at_chapter=1)
        world.world.nodes_by_id[ref] = node
        world.world.states_by_node_id[ref] = node.state
    messages = build_drift_messages(context=CONTEXT, variables=[world.world.get_node("condition")],
                                    runtime=world, lexicon=BookLexicon(), calendar=CALENDAR)
    assert [node["id"] for node in json.loads(messages[-1]["content"])["recent_public_events"]] == ["public-event"]


def test_stale_snapshot_stops_before_drift_call():
    from forwin.canon import CanonStaleVersion

    obj, traces, drops = tick(StubLLM({"changes": [change()]}), runtime(variable()))
    def stale():
        raise CanonStaleVersion("stale")
    obj._check_snapshot = stale
    with pytest.raises(CanonStaleVersion):
        obj._drift([obj.runtime.world.get_node("condition")])
    assert traces == [] and drops == []


def test_no_model_leaves_drift_due_next_tick():
    from forwin.world_engine.drift import due_world_variables

    obj, traces, drops = tick(StubLLM(), runtime(variable()))
    obj.service.llm_client = None
    assert obj._settled(obj._drift([obj.runtime.world.get_node("condition")])) == []
    assert [node.id for node in due_world_variables(obj.runtime, now=120, every=60, limit=3)] == ["condition"]
    assert traces == [] and drops[0][1]["reason"] == "llm_unavailable"


@pytest.mark.parametrize("genre", ["xianxia", "interstellar", "urban_mystery"])
def test_genre_variable_drifts_through_shared_builder(genre):
    from pathlib import Path
    from forwin.world_engine.delta_builder import _TickBuilder
    from forwin.world_engine.drift import WorldDriftRunner, drift_candidate

    pack = json.loads((Path(__file__).parent / "fixtures" / "world_seed" / f"{genre}.json").read_text())
    seed = pack["pack"]["world_seed"]["world_variables"][0]
    node = variable()
    node.profile.update({key: seed[key] for key in ("label", "scale", "drift_tendency", "visibility")})
    node.state["level"] = seed["level"]
    world = runtime(node)
    new_level = 1 if seed["level"] == 0 else seed["level"] - 1
    client = StubLLM({"changes": [change(new_level=new_level, trend="stable")]})
    call = WorldDriftRunner(client).drift(context=CONTEXT, variables=[node], runtime=world,
                                        lexicon=BookLexicon(), calendar=CALENDAR)
    candidate = drift_candidate(node, call.result.changes[0], now=120)
    built = _TickBuilder(context=CONTEXT, tick_id="tick-test", runtime=world,
                         calendar=CALENDAR, lexicon=BookLexicon()).build([candidate], [])
    assert built.delta is not None, built.issues
    assert {patch.field_path: (patch.old_value, patch.new_value) for patch in built.delta.node_patches} == {
        "state.level": (seed["level"], new_level), "state.trend": ("stable", "stable"),
        "state.last_drift_at": (0, 120),
    }
    assert client.calls[0][1]["max_tokens"] == 4000
    assert seed["label"] in client.calls[0][0][-1]["content"]
