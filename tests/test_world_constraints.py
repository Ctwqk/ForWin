from __future__ import annotations

from itertools import permutations

import pytest

from forwin.book_state.cognition import CognitionView
from forwin.book_state.lexicon import BookLexicon, TimeUnitAlias
from forwin.book_state.map_graph import MapGraph
from forwin.book_state.narrative import NarrativeControlGraph
from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
from forwin.book_state.story_clock import CalendarUnit, StoryCalendar
from forwin.protocol.book_state import CognitionOverlay, FactNode, MapEdge, MapNode, NarrativeNode, WorldNode
from forwin.world_engine import constraints
from forwin.world_engine.constraints import ENDED_STATUSES
from forwin.world_engine.intents import apply_intent_interactions, onstage_bookkeeping
from forwin.world_engine.types import TickCandidate, WorldEffects


NOW = 1000
LEXICON = BookLexicon(time_unit_aliases=(TimeUnitAlias("minute", 1), TimeUnitAlias("quarter", 15)))
CALENDAR = StoryCalendar("", 0, (CalendarUnit("cycle", 120),))


@pytest.fixture
def checks():
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
            FactNode(id="protected", project_id="p", proposition="p", sensitivity_level="must_not_reveal"),
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


@pytest.mark.parametrize("effects", [
    _change("a", "location_id", "l2"),
    _change("a", "alive_status", "dead"),
    _change("a", "restraint.state", "restrained"),
    _change("a", "state", {"alive_status": "dead"}),
    {"movements": [_movement()]},
    {"item_transfers": [{"item_ref": "item", "from_ref": "a", "to_ref": "b"}]},
    _change("item", "state", {"holder_id": "b"}),
    _change("resource", "holder_id", "b"),
])
def test_tick_never_moves_or_harms_the_chapters_onstage_entities(checks, effects):
    issues = checks.check_hard_rules(
        _runtime(), LEXICON, _candidate(effects), now=NOW, calendar=CALENDAR, window_start=800,
        onstage_refs=frozenset({"a"}),
    )
    assert "touches_onstage_entity" in [issue.code for issue in issues]


@pytest.mark.parametrize("effects", [
    {"knowledge": [_knowledge(observer_ref="a")]},
    {"relation_changes": [{"source_ref": "a", "target_ref": "b", "edge_type": "ally_of", "change": "create"}]},
    _change("b", "location_id", "l2"),
    _change("a", "location_id", "l1"),
])
def test_onstage_exclusion_allows_knowledge_relations_and_unchanged_state(checks, effects):
    assert checks.check_hard_rules(
        _runtime(), LEXICON, _candidate(effects), now=NOW, calendar=CALENDAR, window_start=800,
        onstage_refs=frozenset({"a"}),
    ) == []


ONSTAGE_PHYSICAL_CHANGES = [
    pytest.param("a", "item_ids", [], "a", id="inventory"),
    pytest.param("a", "equipped_item_ids", [], "a", id="equipment"),
    pytest.param("item", "status", "destroyed", "a", id="held-item"),
    pytest.param("resource", "quantity", 0, "a", id="held-resource"),
    pytest.param("f", "headquarters_location_id", "l2", "f", id="headquarters"),
    pytest.param("activity", "current_location_id", "l2", "a", id="activity"),
]


def _onstage_physical_runtime():
    return _runtime(
        _node("a", alive_status="alive", current_activity_id="activity",
              item_ids=["item"], equipped_item_ids=["item"]),
        _node("item", "item", holder_id="a", status="intact"),
        _node("resource", "resource", holder_id="a", quantity=5),
        _node("f", "faction", headquarters_location_id="l1"),
        _node("activity", "activity", current_location_id="l1"),
    )


@pytest.mark.parametrize("ref, field, value, onstage", ONSTAGE_PHYSICAL_CHANGES)
def test_onstage_physical_changes_use_supported_builder_fields(checks, ref, field, value, onstage):
    from copy import deepcopy

    runtime = _onstage_physical_runtime()
    before = deepcopy(runtime.world.states_by_node_id)
    candidate = _candidate(_change(ref, field, value))
    assert _build_alone(runtime, candidate).issues == []
    issues = checks.check_hard_rules(
        runtime, LEXICON, candidate, now=NOW, calendar=CALENDAR, window_start=800,
        onstage_refs=frozenset({onstage}),
    )
    assert [(issue.code, issue.ref) for issue in issues] == [("touches_onstage_entity", onstage)]
    assert runtime.world.states_by_node_id == before


@pytest.mark.parametrize("depart, expected", [(949, ["movement_overlap"]), (950, [])])
def test_movement_overlap_uses_last_arrival_and_allows_adjacent_moves(checks, depart, expected):
    issues = checks.check_hard_rules(
        _runtime(), LEXICON, _candidate({"movements": [_movement(depart_at=depart, arrive_at=1000)]}),
        now=NOW, calendar=CALENDAR, window_start=800, last_arrival_by_mover={"a": 950},
    )
    assert [issue.code for issue in issues] == expected


def test_movement_overlap_within_one_candidate(checks):
    issues = checks.check_hard_rules(
        _runtime(), LEXICON, _candidate({"movements": [
            _movement(arrive_at=900), _movement(depart_at=899),
        ]}), now=NOW, calendar=CALENDAR, window_start=800,
    )
    assert [issue.code for issue in issues] == ["movement_overlap"]


def test_staging_excluded_intent_is_bookkeeping_even_for_milestone_ref(checks):
    candidate = _candidate(_change("intent", "requires_onstage", True), origin="onstage", intent_id="intent",
                           intent_status_changes={"intent": "due"})
    assert checks.classify_impact(_runtime(), candidate, milestone_refs=frozenset({"intent"})) == "minor"


@pytest.mark.parametrize(("effects", "expected"), [
    (_change("a", "alive_status", "dead"), "major"),
    (_change("b", "state.alive_status", "dead"), "major"),
    (_change("c", "alive_status", "dead"), "moderate"),
    *[(_change(ref, "status", status), "major") for ref in ("f", "o", "g", "s") for status in sorted(ENDED_STATUSES)],
    *[(_change(ref, "state", {"status": "destroyed"}), "major") for ref in ("f", "s")],
    *[(_change(ref, "leader_id", "b"), "major") for ref in ("f", "g")],
    (_change("s", "controller_faction_id", "f"), "major"),
    *[({"knowledge": [_knowledge(fact_ref=f"fact:{ref}", state=state)]}, "major")
      for ref in ("hidden", "secret", "protected") for state in ("knows", "believes")],
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
    *[(_change(ref, "status", "besieged"), "minor") for ref in ("f", "s")],
    # Fields these node types do not have, which the builder refuses: no rule reads them.
    *[(_change(ref, "is_active", False), "minor") for ref in ("f", "o", "g", "s")],
    *[(_change(ref, "controlled_by_id", "b"), "minor") for ref in ("f", "o", "g")],
    (_change("o", "leader_id", "b"), "minor"),
    (_change("fact:secret", "sensitivity", "public"), "minor"),
    # Ordinary news, weak knowledge and a secret's creators knowing it expose nothing.
    ({"knowledge": [_knowledge(fact_ref="fact:public")]}, "minor"),
    ({"knowledge": [_knowledge(state="suspects")]}, "minor"),
    ({"new_facts": [{"key": "k", "proposition": "p", "truth_value": True, "sensitivity": "secret",
                     "known_by": ["a"]}]}, "minor"),
    ({"events": [_event(visibility="secret")]}, "minor"),
    ({}, "minor"),
])
def test_impact_rows(checks, effects, expected):
    assert checks.classify_impact(_runtime(), _candidate(effects)) == expected


@pytest.mark.parametrize("candidate", [
    _candidate(_change("a", "location_id", "l2")),
    _candidate({"knowledge": [_knowledge(fact_ref="fact:secret")]}),
    _candidate({"movements": [_movement(entity_ref="a")]}),
    _candidate({"item_transfers": [{"item_ref": "item", "from_ref": "b", "to_ref": "a"}]}),
    *[_candidate({"relation_changes": [{"source_ref": "b", "target_ref": "a", "edge_type": "ally_of",
                                         "change": change}]}) for change in ("create", "set", "end")],
])
def test_milestone_references_are_critical(checks, candidate):
    assert checks.classify_impact(_runtime(), candidate, milestone_refs=frozenset({"a", "secret"})) == "critical"


@pytest.mark.parametrize("candidate", [
    _candidate(actor_id="a", intent_id="a", next_turn_at={"a": 1100}),
    _candidate(actor_id="a", slip_updates={"intent": 1}, intent_status_changes={"intent": "due"}),
    _candidate(new_intents=[_intent(target_refs=["a"])]),
    _candidate({"events": [_event(participant_refs=["a"])]}),
])
def test_milestone_authorship_and_bookkeeping_do_not_raise_impact(checks, candidate):
    assert checks.classify_impact(_runtime(), candidate, milestone_refs=frozenset({"a"})) == "minor"


@pytest.mark.parametrize("effects", [
    {"movements": [_movement(entity_ref="item")]},
    {"item_transfers": [{"item_ref": "item", "from_ref": "a", "to_ref": "b"}]},
])
def test_milestone_item_state_changes_are_critical(checks, effects):
    assert checks.classify_impact(_runtime(), _candidate(effects), milestone_refs=frozenset({"item"})) == "critical"


def test_declared_impact_cannot_lower_computed_level(checks):
    assert checks.classify_impact(_runtime(), _candidate(_change("a", "alive_status", "dead"))) == "major"
    assert checks.classify_impact(_runtime(), _candidate(impact_level="critical")) == "critical"
    assert checks.classify_impact(_runtime(), _candidate(summary="a"), milestone_refs=frozenset({"a"})) == "minor"


@pytest.mark.parametrize(("extra", "candidate", "code", "ref"), [
    ([_node("a", alive_status="dead")], _candidate(actor_id="a", new_intents=[_intent()]), "dead_owner_acts", "a"),
    ([_node("a", alive_status="dead")],
     _candidate(intent_id="intent", intent_status_changes={"intent": "resolved"}), "dead_owner_acts", "a"),
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
    issues = checks.check_hard_rules(_runtime(*extra), LEXICON, candidate,
                                    now=NOW, calendar=CALENDAR, window_start=800)
    assert (code, ref) in [(issue.code, issue.ref) for issue in issues]
    assert all(issue.message for issue in issues)


@pytest.mark.parametrize(("hidden_route", "location"), [(True, "l2"), (False, "l3")])
def test_unknown_or_unreachable_route_is_rejected(checks, hidden_route, location):
    issues = checks.check_hard_rules(_runtime(hidden_route=hidden_route), LEXICON,
        _candidate(actor_id="a", new_intents=[_intent(location_id=location)]),
        now=NOW, calendar=CALENDAR, window_start=800)
    assert [issue.code for issue in issues] == ["unreachable_before_scheduled"]


@pytest.mark.parametrize("wait", [
    {"amount": 1, "unit": "cycle"}, {"amount": 8, "unit": "quarter"}, {"amount": 120, "unit": "minute"},
])
def test_schedule_uses_calendar_and_alias_minutes(checks, wait):
    assert checks.check_hard_rules(_runtime(), LEXICON, _candidate(actor_id="a", new_intents=[
        _intent(scheduled_in=wait),
    ]), now=NOW, calendar=CALENDAR, window_start=800) == []


@pytest.mark.parametrize("amount", [-10, 0])
def test_nonpositive_schedule_is_now(checks, amount):
    assert checks.check_hard_rules(_runtime(), LEXICON, _candidate(actor_id="a", new_intents=[
        _intent(location_id="l1", scheduled_in={"amount": amount, "unit": "minute"}),
    ]), now=NOW, calendar=CALENDAR, window_start=800) == []


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
    assert checks.check_hard_rules(runtime, LEXICON, candidate, now=NOW, calendar=CALENDAR, window_start=800) == []
    assert runtime.world.get_state("a")["restraint"]["state"] == "restrained"


def test_holder_checks_follow_transfer_order_and_allow_unknown_holder(checks):
    candidate = _candidate({"item_transfers": [
        {"item_ref": "item", "from_ref": "a", "to_ref": "b"},
        {"item_ref": "item", "from_ref": "b", "to_ref": "c"},
        {"item_ref": "resource", "from_ref": "b", "to_ref": "c"},
    ]})
    assert checks.check_hard_rules(_runtime(_node("resource", "resource")), LEXICON, candidate,
                                   now=NOW, calendar=CALENDAR, window_start=800) == []
    candidate.effects.item_transfers[1].from_ref = "a"
    assert [issue.code for issue in checks.check_hard_rules(_runtime(_node("resource", "resource")), LEXICON,
        candidate, now=NOW, calendar=CALENDAR, window_start=800)] == ["resource_not_held"]


@pytest.mark.parametrize(("protection", "effects"), [
    *[(_protection("must_exist", ref), _change(ref, "status", status))
      for ref in ("f", "s") for status in sorted(ENDED_STATUSES)],
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
    ("retired", 10, 0), ("deleted", 10, 0), ("revoked", 10, 0), ("archived", 10, 0), ("unknown", 10, 0),
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
    assert result.postponed == []
    assert result.deferred_without_intent == [candidates[1]]
    result = checks.apply_budgets(candidates, impacts={"z": "critical", "a": "major", "minor": "minor"},
                                 max_major_events=0, runtime=runtime, now=NOW)
    postponed = next(c for c in result.postponed if c.candidate_id == "z")
    assert postponed.impact_level == "minor"
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
    result = checks.apply_budgets(candidates, impacts={c.candidate_id: "major" for c in candidates}, max_major_events=1,
                                 runtime=_runtime(), now=NOW)
    assert [c.candidate_id for c in result.kept] == ["a"]
    assert result.postponed == []
    assert [c.candidate_id for c in result.deferred_without_intent] == ["z", "b"]


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
                                    now=NOW, calendar=CALENDAR, window_start=800)
    assert [(issue.code, issue.ref) for issue in issues] == [("time_not_monotonic", "event")]


def test_postponed_intent_builds_reversible_bookkeeping(checks):
    from forwin.world_engine.delta_builder import _TickBuilder
    from forwin.world_engine.types import TickContext

    runtime = _runtime()
    candidate = _candidate(intent_id="intent", intent_status_changes={"intent": "resolved"},
                           effects=_change("a", "alive_status", "dead"), impact_level="major")
    result = checks.apply_budgets([candidate], impacts={"candidate": "major"}, max_major_events=0,
                                 runtime=runtime, now=NOW)
    built = _TickBuilder(context=TickContext(project_id="p", chapter_number=3, now=NOW, window_start=800, seed=1),
                         tick_id="tick", runtime=runtime, calendar=CALENDAR, lexicon=LEXICON).build(result.postponed, [])
    assert built.issues == []
    assert built.delta.metadata["candidate_impacts"] == {"candidate": "minor"}
    assert [(p.node_id, p.field_path, p.old_value, p.new_value) for p in built.delta.node_patches] == [
        ("intent", "state.status", "due", "planned"), ("intent", "state.scheduled_at", 900, 1000),
    ]


def test_deferral_preserves_candidates_without_existing_intents(checks):
    candidate = _candidate(actor_id="a", new_intents=[_intent()], impact_level="major")
    result = checks.apply_budgets([candidate], impacts={"candidate": "major"}, max_major_events=0,
                                 runtime=_runtime(), now=NOW)
    assert result.kept == []
    assert result.postponed == []
    assert result.deferred_without_intent == [candidate]
    assert result.deferred_without_intent[0] is candidate


@pytest.mark.parametrize("missing", ["calendar", "window_start", "both"])
def test_hard_rules_require_calendar_and_window(checks, missing):
    context = {"now": NOW, "calendar": CALENDAR, "window_start": 800}
    for key in ("calendar", "window_start") if missing == "both" else (missing,):
        context.pop(key)
    with pytest.raises(TypeError):
        checks.check_hard_rules(_runtime(), LEXICON, _candidate(), **context)


def test_event_before_explicit_window_is_rejected(checks):
    issues = checks.check_hard_rules(_runtime(), LEXICON, _candidate({"events": [_event(at=5)]}),
                                    now=NOW, calendar=CALENDAR, window_start=800)
    assert [(issue.code, issue.ref) for issue in issues] == [("time_not_monotonic", "candidate")]


@pytest.mark.parametrize("missing", ["runtime", "now", "both"])
def test_budgets_require_runtime_and_now(checks, missing):
    context = {"runtime": _runtime(), "now": NOW}
    for key in ("runtime", "now") if missing == "both" else (missing,):
        context.pop(key)
    with pytest.raises(TypeError):
        checks.apply_budgets([], impacts={}, max_major_events=0, **context)


def test_budget_requires_computed_impact_for_every_candidate(checks):
    with pytest.raises(ValueError, match="candidate"):
        checks.apply_budgets([_candidate()], impacts={}, max_major_events=0, runtime=_runtime(), now=NOW)


@pytest.mark.parametrize("intent_id", ["missing", "item"])
def test_budget_does_not_rewrite_missing_or_nonintent_refs(checks, intent_id):
    candidate = _candidate(intent_id=intent_id, intent_status_changes={intent_id: "resolved"},
                           effects=_change("a", "alive_status", "dead"))
    result = checks.apply_budgets([candidate], impacts={"candidate": "major"}, max_major_events=0,
                                 runtime=_runtime(), now=NOW)
    assert result.kept == result.postponed == []
    assert result.deferred_without_intent == [candidate]


def test_postponed_candidate_contains_only_existing_intent_bookkeeping(checks):
    candidate = _candidate(actor_id="a", intent_id="intent", impact_level="major",
        effects=_change("b", "alive_status", "dead"), new_intents=[_intent()],
        agenda_changes=[{"agenda_ref": "agenda", "priority": 5}],
        intent_status_changes={"intent": "resolved", "missing": "resolved"},
        slip_updates={"intent": 1}, next_turn_at={"a": 1200})
    original = candidate.model_copy(deep=True)
    result = checks.apply_budgets([candidate], impacts={"candidate": "major"}, max_major_events=0,
                                 runtime=_runtime(), now=NOW)
    assert result.kept == result.deferred_without_intent == []
    assert len(result.postponed) == 1
    postponed = result.postponed[0]
    assert postponed.impact_level == "minor"
    assert postponed.intent_status_changes == {"intent": "planned"}
    assert postponed.effects == WorldEffects(entity_changes=[
        {"ref": "intent", "field": "scheduled_at", "new_value": NOW},
    ])
    assert postponed.new_intents == postponed.agenda_changes == []
    assert postponed.slip_updates == postponed.next_turn_at == {}
    assert candidate == original


def test_budget_tier_overrules_priority_and_input_order(checks):
    runtime = _runtime(_node("other", "intent", profile={"owner_ref": "b", "agenda_ref": "agenda"}))
    candidates = [
        _candidate(candidate_id="z", actor_id="a", intent_id="intent",
                   agenda_changes=[{"agenda_ref": "agenda", "priority": 1}]),
        _candidate(candidate_id="a", actor_id="b", intent_id="other",
                   agenda_changes=[{"agenda_ref": "agenda", "priority": 5}]),
        _candidate(candidate_id="b", actor_id="c",
                   agenda_changes=[{"agenda_ref": "agenda", "priority": 5}]),
    ]
    for shuffled in permutations(candidates):
        result = checks.apply_budgets(shuffled, impacts={c.candidate_id: "major" for c in candidates},
                                     max_major_events=1, runtime=runtime, now=NOW)
        assert result.kept == [candidates[0]]
        assert [candidate.candidate_id for candidate in result.postponed] == ["a"]
        assert result.postponed[0].intent_status_changes == {"other": "planned"}
        assert result.deferred_without_intent == [candidates[2]]


@pytest.mark.parametrize("candidate", [
    _candidate(actor_id="a"),
    _candidate(intent_id="intent"),
    *[_candidate(actor_id="a", intent_id="intent", intent_status_changes={"intent": status})
      for status in ("disrupted", "vetoed", "held", "planned")],
    _candidate(actor_id="a", intent_id="intent", slip_updates={"intent": 1}),
    _candidate(actor_id="a", next_turn_at={"a": 1200}),
])
def test_dead_owner_bookkeeping_is_not_action(checks, candidate):
    assert checks.check_hard_rules(_runtime(_node("a", alive_status="dead")), LEXICON, candidate,
                                   now=NOW, calendar=CALENDAR, window_start=800) == []


def test_dead_owner_chapter_disruption_is_not_action(checks):
    runtime = _runtime(_node("a", alive_status="dead"))
    candidates = apply_intent_interactions(runtime, interactions=[{"intent_id": "intent", "effect": "disrupted"}])
    assert len(candidates) == 1
    assert candidates[0].intent_status_changes == {"intent": "disrupted"}
    assert checks.check_hard_rules(runtime, LEXICON, candidates[0],
                                   now=NOW, calendar=CALENDAR, window_start=800) == []


def test_dead_owner_onstage_slip_is_not_action(checks):
    runtime = _runtime(_node("a", alive_status="dead"))
    result = onstage_bookkeeping(runtime, onstage_due=[], already_due=[runtime.world.get_node("intent")],
        interactions=[], now=NOW, max_slips=2, chapter_number=3,
        bound_intent_refs=frozenset({"intent"}), band_ended=False, band_bound_intent_refs=frozenset())
    assert len(result.candidates) == 1
    assert result.candidates[0].slip_updates == {"intent": 1}
    assert checks.check_hard_rules(runtime, LEXICON, result.candidates[0],
                                   now=NOW, calendar=CALENDAR, window_start=800) == []


@pytest.mark.parametrize("candidate", [
    _candidate({"movements": [_movement()]}),
    _candidate(_change("a", "location_id", "l2")),
    _candidate({"events": [_event(participant_refs=["a"])]}),
    _candidate({"knowledge": [_knowledge(observer_ref="a")]}),
    _candidate({"knowledge": [_knowledge(channel="told", source_ref="a")]}),
    _candidate(intent_status_changes={"intent": "resolved"}),
])
def test_dead_actual_actor_is_rejected(checks, candidate):
    issues = checks.check_hard_rules(_runtime(_node("a", alive_status="dead")), LEXICON, candidate,
                                    now=NOW, calendar=CALENDAR, window_start=800)
    assert [(issue.code, issue.ref) for issue in issues] == [("dead_owner_acts", "a")]


@pytest.mark.parametrize("field", ["state", ""])
@pytest.mark.parametrize("value", ["x", None, []])
@pytest.mark.parametrize(("ref", "kind"), [("a", "character"), ("event", "event")])
def test_nondict_state_is_skipped_by_classification_and_reported_by_checks(checks, field, value, ref, kind):
    runtime = _runtime(_node(ref, kind, location_id="l1"))
    candidate = _candidate(_change(ref, field, value))
    assert checks.classify_impact(runtime, candidate) == "minor"
    issues = checks.check_hard_rules(runtime, LEXICON, candidate, now=NOW, calendar=CALENDAR, window_start=800)
    assert len(issues) == 1
    assert isinstance(issues[0], checks.ConstraintIssue)
    assert issues[0].ref == ref
    assert issues[0].message
    assert runtime.world.get_state(ref)["location_id"] == "l1"


@pytest.mark.parametrize(("minutes", "expected"), [(18, []), (17, ["unreachable_before_scheduled"])])
def test_fractional_travel_arrival_boundary_uses_gate_epsilon(checks, minutes, expected):
    runtime = _runtime()
    runtime.map = MapGraph(
        nodes=[MapNode(id=ref, project_id="p", node_type="site") for ref in ("l1", "l2", "l3")],
        edges=[MapEdge(id=ref, project_id="p", from_node_id=start, to_node_id=end,
                       edge_type="road", travel_time=hours)
               for ref, start, end, hours in [("first", "l1", "l2", 0.1), ("second", "l2", "l3", 0.2)]],
    )
    candidate = _candidate(actor_id="a", new_intents=[
        _intent(location_id="l3", scheduled_in={"amount": minutes, "unit": "minute"}),
    ])
    issues = checks.check_hard_rules(runtime, LEXICON, candidate, now=NOW, calendar=CALENDAR, window_start=800)
    assert [issue.code for issue in issues] == expected


@pytest.mark.parametrize("until", [None, "5", [], {}])
def test_noninteger_protection_expiry_is_open(checks, until):
    protection = _protection("must_be_alive", until_chapter=until)
    violations = checks.check_narrative_boundaries(_runtime(protections=[protection]),
        _candidate(_change("a", "alive_status", "dead")), next_chapter=4)
    assert [(v.kind, v.ref) for v in violations] == [("must_be_alive", "a")]


@pytest.mark.parametrize("fact_ref", ["secret", "fact:secret"])
def test_protection_fact_reference_is_normalized(checks, fact_ref):
    protection = _protection("must_not_know", "b", observer_ref="b", fact_ref=fact_ref)
    violations = checks.check_narrative_boundaries(_runtime(protections=[protection]),
        _candidate({"knowledge": [_knowledge()]}), next_chapter=4)
    assert [(v.kind, v.ref) for v in violations] == [("must_not_know", "b")]


@pytest.mark.parametrize("payload", [
    {}, {"ref": "a", "kind": "must_hold_state"},
    {"ref": "a", "kind": "must_hold_state", "field": [], "value": "alive"},
    {"ref": "b", "kind": "must_not_know", "observer_ref": [], "fact_ref": "secret"},
    {"ref": "b", "kind": "must_not_know", "observer_ref": "b", "fact_ref": {}},
])
def test_malformed_protection_payload_is_ignored(checks, payload):
    protection = _protection("must_hold_state").model_copy(update={"payload": payload})
    candidate = _candidate({**_change("a", "state", {"alive_status": "dead"}), "knowledge": [_knowledge()]})
    assert checks.check_narrative_boundaries(_runtime(protections=[protection]), candidate, next_chapter=4) == []


@pytest.mark.parametrize(("protection", "node", "effects"), [
    (_protection("must_exist", "s"), _node("s", "site_state", status="destroyed"), _change("s", "status", "inactive")),
    (_protection("must_be_alive"), _node("a", alive_status="dead"), _change("a", "alive_status", "dead")),
])
def test_already_inactive_or_dead_ref_does_not_violate_boundary(checks, protection, node, effects):
    assert checks.check_narrative_boundaries(_runtime(node, protections=[protection]),
        _candidate(effects), next_chapter=4) == []


@pytest.mark.parametrize("field", [["unused"], {"unused": True}, 5])
def test_protection_ignores_field_for_kinds_that_do_not_use_it(checks, field):
    protection = _protection("must_be_alive", field=field)
    violations = checks.check_narrative_boundaries(_runtime(protections=[protection]),
        _candidate(_change("a", "alive_status", "dead")), next_chapter=4)
    assert [(v.kind, v.ref) for v in violations] == [("must_be_alive", "a")]


def test_invalid_state_does_not_hide_a_later_death_change(checks):
    candidate = _candidate({"entity_changes": [
        {"ref": "a", "field": "state", "new_value": "x"},
        {"ref": "a", "field": "alive_status", "new_value": "dead"},
    ]})
    assert checks.classify_impact(_runtime(), candidate) == "major"
    issues = checks.check_hard_rules(_runtime(), LEXICON, candidate, now=NOW, calendar=CALENDAR, window_start=800)
    assert [(issue.code, issue.ref) for issue in issues] == [("invalid_state", "a")]


# Ruling P31: every deterministic rule reads a field the builder can write.


def _build_alone(runtime, candidate):
    from forwin.world_engine.delta_builder import _TickBuilder
    from forwin.world_engine.types import TickContext

    return _TickBuilder(
        context=TickContext(project_id="p", chapter_number=3, now=NOW, window_start=800, seed=1),
        tick_id="tick", runtime=runtime, calendar=CALENDAR, lexicon=LEXICON,
    ).build([candidate], [])


_NEW_SECRET = {"key": "k", "proposition": "p", "truth_value": True, "sensitivity": "secret", "known_by": ["a"]}


@pytest.mark.parametrize(("effects", "expected"), [
    (_change("a", "alive_status", "dead"), "major"),
    *[(_change(ref, "status", "destroyed"), "major") for ref in ("f", "o", "g", "s")],
    *[(_change(ref, "leader_id", "b"), "major") for ref in ("f", "g")],
    (_change("s", "controller_faction_id", "f"), "major"),
    ({"knowledge": [_knowledge()]}, "major"),
    ({"new_facts": [_NEW_SECRET], "knowledge": [_knowledge(fact_ref="fact:new:k")]}, "major"),
    (_change("event", "visibility_level", "public"), "major"),
    ({"relation_changes": [{"source_ref": "a", "target_ref": "b", "edge_type": "ally_of", "change": "create"}]},
     "moderate"),
    ({"item_transfers": [{"item_ref": "item", "from_ref": "a", "to_ref": "b"}]}, "moderate"),
    (_change("resource", "holder_id", "b"), "moderate"),
    (_change("a", "restraint", {"state": "restrained"}), "moderate"),
    (_change("a", "health", 0.5), "moderate"),
])
def test_every_impact_rule_builds_and_classifies(checks, effects, expected):
    runtime = _runtime(_node("event", "event", visibility_level="secret", result_refs=["fact:hidden"]))
    candidate = _candidate(effects)
    assert [issue.code for issue in _build_alone(runtime, candidate).issues if issue.severity == "error"] == []
    assert checks.classify_impact(runtime, candidate) == expected


def test_changes_the_builder_refuses_are_not_major(checks):
    """The final review's probe table: the old rules read fields no candidate can carry."""
    runtime = _runtime()
    refused = {
        "faction_is_active": (_change("f", "is_active", False), "unknown_field"),
        "faction_controlled_by": (_change("f", "controlled_by_id", "b"), "unknown_field"),
        "fact_made_public": (_change("fact:secret", "sensitivity", "public"), "unresolved_ref"),
    }
    for effects, code in refused.values():
        candidate = _candidate(effects)
        assert checks.classify_impact(runtime, candidate) == "minor"
        assert [issue.code for issue in _build_alone(runtime, candidate).issues] == [code]
    collapse = _candidate({"entity_changes": [
        {"ref": "f", "field": "leader_id", "new_value": "b"}, {"ref": "f", "field": "status", "new_value": "destroyed"},
    ]})
    assert checks.classify_impact(runtime, collapse) == "major"
    assert _build_alone(runtime, collapse).issues == []


def test_a_secret_an_observer_already_holds_is_not_exposed_again(checks):
    runtime = _runtime()
    runtime.cognition_by_observer[("character", "b")] = CognitionView(CognitionOverlay(
        id="view-b", project_id="p", observer_type="character", observer_id="b",
        knowledge_by_ref={"fact:secret": "knows", "fact:hidden": "suspects"},
    ))
    assert checks.classify_impact(runtime, _candidate({"knowledge": [_knowledge()]})) == "minor"
    # A suspicion is not knowledge: being sure of it now exposes the secret.
    assert checks.classify_impact(runtime, _candidate({"knowledge": [_knowledge(fact_ref="fact:hidden")]})) == "major"
    # A new secret known only to the actors that made it exposes nothing.
    assert checks.classify_impact(runtime, _candidate({
        "new_facts": [_NEW_SECRET], "knowledge": [_knowledge(observer_ref="a", fact_ref="fact:new:k")],
    })) == "minor"


def test_must_exist_ignores_a_status_that_does_not_end_the_ref(checks):
    protection = _protection("must_exist", "f")
    assert checks.check_narrative_boundaries(_runtime(protections=[protection]),
        _candidate(_change("f", "status", "besieged")), next_chapter=4) == []
