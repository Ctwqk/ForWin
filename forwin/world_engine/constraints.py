"""Deterministic impact, world-rule, narrative-protection and budget checks."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from forwin.book_state.knowledge import FACT_REF_PREFIX, HIDDEN_SENSITIVITY
from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.path_patch import apply_path_patch
from forwin.book_state.protections import (
    ENDED_STATUSES, active_protections, breaks_protection, guarded_path, paths_overlap, value_at, world_field_path,
)
from forwin.book_state.rule_predicates import active_rule_predicates, changes_from_new_intents, evaluate_rules
from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph, resolve_world_node_location_id
from forwin.book_state.schema import ACTOR_NODE_TYPES, ACTOR_TIERS, IMPACT_LEVELS, WORLD_NODE_FIELDS
from forwin.protocol.book_state import WorldNode
from forwin.book_state.story_clock import StoryCalendar, StoryClockError, duration_to_minutes
from forwin.world_engine.travel import estimate_travel
from forwin.world_engine.types import NEW_FACT_REF_PREFIX, EffectEntityChange, TickCandidate, WorldEffects


# Node types whose end is major; WORLD_NODE_FIELDS gives each a state.status.
_ENDING_TYPES = frozenset({"faction", "organization", "group", "site_state"})
# State fields whose change is major on the node types WORLD_NODE_FIELDS gives them to: who leads a
# faction or group, and which faction controls a site.
_MAJOR_STATE_FIELDS = frozenset({"leader_id", "controller_faction_id"})
# The knowledge states in which an observer holds a fact as known.
_KNOWN_STATES = frozenset({"knows", "believes"})
# Physical state of held objects; knowledge references and descriptive bookkeeping stay allowed.
_HELD_PHYSICAL_FIELDS = {
    "item": frozenset({
        "status", "owner_id", "holder_id", "equipped_by_id", "location_id", "durability",
        "seal_status", "awakened_level", "charge_level", "current_form", "lock_status", "contamination_state",
    }),
    "resource": frozenset({
        "quantity", "owner_id", "holder_id", "location_id", "controlled_by_id", "flow_rate",
        "quality_level", "reserved_amount", "consumed_amount", "scarcity_level",
    }),
}


@dataclass(frozen=True)
class ConstraintIssue:
    code: str
    message: str
    ref: str


@dataclass(frozen=True)
class BoundaryViolation:
    constraint_node_id: str
    kind: str
    ref: str


@dataclass(frozen=True)
class BudgetResult:
    kept: list[TickCandidate]
    postponed: list[TickCandidate]
    deferred_without_intent: list[TickCandidate]


def is_living_actor(world: ObjectiveWorldGraph, node: WorldNode | None) -> bool:
    """An active actor node that is not dead: one that still takes turns and learns news."""
    return (node is not None and str(node.node_type) in ACTOR_NODE_TYPES and node.is_active
            and world.get_state(node.id).get("alive_status") != "dead")


def _payload(runtime: BookStateRuntime, ref: str) -> dict[str, Any]:
    node = runtime.world.get_node(ref)
    if node is not None:
        return {**node.model_dump(mode="python"), "state": deepcopy(runtime.world.get_state(ref))}
    fact = runtime.world.facts_by_id.get(ref.removeprefix(FACT_REF_PREFIX))
    return fact.model_dump(mode="python") if fact is not None else {"state": {}}


def _changes(candidate: TickCandidate) -> Iterator[tuple[str, str, Any]]:
    # Match the builder's ordering, including its suppression of redundant location edits.
    for change in candidate.agenda_changes:
        if change.agenda_ref == "new":
            continue
        if change.status:
            yield change.agenda_ref, "state.status", change.status
        if change.priority is not None:
            yield change.agenda_ref, "state.priority", _priority(change.priority)
    for ref, status in candidate.intent_status_changes.items():
        yield ref, "state.status", status
    for ref, count in candidate.slip_updates.items():
        yield ref, "state.slip_count", count
    for ref, minute in candidate.next_turn_at.items():
        yield ref, "state.next_turn_at", minute
    movers = {move.entity_ref for move in candidate.effects.movements}
    for change in candidate.effects.entity_changes:
        if change.ref in movers and change.field.removeprefix("state.") == "location_id":
            continue
        yield change.ref, change.field, change.new_value
    for move in candidate.effects.movements:
        yield move.entity_ref, "state.location_id", move.to_location_id
    for transfer in candidate.effects.item_transfers:
        yield transfer.item_ref, "state.holder_id", transfer.to_ref


def _transitions(runtime: BookStateRuntime, changes: Iterable[tuple[str, str, Any]]):
    payloads: dict[str, dict[str, Any]] = {}
    for ref, field, value in changes:
        before = payloads.get(ref, _payload(runtime, ref))
        after = deepcopy(before)
        path = world_field_path(runtime, ref, field)
        apply_path_patch(after, path, deepcopy(value))
        # Report malformed state without letting it corrupt subsequent transitions.
        if isinstance(after.get("state", {}), dict):
            payloads[ref] = after
        yield ref, path, before, after


def _onstage_location_changes(
    runtime: BookStateRuntime,
    transitions: Sequence[tuple[str, str, dict[str, Any], dict[str, Any]]],
    onstage_refs: frozenset[str],
) -> Iterator[str]:
    if not onstage_refs or not transitions:
        return
    # The resolver also reads dependent activity state and site profiles. Keep its semantics
    # on a separate world while applying transitions in builder order, leaving canon untouched.
    world = ObjectiveWorldGraph(
        nodes=list(runtime.world.nodes_by_id.values()), states_by_node_id=runtime.world.states_by_node_id,
    )
    locations = {ref: resolve_world_node_location_id(world, ref) for ref in onstage_refs}
    touched = set()
    for ref, _path, _before, after in transitions:
        world.states_by_node_id[ref] = after.get("state", {})
        node = world.get_node(ref)
        if node is not None:
            world.nodes_by_id[ref] = node.model_copy(update={"profile": after.get("profile", {})})
        for onstage in onstage_refs:
            location = resolve_world_node_location_id(world, onstage)
            if location != locations[onstage]:
                touched.add(onstage)
            locations[onstage] = location
    yield from sorted(touched)


def _refs(value: Any, *, references: bool = False) -> Iterator[str]:
    """Read structural reference fields, never prose or schema labels."""
    if isinstance(value, str) and references:
        yield value
        if value.startswith(FACT_REF_PREFIX):
            yield value.removeprefix(FACT_REF_PREFIX)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key not in {"summary", "proposition", "goal", "motivation", "candidate_id"}:
                is_ref = (references or key in {"ref", "new_value", "known_by"}
                          or key.endswith(("_ref", "_refs", "_id", "_ids")))
                yield from _refs(item, references=is_ref)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _refs(item, references=references)


def milestone_change_references(candidate: TickCandidate) -> frozenset[str]:
    """Refs whose world state changes, excluding authorship, proposals and bookkeeping (R28)."""
    effects = candidate.effects
    refs = {
        change.ref for change in effects.entity_changes
        if not (candidate.origin == "onstage" and change.field in {"requires_onstage", "profile.requires_onstage"})
    }
    refs.update(move.entity_ref for move in effects.movements)
    refs.update(ref for transfer in effects.item_transfers for ref in (transfer.item_ref, transfer.to_ref))
    refs.update(ref for change in effects.relation_changes for ref in (change.source_ref, change.target_ref))
    refs.update(ref for actor in effects.new_actors for ref in actor.affiliation_refs)
    for knowledge in effects.knowledge:
        refs.update((knowledge.fact_ref, knowledge.fact_ref.removeprefix(FACT_REF_PREFIX)))
    return frozenset(refs)


def classify_impact(
    runtime: BookStateRuntime,
    candidate: TickCandidate,
    *,
    milestone_refs: frozenset[str] = frozenset(),
) -> str:
    level = IMPACT_LEVELS.index(candidate.impact_level)
    references = milestone_change_references(candidate)
    if milestone_refs.intersection(references):
        return "critical"
    if candidate.effects.relation_changes or candidate.effects.new_actors:
        level = max(level, IMPACT_LEVELS.index("moderate"))
    transitions = list(_transitions(runtime, _changes(candidate)))
    for ref, path, before, after in transitions:
        if before == after:
            continue
        node = runtime.world.get_node(ref)
        kind = str(node.node_type) if node else ""
        old, new = before.get("state", {}), after.get("state", {})
        if not isinstance(old, dict) or not isinstance(new, dict):
            continue
        changed = {field for field in old.keys() | new.keys() if old.get(field) != new.get(field)}
        fields = WORLD_NODE_FIELDS.get(kind, {}).get("state", set())
        major = (
            (kind == "character" and "alive_status" in changed and new.get("alive_status") == "dead"
             and runtime.world.get_state(ref).get("actor_tier") in {"major", "minor"})
            or (kind in _ENDING_TYPES and not _ended(old) and _ended(new))
            or bool(changed & _MAJOR_STATE_FIELDS & fields)
            or (kind == "world_variable" and before.get("profile", {}).get("holder_refs")
                != after.get("profile", {}).get("holder_refs"))
        )
        moderate = (
            (kind in {"item", "resource"} and "holder_id" in changed)
            or (kind == "world_variable" and "level" in changed)
            or "restraint" in changed
            or (kind == "character" and bool(changed - {"location_id", "next_turn_at"}))
        )
        if major:
            level = max(level, IMPACT_LEVELS.index("major"))
        elif moderate:
            level = max(level, IMPACT_LEVELS.index("moderate"))
    hidden = {
        f"{FACT_REF_PREFIX}{fact.id}"
        for fact in runtime.world.facts_by_id.values() if fact.sensitivity_level in HIDDEN_SENSITIVITY
    }
    hidden.update(f"fact:new:{fact.key}" for fact in candidate.effects.new_facts if fact.sensitivity in HIDDEN_SENSITIVITY)
    for ref, path, before, after in transitions:
        node = runtime.world.get_node(ref)
        state = after.get("state", {})
        if not isinstance(state, dict):
            continue
        if (node is not None and node.node_type == "event" and state.get("visibility_level") == "public"
                and hidden.intersection(_refs(after))):
            level = max(level, IMPACT_LEVELS.index("major"))
    if candidate.origin == "drift" and any(event.visibility == "public" for event in candidate.effects.events):
        variable = runtime.world.get_node(candidate.candidate_id.removeprefix("drift:"))
        if variable is not None and variable.profile.get("visibility") == "secret":
            level = max(level, IMPACT_LEVELS.index("major"))
    if _exposes_secret(runtime, candidate):
        level = max(level, IMPACT_LEVELS.index("major"))
    return IMPACT_LEVELS[level]


def _ended(state: dict[str, Any]) -> bool:
    return state.get("status") in ENDED_STATUSES


def _exposes_secret(runtime: BookStateRuntime, candidate: TickCandidate) -> bool:
    """A knowledge effect makes a hidden or secret fact known to an observer that did not know it.

    A new hidden fact counts for the observers outside its known_by; its known_by made it.
    """
    makers = {
        f"{NEW_FACT_REF_PREFIX}{fact.key}": set(fact.known_by)
        for fact in candidate.effects.new_facts if fact.sensitivity in HIDDEN_SENSITIVITY
    }
    for item in candidate.effects.knowledge:
        if item.state not in _KNOWN_STATES:
            continue
        if item.fact_ref in makers:
            if item.observer_ref not in makers[item.fact_ref]:
                return True
            continue
        fact = runtime.world.facts_by_id.get(item.fact_ref.removeprefix(FACT_REF_PREFIX))
        if fact is None or fact.sensitivity_level not in HIDDEN_SENSITIVITY:
            continue
        views = [view for (_kind, observer_id), view in runtime.cognition_by_observer.items()
                 if observer_id == item.observer_ref]
        if not any(view.knowledge_by_ref.get(item.fact_ref) in _KNOWN_STATES for view in views):
            return True
    return False


def _owner(runtime: BookStateRuntime, candidate: TickCandidate) -> str:
    if candidate.actor_id:
        return candidate.actor_id
    intent = runtime.world.get_node(candidate.intent_id)
    return str(intent.profile.get("owner_ref") or "") if intent else ""


def _restraint(payload: dict[str, Any]) -> str:
    state = payload.get("state") or {}
    restraint = state.get("restraint") if isinstance(state, dict) else None
    return str((restraint.get("state") if isinstance(restraint, dict) else None) or "free")


def _drift_issues(runtime: BookStateRuntime, candidate: TickCandidate) -> list[ConstraintIssue]:
    ref = candidate.candidate_id.removeprefix("drift:")
    variable = runtime.world.get_node(ref)
    if variable is None or variable.node_type != "world_variable":
        return [ConstraintIssue("unknown_ref", "Drift must name a world variable", ref)]
    issues = []
    old_level = runtime.world.get_state(ref).get("level")
    scale = variable.profile.get("scale", [])
    for change in candidate.effects.entity_changes:
        if change.ref != ref or change.field.removeprefix("state.") != "level":
            continue
        new_level = change.new_value
        if type(new_level) is not int or not 0 <= new_level < len(scale):
            issues.append(ConstraintIssue("drift_level_out_of_range", "Drift level must index the variable scale", ref))
        if type(new_level) is int and type(old_level) is int and abs(new_level - old_level) > 1:
            issues.append(ConstraintIssue("drift_step_too_large", "Drift may move at most one level", ref))
    scope = set(variable.profile.get("scope_refs", []))
    allowed = scope | set(variable.profile.get("holder_refs", []))
    allowed.update(node.id for node in runtime.world.nodes_by_id.values()
                   if node.node_type in ACTOR_NODE_TYPES
                   and resolve_world_node_location_id(runtime.world, node.id) in scope)
    for event in candidate.effects.events:
        for participant in event.participant_refs:
            if participant not in allowed:
                issues.append(ConstraintIssue("drift_participant_out_of_scope",
                                              "Drift event participant must be a holder or within scope", participant))
        if event.location_id and event.location_id not in scope and event.location_id not in runtime.map.nodes_by_id:
            issues.append(ConstraintIssue("unknown_ref", "Drift event location must be in scope or on the map",
                                          event.location_id))
    return issues


def check_hard_rules(
    runtime: BookStateRuntime,
    lexicon: BookLexicon,
    candidate: TickCandidate,
    *,
    now: int,
    calendar: StoryCalendar,
    window_start: int,
    onstage_refs: frozenset[str] = frozenset(),
    last_arrival_by_mover: Mapping[str, int] | None = None,
) -> list[ConstraintIssue]:
    """Check against the pre-tick runtime using the caller's calendar and tick window."""
    # The review gate imports world-engine types; defer this import to avoid a cycle.
    from forwin.book_state.reviewer import _MINUTE_EPSILON

    issues: list[ConstraintIssue] = []

    def issue(code: str, ref: str, message: str) -> None:
        issues.append(ConstraintIssue(code, message, ref))

    if candidate.origin == "drift":
        issues.extend(_drift_issues(runtime, candidate))

    owner = _owner(runtime, candidate)
    actors = {owner} if candidate.new_intents else set()
    for intent_id, status in candidate.intent_status_changes.items():
        if status != "resolved":
            continue
        intent = runtime.world.get_node(intent_id)
        if intent is not None:
            actors.add(str(intent.profile.get("owner_ref") or ""))
    actors.update(move.entity_ref for move in candidate.effects.movements)
    for event in candidate.effects.events:
        actors.update(event.participant_refs)
    for knowledge in candidate.effects.knowledge:
        if knowledge.observer_ref != "reader":
            actors.add(knowledge.observer_ref)
        if knowledge.channel == "told" and knowledge.source_ref:
            actors.add(knowledge.source_ref)
    for fact in candidate.effects.new_facts:
        actors.update(ref for ref in fact.known_by if ref != "reader")
    transitions = []
    for transition in _transitions(runtime, _changes(candidate)):
        ref, path, before, after = transition
        if not isinstance(before.get("state", {}), dict) or not isinstance(after.get("state", {}), dict):
            issue("invalid_state", ref, "Entity state must be a dict")
            continue
        transitions.append(transition)
    movers = {move.entity_ref for move in candidate.effects.movements}
    for ref in _onstage_location_changes(runtime, transitions, onstage_refs):
        issue("touches_onstage_entity", ref, f"Candidate changes onstage entity location {ref}")
    for ref, path, before, after in transitions:
        old, new = before.get("state", {}), after.get("state", {})
        if ref in onstage_refs and any(
            old.get(field) != new.get(field)
            for field in ("alive_status", "restraint", "item_ids", "equipped_item_ids")
        ):
            issue("touches_onstage_entity", ref, f"Candidate changes onstage entity {ref}")
        held_fields = _HELD_PHYSICAL_FIELDS.get(before.get("node_type"), ())
        if old.get("holder_id") != new.get("holder_id") or any(
            old.get(field) != new.get(field) for field in held_fields
        ):
            for holder in sorted(onstage_refs.intersection(
                value for value in (old.get("holder_id"), new.get("holder_id")) if isinstance(value, str)
            )):
                issue("touches_onstage_entity", holder, f"Candidate changes held item {ref} of {holder}")
        if old.get("location_id") != new.get("location_id"):
            movers.add(ref)
    actors.update(movers)
    for ref in sorted(actors - {""}):
        if runtime.world.get_state(ref).get("alive_status") == "dead":
            issue("dead_owner_acts", ref, f"Dead entity {ref} acts in this candidate")
    for ref in sorted(movers):
        if _restraint(_payload(runtime, ref)) != "free" and not any(
            changed == ref and (path == "state" or path.startswith("state.")) and _restraint(after) == "free"
            for changed, path, before, after in transitions
        ):
            issue("restrained_moves_without_release", ref, f"Restrained entity {ref} moves without release")

    holders: dict[str, str] = {}
    # Entity holder changes precede explicit transfer records in the delta builder.
    for change in candidate.effects.entity_changes:
        if world_field_path(runtime, change.ref, change.field) == "state.holder_id":
            holders[change.ref] = str(change.new_value or "")
    for transfer in candidate.effects.item_transfers:
        node = runtime.world.get_node(transfer.item_ref)
        if node is None or node.node_type not in {"item", "resource"}:
            continue
        current = holders.get(transfer.item_ref, str(runtime.world.get_state(transfer.item_ref).get("holder_id") or ""))
        if current and transfer.from_ref and current != transfer.from_ref:
            issue("resource_not_held", transfer.item_ref,
                  f"{transfer.item_ref} is held by {current}, not {transfer.from_ref}")
        holders[transfer.item_ref] = transfer.to_ref

    for violation in evaluate_rules(runtime, active_rule_predicates(runtime), changes_from_new_intents(runtime, candidate)):
        if violation.hard:
            issue("rule_violated", violation.ref, violation.message)
    for intent in candidate.new_intents:
        try:
            delay = duration_to_minutes(calendar, lexicon,
                                        amount=max(0.0, intent.scheduled_in.amount), unit_name=intent.scheduled_in.unit)
        except StoryClockError as exc:
            issue("time_not_monotonic", owner, f"Cannot determine scheduled_at: {exc}")
            continue
        if intent.location_id:
            start = resolve_world_node_location_id(runtime.world, owner)
            travel = estimate_travel(runtime, lexicon, mover_id=owner, from_location_id=start,
                                     to_location_id=intent.location_id)
            if (not travel.reachable or not travel.route_known or travel.minutes is None
                    or travel.minutes > delay + _MINUTE_EPSILON):
                issue("unreachable_before_scheduled", owner,
                      f"{owner} cannot reach {intent.location_id} in {delay} minutes")
    for ref, path, before, after in transitions:
        state = after.get("state", {})
        scheduled = state.get("scheduled_at")
        if (path == "state" or path == "state.scheduled_at") and isinstance(scheduled, int) and scheduled < now:
            issue("time_not_monotonic", ref, f"scheduled_at {scheduled} precedes now {now}")
        actual = state.get("actual_story_time")
        if (path in {"state", "state.actual_story_time"} and type(actual) is int
                and not window_start <= actual <= now):
            issue("time_not_monotonic", ref, f"Event time {actual} is outside [{window_start}, {now}]")
    arrivals = dict(last_arrival_by_mover or {})
    for move in candidate.effects.movements:
        last_arrival = arrivals.get(move.entity_ref)
        if last_arrival is not None and move.depart_at < last_arrival:
            issue("movement_overlap", move.entity_ref,
                  f"Movement departs at {move.depart_at} before last arrival {last_arrival}")
        arrivals[move.entity_ref] = max(move.arrive_at, last_arrival if last_arrival is not None else move.arrive_at)
        if not window_start <= move.depart_at <= move.arrive_at <= now:
            issue("time_not_monotonic", move.entity_ref,
                  f"Movement [{move.depart_at}, {move.arrive_at}] is outside [{window_start}, {now}]")
    for event in candidate.effects.events:
        if not window_start <= event.at <= now:
            issue("time_not_monotonic", candidate.candidate_id,
                  f"Event time {event.at} is outside [{window_start}, {now}]")
    return issues


def check_narrative_boundaries(
    runtime: BookStateRuntime, candidate: TickCandidate, *, next_chapter: int,
) -> list[BoundaryViolation]:
    knowledge = {(item.observer_ref, item.fact_ref) for item in candidate.effects.knowledge
                 if item.state in {"knows", "believes"}}
    knowledge.update((observer, f"fact:new:{fact.key}") for fact in candidate.effects.new_facts for observer in fact.known_by)
    return narrative_boundary_violations(
        runtime, changes=_changes(candidate), knowledge=knowledge, next_chapter=next_chapter,
    )


def narrative_boundary_violations(
    runtime: BookStateRuntime,
    *,
    changes: Iterable[tuple[str, str, Any]],
    knowledge: Collection[tuple[str, str]],
    next_chapter: int,
) -> list[BoundaryViolation]:
    """The protections in force in ``next_chapter`` that the changes or the knowledge break.

    ``changes`` are (ref, field, value) in the order they apply, a field bare or with its ``state.`` /
    ``profile.`` section; ``knowledge`` holds (observer ref, ``fact:<id>``) pairs the observers come to know.
    """
    transitions = list(_transitions(runtime, changes))
    violations: list[BoundaryViolation] = []
    for protection in active_protections(runtime, chapter=next_chapter):
        if protection.kind == "must_not_know":
            violated = (protection.observer_ref, protection.fact_ref) in knowledge
        else:
            path = guarded_path(runtime, protection)
            violated = any(
                changed == protection.ref and paths_overlap(changed_path, path)
                and breaks_protection(protection, value_at(before, path), value_at(after, path))
                for changed, changed_path, before, after in transitions
            )
        if violated:
            violations.append(BoundaryViolation(protection.node_id, protection.kind, protection.ref))
    return violations


def _priority(value: Any) -> int:
    return min(5, max(1, value)) if type(value) is int else 3


def apply_budgets(
    candidates: Sequence[TickCandidate],
    *,
    impacts: Mapping[str, str],
    max_major_events: int,
    runtime: BookStateRuntime,
    now: int,
) -> BudgetResult:
    """Keep major events by owner tier, descending agenda priority, then id.

    Postponed candidates contain only planned-at-now bookkeeping for existing
    intents. Candidates without an existing intent are deferred unchanged for retry.
    """
    missing = sorted({candidate.candidate_id for candidate in candidates} - impacts.keys())
    if missing:
        raise ValueError(f"Missing computed impacts for candidates: {', '.join(missing)}")

    def order(candidate: TickCandidate) -> tuple[int, int, str]:
        tier = runtime.world.get_state(_owner(runtime, candidate)).get("actor_tier")
        agenda_refs = {intent.agenda_ref for intent in candidate.new_intents}
        intent = runtime.world.get_node(candidate.intent_id)
        if intent is not None:
            agenda_refs.add(str(intent.profile.get("agenda_ref") or ""))
        agenda_refs.update(change.agenda_ref for change in candidate.agenda_changes)
        priorities = {ref: _priority(runtime.world.get_state(ref).get("priority")) for ref in agenda_refs if ref}
        for change in candidate.agenda_changes:
            if change.priority is not None:
                priorities[change.agenda_ref] = _priority(change.priority)
        return (ACTOR_TIERS.index(tier) if tier in ACTOR_TIERS else len(ACTOR_TIERS),
                -max(priorities.values(), default=3), candidate.candidate_id)

    major = sorted((candidate for candidate in candidates
                    if impacts[candidate.candidate_id] in {"major", "critical"}), key=order)
    over_budget = major[max(0, max_major_events):]
    postponed_ids = {candidate.candidate_id for candidate in over_budget}
    kept = [candidate for candidate in candidates if candidate.candidate_id not in postponed_ids]
    postponed: list[TickCandidate] = []
    deferred_without_intent: list[TickCandidate] = []
    for candidate in over_budget:
        intents = sorted(ref for ref in {candidate.intent_id, *candidate.intent_status_changes}
                         if (node := runtime.world.get_node(ref)) is not None and node.node_type == "intent")
        if not intents:
            deferred_without_intent.append(candidate)
            continue
        postponed.append(candidate.model_copy(update={
            "impact_level": "minor", "summary": "Intent postponed by event budget",
            "effects": WorldEffects(entity_changes=[
                EffectEntityChange(ref=ref, field="scheduled_at", new_value=now) for ref in intents
            ]),
            "new_intents": [], "agenda_changes": [], "slip_updates": {}, "next_turn_at": {},
            "intent_status_changes": {ref: "planned" for ref in intents},
        }, deep=True))
    return BudgetResult(kept, postponed, deferred_without_intent)
