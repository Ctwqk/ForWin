from __future__ import annotations

import pytest

from forwin.protocol.book_state import EdgePatch, GraphDelta, MapNode, NodePatch, WorldEdge
from tests.test_world_constraints import _candidate, _intent, _node, _runtime


def rule(kind, *, hard=True, **params):
    return _node("r", "rule", profile={"hardness": "hard" if hard else "soft",
                 "checkable": {"kind": kind, **params}}, status="active")


@pytest.mark.parametrize("kind,params", [
    ("actor_cannot_enter", {"location_ref": "l2", "except_refs": ["a"], "except_member_of": ["g"]}),
    ("requires_travel_mode", {"edge_types": ["road"], "mode_ids": ["m"]}),
    ("exclusive_relation", {"edge_type": "serves", "max_per_source": 1}),
    ("irreversible_state", {"node_types": ["character"], "field": "alive_status", "value": "dead"}),
    ("blocked_channel", {"fact_ref": "fact:secret", "source_refs": ["a"], "channels": ["told"]}),
])
def test_parse_each_kind_valid_and_invalid(kind, params):
    from forwin.book_state.rule_predicates import RuleRefScope, parse_checkable
    scope = RuleRefScope(frozenset({"l2"}), frozenset({"a", "g"}), frozenset({"fact:secret"}), frozenset({"m"}))
    predicate, issues = parse_checkable({"kind": kind, **params}, scope=scope)
    assert predicate is not None and not issues
    for invalid in ({"kind": kind}, {"kind": kind, **params, "prose": "x"},
                    {"kind": kind, **{key: {} for key in params}}):
        parsed, issues = parse_checkable(invalid, scope=scope)
        assert parsed is None and issues
    if kind in {"actor_cannot_enter", "requires_travel_mode", "blocked_channel"}:
        assert parse_checkable({"kind": kind, **params}, scope=RuleRefScope(*[frozenset()] * 4))[1]


def test_evaluates_folded_state_relations_and_knowledge():
    from forwin.book_state.rule_predicates import active_rule_predicates, changes_from_delta, evaluate_rules
    runtime = _runtime(rule("irreversible_state", node_types=["character"], field="alive_status", value="dead"))
    delta = GraphDelta(id="d", project_id="p", chapter_number=4, node_patches=[
        NodePatch(node_id="a", node_type="character", op="set", field_path="state", old_value={}, new_value={"alive_status": "dead"}),
        NodePatch(node_id="a", node_type="character", op="set", field_path="state.alive_status", old_value="dead", new_value="alive"),
    ])
    changes = changes_from_delta(runtime, delta)
    assert evaluate_rules(runtime, active_rule_predicates(runtime), changes)[0].ref == "a"
    assert runtime.world.get_state("a")["alive_status"] == "alive"
    runtime = _runtime(rule("exclusive_relation", edge_type="serves", max_per_source=1))
    runtime.world.add_edge(WorldEdge(id="e", project_id="p", source_id="a", target_id="g",
                                    edge_type="serves", edge_family="loyalty_conflict"))
    delta = GraphDelta(id="d", project_id="p", chapter_number=4, edge_patches=[
        EdgePatch(edge_id="e2", op="create", source_id="a", target_id="f", edge_type="serves",
                  edge_family="loyalty_conflict", new_value={"project_id": "p"}),
    ])
    assert evaluate_rules(runtime, active_rule_predicates(runtime), changes_from_delta(runtime, delta))
    delta.edge_patches.insert(0, EdgePatch(edge_id="e", op="deactivate", field_path="is_active", old_value=True))
    assert not evaluate_rules(runtime, active_rule_predicates(runtime), changes_from_delta(runtime, delta))
    runtime = _runtime(rule("blocked_channel", fact_ref="fact:secret", source_refs=["a"], channels=["told"]))
    delta = GraphDelta(id="d", project_id="p", chapter_number=4, metadata={"knowledge_tags": [
        {"observer_id": "b", "fact_ref": "fact:secret", "channel": "told", "source_id": "a"},
    ]})
    assert evaluate_rules(runtime, active_rule_predicates(runtime), changes_from_delta(runtime, delta))[0].ref == "b"


def test_entry_children_and_membership_exceptions():
    from forwin.book_state.rule_predicates import active_rule_predicates, changes_from_new_intents, evaluate_rules
    runtime = _runtime(rule("actor_cannot_enter", location_ref="l2", except_member_of=["g"]))
    runtime.map.nodes_by_id["l3"] = MapNode(id="l3", project_id="p", node_type="room", parent_id="l2")
    changes = changes_from_new_intents(runtime, _candidate(actor_id="a", new_intents=[_intent(location_id="l3")]))
    assert evaluate_rules(runtime, active_rule_predicates(runtime), changes)
    runtime.world.add_edge(WorldEdge(id="e", project_id="p", source_id="a", target_id="g",
                                    edge_type="member_of", edge_family="organization"))
    assert not evaluate_rules(runtime, active_rule_predicates(runtime), changes)


def test_repair_names_concealed_rule_without_fact_proposition():
    from forwin.review.world_consistency import WorldConsistencyReviewer, _Finding
    from forwin.writer.prompt_core.world_view import UNNAMED_ACTOR_LABEL
    from forwin.book_state.rule_predicates import active_rule_predicates, evaluate_rules, RuleSubjectChanges
    runtime = _runtime(rule("blocked_channel", fact_ref="fact:secret", source_refs=["a"], channels=["told"]))
    violation = evaluate_rules(runtime, active_rule_predicates(runtime),
                              RuleSubjectChanges(knowledge=(("b", "fact:secret", "told", "a"),)))[0]
    issue = WorldConsistencyReviewer()._issue(
        _Finding("error", "rule_violated", violation.message, "rule:r", "gate"),
        {"b": "observer-name", "r": UNNAMED_ACTOR_LABEL, "fact:secret": "SECRET-PROPOSITION"}, {},
        {"r": "concealed-rule-name"})
    assert UNNAMED_ACTOR_LABEL in issue.description
    assert "observer-name" in issue.description
    assert "concealed-rule-name" not in issue.description + issue.suggested_fix
    assert "SECRET-PROPOSITION" not in issue.description + issue.suggested_fix


@pytest.mark.parametrize("params", [
    {"edge_type": "invented", "max_per_source": 1},
    {"edge_type": "serves", "max_per_source": True},
    {"edge_type": "serves", "max_per_source": 0},
])
def test_exclusive_relation_rejects_invalid_enum_or_integer(params):
    from forwin.book_state.rule_predicates import parse_checkable
    assert parse_checkable({"kind": "exclusive_relation", **params}, scope=None)[1]


def test_soft_and_inactive_travel_rules_do_not_block():
    from forwin.book_state.rule_predicates import active_rule_predicates, changes_from_delta, evaluate_rules
    from forwin.world_engine.travel import estimate_travel
    from tests.test_world_constraints import LEXICON
    runtime = _runtime(rule("requires_travel_mode", hard=False, edge_types=["road"], mode_ids=["m"]))
    assert estimate_travel(runtime, LEXICON, mover_id="a", from_location_id="l1", to_location_id="l2").minutes == 120
    delta = GraphDelta(id="d", project_id="p", chapter_number=4,
                      metadata={"movements": [{"entity_id": "a", "to_id": "l2"}]})
    violations = evaluate_rules(runtime, active_rule_predicates(runtime), changes_from_delta(runtime, delta))
    assert len(violations) == 1 and not violations[0].hard
    runtime.world.states_by_node_id["r"]["status"] = "inactive"
    assert active_rule_predicates(runtime) == []


def test_new_actor_cannot_appear_at_forbidden_site_or_reverse_terminal_state():
    from forwin.book_state.rule_predicates import active_rule_predicates, changes_from_delta, evaluate_rules
    runtime = _runtime(rule("actor_cannot_enter", location_ref="l2"))
    delta = GraphDelta(id="d", project_id="p", chapter_number=4, node_patches=[
        NodePatch(node_id="new-actor", node_type="character", op="create", new_value={
            "project_id": "p", "state": {"location_id": "l2", "alive_status": "dead"},
        }),
        NodePatch(node_id="new-actor", node_type="character", op="set", field_path="state.alive_status",
                  old_value="dead", new_value="alive"),
    ])
    assert evaluate_rules(runtime, active_rule_predicates(runtime), changes_from_delta(runtime, delta))[0].ref == "new-actor"
    runtime = _runtime(rule("irreversible_state", node_types=["character"], field="alive_status", value="dead"))
    assert evaluate_rules(runtime, active_rule_predicates(runtime), changes_from_delta(runtime, delta))[0].ref == "new-actor"


def test_exclusive_relation_counts_reactivated_edge():
    from forwin.book_state.rule_predicates import active_rule_predicates, changes_from_delta, evaluate_rules
    runtime = _runtime(rule("exclusive_relation", edge_type="serves", max_per_source=1))
    for ref, target, active in [("old", "g", False), ("current", "f", True)]:
        runtime.world.add_edge(WorldEdge(id=ref, project_id="p", source_id="a", target_id=target,
            edge_type="serves", edge_family="loyalty_conflict", is_active=active,
            ended_at_chapter=None if active else 3))
    delta = GraphDelta(id="d", project_id="p", chapter_number=4,
                      edge_patches=[EdgePatch(edge_id="old", op="create")])
    assert evaluate_rules(runtime, active_rule_predicates(runtime), changes_from_delta(runtime, delta))[0].ref == "a"


@pytest.mark.parametrize("record_indices", [(), (0,), (0, 2), (0, 1, 2)])
def test_repeated_destinations_keep_each_legs_origin_and_modes(monkeypatch, record_indices):
    from copy import deepcopy
    from forwin.book_state.map_graph import MapGraph
    from forwin.book_state.rule_predicates import (
        active_rule_predicates, changes_from_delta, evaluate_rules, runtime_for_movement,
    )
    from forwin.protocol.book_state import MapEdge
    from forwin.world_engine.travel import estimate_travel, estimate_travel_from
    from tests.test_world_gate_v6 import _codes, _review_travel_delta, _travel_rule_fixture

    runtime, lexicon = _travel_rule_fixture(monkeypatch, modes=["m"], hard=False)
    runtime.map = MapGraph(nodes=list(runtime.map.nodes_by_id.values()), edges=[
        *runtime.map.edges_by_id.values(),
        MapEdge(id="back", project_id="p", from_node_id="l2", to_node_id="l1", edge_type="path", travel_time=2),
    ])
    before = deepcopy(runtime.world.states_by_node_id)
    patches = [
        NodePatch(node_id="a", node_type="character", op="set", field_path="state.location_id",
                  old_value="l1", new_value="l2"),
        NodePatch(node_id="a", node_type="character", op="remove", field_path="state.travel_mode_ids",
                  old_value=["m"], new_value="m"),
        NodePatch(node_id="a", node_type="character", op="merge", field_path="state",
                  old_value=runtime.world.get_state("a"), new_value={"location_id": "l1"}),
        NodePatch(node_id="a", node_type="character", op="set", field_path="state.location_id",
                  old_value="l1", new_value="l2"),
    ]
    movements = [
        {"entity_id": "a", "from_id": "l1", "to_id": "l2", "depart_minute": 600, "arrive_minute": 660},
        {"entity_id": "a", "from_id": "l2", "to_id": "l1", "depart_minute": 660, "arrive_minute": 780},
        {"entity_id": "a", "from_id": "l1", "to_id": "l2", "depart_minute": 780, "arrive_minute": 900},
    ]
    movements = [movements[index] for index in record_indices]
    delta = GraphDelta(id="d", project_id="p", chapter_number=4, node_patches=patches,
                       metadata={"movements": movements})
    changes = changes_from_delta(runtime, delta)
    violations = evaluate_rules(runtime, active_rule_predicates(runtime), changes)
    assert len(violations) == 1 and not violations[0].hard
    verdict = _review_travel_delta(runtime, patches, movements)
    assert verdict.accepted and _codes(verdict) == [("warning", "rule_violated")]
    assert changes.moves == (("a", "l2"), ("a", "l1"), ("a", "l2"))
    for leg, start, target, modes, minutes in zip(changes.movement_legs,
            ("l1", "l2", "l1"), ("l2", "l1", "l2"), (("m",), (), ()), (60, 120, 120), strict=True):
        assert (leg.from_id, leg.to_id, leg.mode_ids) == (start, target, modes)
        effective = runtime_for_movement(runtime, leg)
        travel = estimate_travel(effective, lexicon, mover_id="a", from_location_id=start, to_location_id=target)
        assert travel.reachable and travel.minutes == minutes
        assert estimate_travel_from(effective, lexicon, mover_id="a", from_location_id=start)[target] == travel
        independent = deepcopy(runtime)
        independent.world.states_by_node_id["a"]["travel_mode_ids"] = list(modes)
        assert estimate_travel(independent, lexicon, mover_id="a", from_location_id=start, to_location_id=target) == travel
    assert runtime.world.states_by_node_id == before
