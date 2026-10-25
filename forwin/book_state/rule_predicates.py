"""Structural predicates over BookState changes, independent of narrative text."""

from __future__ import annotations

from collections.abc import Mapping
from copy import copy, deepcopy
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from forwin.book_state.knowledge import FACT_REF_PREFIX, KNOWLEDGE_CHANNELS
from forwin.book_state.path_patch import apply_path_patch
from forwin.book_state.protections import ENDED_STATUSES
from forwin.book_state.runtime import BookStateRuntime, _reactivated, _reactivates
from forwin.book_state.schema import ACTOR_NODE_TYPES, WORLD_NODE_FIELDS
from forwin.protocol.book_state import GraphDelta, MapEdgeType, WORLD_EDGE_TYPES

RULE_PREDICATE_KINDS = (
    "actor_cannot_enter", "requires_travel_mode", "exclusive_relation", "irreversible_state", "blocked_channel",
)
_PARAMS = {
    "actor_cannot_enter": {"location_ref", "include_children", "except_refs", "except_member_of"},
    "requires_travel_mode": {"edge_types", "mode_ids"},
    "exclusive_relation": {"edge_type", "max_per_source"},
    "irreversible_state": {"node_types", "field", "value"},
    "blocked_channel": {"fact_ref", "source_refs", "channels"},
}


class RulePredicate(BaseModel):
    rule_id: str = ""
    hard: bool = True
    kind: str
    params: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class RuleRefScope:
    locations: frozenset[str]
    actors: frozenset[str]
    facts: frozenset[str]
    modes: frozenset[str]


def parse_checkable(payload: Mapping, *, scope: RuleRefScope | None) -> tuple[RulePredicate | None, list[str]]:
    """Parse a flat checkable object; node identity and hardness are supplied by its owner."""
    if not isinstance(payload, Mapping):
        return None, ["checkable must be an object"]
    kind = payload.get("kind")
    if not isinstance(kind, str) or kind not in RULE_PREDICATE_KINDS:
        return None, ["unknown_rule_predicate"]
    params = {key: value for key, value in payload.items() if key != "kind"}
    issues: list[str] = []
    if params.keys() - _PARAMS[kind]:
        issues.append("unexpected parameters")

    def token(key, allowed=None):
        value = params.get(key)
        if not isinstance(value, str) or not value.strip() or (allowed is not None and value not in allowed):
            issues.append(f"invalid {key}")

    def tokens(key, allowed=None, *, optional=False):
        values = params.get(key, [] if optional else None)
        if (not isinstance(values, list) or (not optional and not values)
                or any(not isinstance(v, str) or not v.strip() or (allowed is not None and v not in allowed)
                       for v in values)):
            issues.append(f"invalid {key}")

    if kind == "actor_cannot_enter":
        token("location_ref", scope.locations if scope else None)
        tokens("except_refs", scope.actors if scope else None, optional=True)
        tokens("except_member_of", scope.actors if scope else None, optional=True)
        params.setdefault("include_children", True)
        params.setdefault("except_refs", [])
        params.setdefault("except_member_of", [])
        if type(params["include_children"]) is not bool:
            issues.append("invalid include_children")
    elif kind == "requires_travel_mode":
        tokens("edge_types", {edge.value for edge in MapEdgeType})
        tokens("mode_ids", scope.modes if scope else None)
    elif kind == "exclusive_relation":
        token("edge_type", WORLD_EDGE_TYPES)
        maximum = params.get("max_per_source")
        if type(maximum) is not int or maximum < 1:
            issues.append("invalid max_per_source")
    elif kind == "irreversible_state":
        tokens("node_types", WORLD_NODE_FIELDS)
        token("field", {"alive_status", "status"})
        token("value", {"dead"} if params.get("field") == "alive_status" else ENDED_STATUSES)
        if not issues and any(params["field"] not in WORLD_NODE_FIELDS[t]["state"] for t in params["node_types"]):
            issues.append("field unavailable on node_types")
    elif kind == "blocked_channel":
        token("fact_ref", scope.facts if scope else None)
        tokens("source_refs", scope.actors if scope else None)
        tokens("channels", KNOWLEDGE_CHANNELS)
    return (None, issues) if issues else (RulePredicate(kind=kind, params=deepcopy(params)), [])


def active_rule_predicates(runtime: BookStateRuntime) -> list[RulePredicate]:
    result = []
    for node in runtime.world.nodes_by_id.values():
        if node.node_type != "rule" or not node.is_active or runtime.world.get_state(node.id).get("status") != "active":
            continue
        predicate, _issues = parse_checkable(node.profile.get("checkable"), scope=None)
        if predicate is not None:
            result.append(predicate.model_copy(update={"rule_id": node.id, "hard": node.profile.get("hardness") == "hard"}))
    return result


@dataclass(frozen=True)
class RuleMovementLeg:
    mover_id: str
    from_id: str
    to_id: str
    mode_ids: tuple[str, ...]
    movement_index: int | None = None


def runtime_for_movement(runtime: BookStateRuntime, leg: RuleMovementLeg) -> BookStateRuntime:
    """A travel view with the mover's modes at this leg, leaving the base world untouched."""
    effective = copy(runtime)
    effective.world = copy(runtime.world)
    effective.world.states_by_node_id = dict(runtime.world.states_by_node_id)
    effective.world.states_by_node_id[leg.mover_id] = {
        **runtime.world.get_state(leg.mover_id),
        "location_id": leg.from_id, "travel_mode_ids": list(leg.mode_ids),
    }
    return effective


@dataclass(frozen=True)
class RuleSubjectChanges:
    moves: tuple[tuple[str, str], ...] = ()
    relation_creates: tuple[tuple[str, str, str], ...] = ()
    relation_ends: tuple[str, ...] = ()
    state_transitions: tuple[tuple[str, str, Any, Any], ...] = ()
    knowledge: tuple[tuple[str, str, str, str], ...] = ()
    intent_locations: tuple[tuple[str, str], ...] = ()
    movement_legs: tuple[RuleMovementLeg, ...] = ()


def _records(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _active_edge(payload):
    return payload.get("is_active", True) and payload.get("status", "active") == "active" and payload.get("ended_at_chapter") is None


def changes_from_delta(runtime: BookStateRuntime, delta: GraphDelta) -> RuleSubjectChanges:
    movements = _records(delta.metadata.get("movements"))
    matched: set[int] = set()
    patched_legs = []
    transitions = []
    payloads = {}
    for patch in delta.node_patches:
        node = runtime.world.get_node(patch.node_id)
        before = payloads.get(patch.node_id, {**node.model_dump(mode="python"),
            "state": deepcopy(runtime.world.get_state(patch.node_id))} if node else {})
        after = deepcopy(before)
        if str(patch.op) == "create":
            after = {"node_type": patch.node_type, **(patch.new_value if isinstance(patch.new_value, dict) else {})}
            transitions.append((patch.node_id, "node_type", None, after["node_type"]))
        elif str(patch.op) == "deactivate":
            after["is_active"] = False
        else:
            apply_path_patch(after, patch.field_path, patch.new_value, op=str(patch.op))
        old, new = before.get("state", {}), after.get("state", {})
        if not isinstance(new, dict):
            continue
        payloads[patch.node_id] = after
        old = old if isinstance(old, dict) else {}
        for field in old.keys() | new.keys():
            if old.get(field) != new.get(field):
                transitions.append((patch.node_id, field, old.get(field), new.get(field)))
        if ((after.get("node_type") in ACTOR_NODE_TYPES or str(patch.op) != "create")
                and old.get("location_id") != new.get("location_id")
                and new.get("location_id")):
            start, target = old.get("location_id") or "", new["location_id"]
            index = next((i for i, m in enumerate(movements) if i not in matched
                          and m.get("entity_id") == patch.node_id and m.get("to_id") == target
                          and (not m.get("from_id") or m["from_id"] == start)), None)
            leg = RuleMovementLeg(patch.node_id, start, target,
                                  tuple(old.get("travel_mode_ids") or ()), index)
            patched_legs.append(leg)
            if index is not None:
                matched.add(index)
    records, locations = {}, {}
    for index, movement in enumerate(movements):
        mover = movement.get("entity_id", "")
        state = runtime.world.get_state(mover)
        start = movement.get("from_id") or locations.get(mover, state.get("location_id") or "")
        target = movement.get("to_id", "")
        # Without a matching location patch there is no ordering anchor for equipment changes.
        if index not in matched:
            records[index] = RuleMovementLeg(mover, start, target, tuple(state.get("travel_mode_ids") or ()), index)
        locations[mover] = target
    legs = []
    for leg in patched_legs:
        if leg.movement_index is not None:
            for index in list(records):
                if index < leg.movement_index:
                    legs.append(records.pop(index))
        legs.append(leg)
    legs.extend(records.values())
    edges = {}
    for patch in delta.edge_patches:
        edge = runtime.world.edges_by_id.get(patch.edge_id)
        payload = deepcopy(edges.get(patch.edge_id, edge.model_dump(mode="python") if edge else {}))
        if str(patch.op) == "create" and edge is not None and _reactivates(edge, delta.chapter_number):
            payload = _reactivated(edge, delta.chapter_number).model_dump(mode="python")
        elif str(patch.op) == "create":
            payload = {"source_id": patch.source_id, "target_id": patch.target_id, "edge_type": patch.edge_type,
                       **(patch.new_value if isinstance(patch.new_value, dict) else {})}
        elif str(patch.op) == "deactivate":
            payload["is_active"] = False
        else:
            apply_path_patch(payload, patch.field_path, patch.new_value, op=str(patch.op))
        edges[patch.edge_id] = payload
    creates, ends = [], []
    for ref, after in edges.items():
        before = runtime.world.edges_by_id.get(ref)
        if before is not None and _active_edge(before.model_dump(mode="python")):
            ends.append(ref)
        if _active_edge(after):
            creates.append((after.get("source_id", ""), after.get("edge_type", ""), after.get("target_id", "")))
    knowledge = tuple((k.get("observer_id", ""), k.get("fact_ref", ""), k.get("channel", ""), k.get("source_id", ""))
                      for k in _records(delta.metadata.get("knowledge_tags")))
    return RuleSubjectChanges(tuple((leg.mover_id, leg.to_id) for leg in legs), tuple(creates), tuple(ends),
                              tuple(transitions), knowledge, movement_legs=tuple(legs))


def changes_from_new_intents(runtime: BookStateRuntime, candidate) -> RuleSubjectChanges:
    intent = runtime.world.get_node(candidate.intent_id)
    owner = candidate.actor_id or (str(intent.profile.get("owner_ref") or "") if intent else "")
    return RuleSubjectChanges(intent_locations=tuple((owner, i.location_id) for i in candidate.new_intents if i.location_id))


@dataclass(frozen=True)
class RuleViolation:
    rule_id: str
    kind: str
    ref: str
    hard: bool
    message: str


def _inside(runtime, location, parent, children):
    seen = set()
    while location and location not in seen:
        if location == parent:
            return True
        seen.add(location)
        node = runtime.map.nodes_by_id.get(location)
        location = node.parent_id if children and node is not None else ""
    return False


def _lacks_mode(runtime, predicate, mover_id):
    modes = runtime.world.get_state(mover_id).get("travel_mode_ids") or []
    return not set(predicate.params["mode_ids"]).intersection(modes)


def blocked_edge_types(runtime: BookStateRuntime, predicates, *, mover_id: str | None) -> frozenset[str]:
    if mover_id is None:
        return frozenset()
    return frozenset(edge for p in predicates if p.hard and p.kind == "requires_travel_mode"
                     and _lacks_mode(runtime, p, mover_id) for edge in p.params["edge_types"])


def evaluate_rules(runtime: BookStateRuntime, predicates, changes: RuleSubjectChanges) -> list[RuleViolation]:
    relations = [(e.source_id, e.edge_type, e.target_id) for e in runtime.world.edges_by_id.values()
                 if e.id not in changes.relation_ends and _active_edge(e.model_dump(mode="python"))]
    relations.extend(changes.relation_creates)
    created_types = {ref: after for ref, field, _before, after in changes.state_transitions if field == "node_type"}
    legs = changes.movement_legs
    if not legs:
        derived, locations = [], {}
        for mover, target in changes.moves:
            state = runtime.world.get_state(mover)
            derived.append(RuleMovementLeg(mover, locations.get(mover, state.get("location_id") or ""),
                                           target, tuple(state.get("travel_mode_ids") or ())))
            locations[mover] = target
        legs = tuple(derived)
    violations = []
    for rule in predicates:
        p, refs = rule.params, set()
        if rule.kind == "actor_cannot_enter":
            for actor, location in (*changes.moves, *changes.intent_locations):
                node = runtime.world.get_node(actor)
                if node is not None and str(node.node_type) not in ACTOR_NODE_TYPES:
                    continue
                exempt = actor in p["except_refs"] or any(
                    source == actor and kind in {"member_of", "leader_of", "serves"} and target in p["except_member_of"]
                    and (group := runtime.world.get_node(target)) is not None
                    and group.node_type in {"faction", "organization", "group"}
                    for source, kind, target in relations)
                if not exempt and _inside(runtime, location, p["location_ref"], p["include_children"]):
                    refs.add(actor)
        elif rule.kind == "exclusive_relation":
            for source, kind, _target in changes.relation_creates:
                if kind == p["edge_type"] and sum(s == source and k == kind for s, k, _t in relations) > p["max_per_source"]:
                    refs.add(source)
        elif rule.kind == "irreversible_state":
            for ref, field, before, after in changes.state_transitions:
                node = runtime.world.get_node(ref)
                node_type = str(node.node_type) if node is not None else created_types.get(ref)
                if (node_type in p["node_types"] and field == p["field"]
                        and before == p["value"] and after != before):
                    refs.add(ref)
        elif rule.kind == "blocked_channel":
            refs.update(observer for observer, fact, channel, source in changes.knowledge
                        if fact.removeprefix(FACT_REF_PREFIX) == p["fact_ref"].removeprefix(FACT_REF_PREFIX)
                        and channel in p["channels"] and source in p["source_refs"])
        elif rule.kind == "requires_travel_mode":
            for leg in legs:
                mover, start, target = leg.mover_id, leg.from_id, leg.to_id
                if not _lacks_mode(runtime_for_movement(runtime, leg), rule, mover):
                    continue
                if not start or start == target:
                    continue
                original = runtime.map.shortest_path(start, target, metric="travel_time")
                allowed = frozenset(str(e.edge_type) for e in runtime.map.edges_by_id.values()) - set(p["edge_types"])
                filtered = runtime.map.shortest_path(start, target, metric="travel_time", allowed_edge_types=allowed)
                if original.reachable and not filtered.reachable:
                    refs.add(mover)
        violations.extend(RuleViolation(rule.rule_id, rule.kind, ref, rule.hard,
                                       f"{ref} violates rule {rule.rule_id} ({rule.kind})") for ref in sorted(refs))
    return violations
