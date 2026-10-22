from __future__ import annotations

import pytest

from forwin.book_state.cognition import CognitionView
from forwin.book_state.lexicon import BookLexicon, TimeUnitAlias
from forwin.book_state.map_graph import MapGraph
from forwin.book_state.narrative import NarrativeControlGraph
from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
from forwin.book_state.story_clock import CalendarUnit, StoryCalendar
from forwin.protocol.book_state import CognitionOverlay, FactNode, MapEdge, MapNode, NarrativeNode, WorldNode
from forwin.world_engine.types import TickCandidate, WorldEffects


NOW = 1000
LEXICON = BookLexicon(time_unit_aliases=(TimeUnitAlias("minute", 1), TimeUnitAlias("quarter", 15)))
CALENDAR = StoryCalendar("", 0, (CalendarUnit("cycle", 120),))


@pytest.fixture
def checks():
    # Import inside the fixture so the initial RED names each missing behavior.
    from forwin.world_engine import constraints
    return constraints


def _node(ref, kind="character", *, profile=None, **state):
    return WorldNode(id=ref, project_id="p", node_type=kind, profile=profile or {}, state=state)


def _runtime(*extra, protections=(), hidden_route=False):
    nodes = [
        _node("a", alive_status="alive", actor_tier="major", location_id="l1", restraint={"state": "free"}),
        _node("b", alive_status="alive", actor_tier="minor", location_id="l1"),
        _node("c", alive_status="alive", actor_tier="background", location_id="l1"),
        _node("f", "faction", controlled_by_id="a"),
        _node("o", "organization", controlled_by_id="a"),
        _node("g", "group", controlled_by_id="a"),
        _node("s", "site_state"), _node("item", "item", holder_id="a"),
        _node("resource", "resource", holder_id="a"),
        _node("agenda", "agenda", profile={"owner_ref": "a"}, priority=3),
        _node("intent", "intent", profile={"owner_ref": "a", "agenda_ref": "agenda"},
              status="due", scheduled_at=900),
    ]
    by_id = {node.id: node for node in [*nodes, *extra]}
    views = {}
    if hidden_route:
        views[("character", "a")] = CognitionView(CognitionOverlay(
            id="view", project_id="p", observer_type="character", observer_id="a",
        ))
    return BookStateRuntime(
        project_id="p", as_of_chapter=3,
        world=ObjectiveWorldGraph(nodes=list(by_id.values()), facts=[
            FactNode(id="secret", project_id="p", proposition="p", sensitivity_level="secret"),
            FactNode(id="hidden", project_id="p", proposition="p", sensitivity_level="hidden"),
            FactNode(id="public", project_id="p", proposition="p", sensitivity_level="public"),
        ]),
        map_graph=MapGraph(
            nodes=[MapNode(id=ref, project_id="p", node_type="site") for ref in ("l1", "l2", "l3")],
            edges=[MapEdge(id="road", project_id="p", from_node_id="l1", to_node_id="l2", edge_type="road",
                           travel_time=2, discovered_by_default=not hidden_route)],
        ),
        cognition_by_observer=views, narrative=NarrativeControlGraph(nodes=list(protections)),
    )


def _candidate(effects=None, **kwargs):
    return TickCandidate.model_validate({
        "candidate_id": "candidate", "origin": "adjudication", "summary": "s", "effects": effects or {}, **kwargs,
    })


def _change(ref, field, value):
    return {"entity_changes": [{"ref": ref, "field": field, "new_value": value}]}


def _intent(**kwargs):
    return {
        "agenda_ref": "agenda", "action_type": "move", "summary": "s", "location_id": "l2",
        "scheduled_in": {"amount": 120, "unit": "minute"}, "requires_onstage": False,
        "impact_level": "minor", "expected_effects": {}, **kwargs,
    }


def _movement(**kwargs):
    return {"entity_ref": "a", "to_location_id": "l2", "depart_at": 800, "arrive_at": 950, **kwargs}


def _event(**kwargs):
    return {"summary": "s", "participant_refs": ["b"], "location_id": "l1", "visibility": "public", "at": 950, **kwargs}


def _knowledge(**kwargs):
    return {"observer_ref": "b", "fact_ref": "fact:secret", "state": "knows", "channel": "witnessed", **kwargs}


def _protection(kind, ref="a", **kwargs):
    return NarrativeNode(id="protection", project_id="p", node_type="review_constraint", payload={
        "ref": ref, "kind": kind, "until_chapter": 4, "source": "test", **kwargs,
    })


@pytest.mark.parametrize(("effects", "expected"), [
    (_change("a", "alive_status", "dead"), "major"),
    (_change("b", "state.alive_status", "dead"), "major"),
    (_change("c", "alive_status", "dead"), "moderate"),
    *[(_change(ref, "is_active", False), "major") for ref in ("f", "o", "g", "s")],
    *[(_change(ref, "controlled_by_id", "b"), "major") for ref in ("f", "o", "g")],
    *[(_change(f"fact:{ref}", "sensitivity_level", "public"), "major") for ref in ("hidden", "secret")],
    ({"events": [_event(participant_refs=["fact:secret"])]}, "major"),
    *[({"relation_changes": [{"source_ref": "a", "target_ref": "b", "edge_type": "ally_of", "change": op}]},
       "moderate") for op in ("create", "end", "set")],
    *[(_change(ref, "holder_id", "b"), "moderate") for ref in ("item", "resource")],
    ({"item_transfers": [{"item_ref": "item", "from_ref": "a", "to_ref": "b"}]}, "moderate"),
    (_change("a", "restraint.state", "restrained"), "moderate"),
    (_change("a", "alive_status", "unknown"), "moderate"),
    (_change("a", "health", 0.5), "moderate"),
    (_change("a", "location_id", "l2"), "minor"),
    (_change("a", "next_turn_at", 1100), "minor"),
    (_change("a", "profile.fear", "f"), "minor"),
    (_change("f", "controlled_by_id", "a"), "minor"),
    ({"events": [_event(visibility="secret", participant_refs=["fact:secret"])]}, "minor"),
    ({"events": [_event(participant_refs=["fact:public"])]}, "minor"),
    ({}, "minor"),
])
def test_impact_rows(checks, effects, expected):
    assert checks.classify_impact(_runtime(), _candidate(effects)) == expected


@pytest.mark.parametrize("candidate", [
    _candidate(_change("a", "location_id", "l2")),
    _candidate({"knowledge": [_knowledge(fact_ref="fact:secret")]}),
    _candidate(next_turn_at={"a": 1100}),
    _candidate(new_intents=[_intent(target_refs=["a"])]),
])
def test_milestone_references_are_critical(checks, candidate):
    assert checks.classify_impact(_runtime(), candidate, milestone_refs=frozenset({"a", "secret"})) == "critical"


def test_declared_impact_cannot_lower_computed_level(checks):
    assert checks.classify_impact(_runtime(), _candidate(_change("a", "alive_status", "dead"))) == "major"
    assert checks.classify_impact(_runtime(), _candidate(impact_level="critical")) == "critical"
    assert checks.classify_impact(_runtime(), _candidate(summary="a"), milestone_refs=frozenset({"a"})) == "minor"


@pytest.mark.parametrize(("extra", "candidate", "code", "ref"), [
    ([_node("a", alive_status="dead")], _candidate(actor_id="a", new_intents=[_intent()]), "dead_owner_acts", "a"),
    ([_node("a", alive_status="dead")], _candidate(intent_id="intent"), "dead_owner_acts", "a"),
    ([_node("b", alive_status="dead")], _candidate({"events": [_event()]}), "dead_owner_acts", "b"),
    ([], _candidate(actor_id="a", new_intents=[_intent(scheduled_in={"amount": 119, "unit": "minute"})]),
     "unreachable_before_scheduled", "a"),
    ([], _candidate({"item_transfers": [{"item_ref": "resource", "from_ref": "b", "to_ref": "a"}]}),
     "resource_not_held", "resource"),
    ([_node("a", restraint={"state": "imprisoned"})], _candidate({"movements": [_movement()]}),
     "restrained_moves_without_release", "a"),
    ([], _candidate(_change("intent", "scheduled_at", NOW - 1)), "time_not_monotonic", "intent"),
    ([], _candidate({"events": [_event(at=NOW + 1)]}), "time_not_monotonic", "candidate"),
    ([], _candidate({"movements": [_movement(depart_at=799)]}), "time_not_monotonic", "a"),
])
def test_hard_rule_codes(checks, extra, candidate, code, ref):
    issues = checks.check_hard_rules(_runtime(*extra), LEXICON, candidate, now=NOW, window_start=800)
    assert (code, ref) in [(issue.code, issue.ref) for issue in issues]
    assert all(issue.message for issue in issues)


@pytest.mark.parametrize(("hidden_route", "location"), [(True, "l2"), (False, "l3")])
def test_unknown_or_unreachable_route_is_rejected(checks, hidden_route, location):
    issues = checks.check_hard_rules(_runtime(hidden_route=hidden_route), LEXICON,
        _candidate(actor_id="a", new_intents=[_intent(location_id=location)]), now=NOW)
    assert [issue.code for issue in issues] == ["unreachable_before_scheduled"]


@pytest.mark.parametrize("wait", [
    {"amount": 1, "unit": "cycle"}, {"amount": 8, "unit": "quarter"}, {"amount": 120, "unit": "minute"},
])
def test_schedule_uses_calendar_and_alias_minutes(checks, wait):
    assert checks.check_hard_rules(_runtime(), LEXICON, _candidate(actor_id="a", new_intents=[
        _intent(scheduled_in=wait),
    ]), now=NOW, calendar=CALENDAR) == []


@pytest.mark.parametrize("amount", [-10, 0])
def test_nonpositive_schedule_is_now(checks, amount):
    assert checks.check_hard_rules(_runtime(), LEXICON, _candidate(actor_id="a", new_intents=[
        _intent(location_id="l1", scheduled_in={"amount": amount, "unit": "minute"}),
    ]), now=NOW) == []


@pytest.mark.parametrize("release", [
    {"field": "restraint", "new_value": {"state": "free"}},
    {"field": "state.restraint.state", "new_value": "free"},
    {"field": "state", "new_value": {"restraint": {"state": "free"}}},
])
def test_release_anywhere_in_candidate_allows_movement(checks, release):
    candidate = _candidate({"movements": [_movement()], "entity_changes": [
        {"ref": "a", **release}, {"ref": "a", "field": "restraint.state", "new_value": "restrained"},
    ]})
    runtime = _runtime(_node("a", restraint={"state": "restrained"}))
    assert checks.check_hard_rules(runtime, LEXICON, candidate, now=NOW) == []
    assert runtime.world.get_state("a")["restraint"]["state"] == "restrained"


def test_holder_checks_follow_transfer_order_and_allow_unknown_holder(checks):
    candidate = _candidate({"item_transfers": [
        {"item_ref": "item", "from_ref": "a", "to_ref": "b"},
        {"item_ref": "item", "from_ref": "b", "to_ref": "c"},
        {"item_ref": "resource", "from_ref": "b", "to_ref": "c"},
    ]})
    assert checks.check_hard_rules(_runtime(_node("resource", "resource")), LEXICON, candidate, now=NOW) == []
    candidate.effects.item_transfers[1].from_ref = "a"
    assert [issue.code for issue in checks.check_hard_rules(_runtime(_node("resource", "resource")), LEXICON,
                                                          candidate, now=NOW)] == ["resource_not_held"]


@pytest.mark.parametrize(("protection", "effects"), [
    (_protection("must_exist", "s"), _change("s", "is_active", False)),
    (_protection("must_be_alive"), _change("a", "alive_status", "dead")),
    (_protection("must_hold_state", "item", field="holder_id", value="a"),
     {"item_transfers": [{"item_ref": "item", "from_ref": "a", "to_ref": "b"}]}),
    (_protection("must_hold_state", field="location_id", value="l1"), {"movements": [_movement()]}),
    (_protection("must_hold_state", field="restraint.state", value="free"),
     _change("a", "restraint", {"state": "restrained"})),
    *[(_protection("must_not_know", "b", observer_ref="b", fact_ref="fact:secret"),
       {"knowledge": [_knowledge(state=state)]}) for state in ("knows", "believes")],
])
def test_narrative_boundary_kinds(checks, protection, effects):
    result = checks.check_narrative_boundaries(_runtime(protections=[protection]), _candidate(effects), next_chapter=4)
    assert [(v.constraint_node_id, v.kind, v.ref) for v in result] == [
        ("protection", protection.payload["kind"], protection.payload["ref"]),
    ]


@pytest.mark.parametrize(("status", "until", "count"), [
    ("inactive", 10, 0), ("resolved", 10, 0), ("closed", 10, 0), ("active", 3, 0),
    ("active", 4, 1), ("active", 0, 1),
])
def test_inactive_or_expired_protection_is_ignored(checks, status, until, count):
    protection = _protection("must_be_alive", until_chapter=until).model_copy(update={"status": status})
    assert len(checks.check_narrative_boundaries(_runtime(protections=[protection]),
        _candidate(_change("a", "alive_status", "dead")), next_chapter=4)) == count


def test_boundary_allows_preserved_state_and_weak_knowledge(checks):
    protection = _protection("must_hold_state", field="restraint.state", value="free")
    assert checks.check_narrative_boundaries(_runtime(protections=[protection]),
        _candidate(_change("a", "restraint", {"state": "free", "by_ref": "b"})), next_chapter=4) == []
    protection = _protection("must_not_know", "b", observer_ref="b", fact_ref="fact:secret")
    assert checks.check_narrative_boundaries(_runtime(protections=[protection]),
        _candidate({"knowledge": [_knowledge(state="suspects")]}), next_chapter=4) == []


def test_budget_postpones_extra_major_candidates(checks):
    runtime = _runtime()
    candidates = [
        _candidate(candidate_id="z", actor_id="a", intent_id="intent", impact_level="major",
                   effects=_change("b", "alive_status", "dead"), intent_status_changes={"intent": "resolved"}),
        _candidate(candidate_id="a", actor_id="b", impact_level="major"),
        _candidate(candidate_id="minor", impact_level="minor"),
    ]
    result = checks.apply_budgets(candidates, impacts={c.candidate_id: c.impact_level for c in candidates},
                                 max_major_events=1, runtime=runtime, now=NOW)
    assert [c.candidate_id for c in result.kept] == ["z", "minor"]
    assert [c.candidate_id for c in result.postponed] == ["a"]
    result = checks.apply_budgets(candidates, impacts={"z": "critical", "a": "major", "minor": "minor"},
                                 max_major_events=0, runtime=runtime, now=NOW)
    postponed = next(c for c in result.postponed if c.candidate_id == "z")
    assert postponed.intent_status_changes == {"intent": "planned"}
    assert postponed.effects == WorldEffects(entity_changes=[
        {"ref": "intent", "field": "scheduled_at", "new_value": NOW},
    ])
    assert candidates[0].intent_status_changes == {"intent": "resolved"}
    assert runtime.world.get_state("intent")["scheduled_at"] == 900


def test_budget_priority_is_clamped_and_id_breaks_ties(checks):
    candidates = [
        _candidate(candidate_id="z", agenda_changes=[{"agenda_ref": "agenda", "priority": 900}]),
        _candidate(candidate_id="a", agenda_changes=[{"agenda_ref": "agenda", "priority": 5}]),
        _candidate(candidate_id="b", agenda_changes=[{"agenda_ref": "agenda", "priority": -100}]),
    ]
    result = checks.apply_budgets(candidates, impacts={c.candidate_id: "major" for c in candidates}, max_major_events=1)
    assert [c.candidate_id for c in result.kept] == ["a"]
    assert [c.candidate_id for c in result.postponed] == ["z", "b"]


def test_budget_reads_existing_agenda_priority(checks):
    runtime = _runtime(
        _node("low", "agenda", priority=1),
        _node("i-low", "intent", profile={"owner_ref": "a", "agenda_ref": "low"}),
    )
    candidates = [_candidate(candidate_id="a", intent_id="i-low"), _candidate(candidate_id="z", intent_id="intent")]
    result = checks.apply_budgets(candidates, impacts={"a": "major", "z": "major"}, max_major_events=1,
                                 runtime=runtime, now=NOW)
    assert [c.candidate_id for c in result.kept] == ["z"]


def test_milestone_ignores_structural_labels(checks):
    assert checks.classify_impact(_runtime(), _candidate(), milestone_refs=frozenset({"minor", "effects"})) == "minor"


def test_public_existing_event_referencing_hidden_fact_is_major(checks):
    runtime = _runtime(_node("event", "event", visibility_level="public", result_refs=[]))
    assert checks.classify_impact(runtime, _candidate(_change("event", "result_refs", ["fact:hidden"]))) == "major"


def test_effect_timestamp_patch_must_fit_window(checks):
    runtime = _runtime(_node("event", "event", actual_story_time=900))
    issues = checks.check_hard_rules(runtime, LEXICON, _candidate(_change("event", "actual_story_time", 799)),
                                    now=NOW, window_start=800)
    assert [(issue.code, issue.ref) for issue in issues] == [("time_not_monotonic", "event")]


def test_postponed_intent_builds_reversible_bookkeeping(checks):
    from forwin.world_engine.delta_builder import _TickBuilder
    from forwin.world_engine.types import TickContext

    runtime = _runtime()
    candidate = _candidate(intent_id="intent", intent_status_changes={"intent": "resolved"},
                           effects=_change("a", "alive_status", "dead"))
    result = checks.apply_budgets([candidate], impacts={"candidate": "major"}, max_major_events=0,
                                 runtime=runtime, now=NOW)
    built = _TickBuilder(context=TickContext(project_id="p", chapter_number=3, now=NOW, window_start=800, seed=1),
                         tick_id="tick", runtime=runtime, calendar=CALENDAR, lexicon=LEXICON).build(result.postponed, [])
    assert built.issues == []
    assert [(p.node_id, p.field_path, p.old_value, p.new_value) for p in built.delta.node_patches] == [
        ("intent", "state.status", "due", "planned"), ("intent", "state.scheduled_at", 900, 1000),
    ]


def test_postponement_preserves_candidates_without_existing_intents(checks):
    candidate = _candidate(actor_id="a", new_intents=[_intent()], impact_level="major")
    result = checks.apply_budgets([candidate], impacts={"candidate": "major"}, max_major_events=0,
                                 runtime=_runtime(), now=NOW)
    assert result.kept == []
    assert result.postponed == [candidate]
