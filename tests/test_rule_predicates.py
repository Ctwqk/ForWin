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
