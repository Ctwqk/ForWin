"""Deterministic impact, world-rule, narrative-protection and budget checks."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from forwin.book_state.knowledge import FACT_REF_PREFIX, HIDDEN_SENSITIVITY
from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.path_patch import apply_path_patch
from forwin.book_state.runtime import BookStateRuntime, resolve_world_node_location_id
from forwin.book_state.schema import ACTOR_TIERS, IMPACT_LEVELS, WORLD_NODE_FIELDS
from forwin.book_state.story_clock import StoryCalendar, StoryClockError, duration_to_minutes
from forwin.world_engine.travel import estimate_travel
from forwin.world_engine.types import EffectEntityChange, TickCandidate, WorldEffects


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


def _path(runtime: BookStateRuntime, ref: str, field: str) -> str:
    field = field.strip()
    if field in {"state", "profile", "is_active", "sensitivity_level"} or field.startswith(("state.", "profile.")):
        return field
    if ref.startswith(FACT_REF_PREFIX) and field == "sensitivity":
        return "sensitivity_level"
    node = runtime.world.get_node(ref)
    fields = WORLD_NODE_FIELDS.get(str(node.node_type), {}) if node else {}
    root = field.split(".", 1)[0]
    if root in fields.get("profile", ()) and root not in fields.get("state", ()):
        return f"profile.{field}"
    return f"state.{field}"


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


def _transitions(runtime: BookStateRuntime, candidate: TickCandidate):
    payloads: dict[str, dict[str, Any]] = {}
    for ref, field, value in _changes(candidate):
        before = payloads.get(ref, _payload(runtime, ref))
        after = deepcopy(before)
        path = _path(runtime, ref, field)
        apply_path_patch(after, path, deepcopy(value))
        # Report malformed state without letting it corrupt subsequent transitions.
        if isinstance(after.get("state", {}), dict):
            payloads[ref] = after
        yield ref, path, before, after


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


def classify_impact(
    runtime: BookStateRuntime,
    candidate: TickCandidate,
    *,
    milestone_refs: frozenset[str] = frozenset(),
) -> str:
    level = IMPACT_LEVELS.index(candidate.impact_level)
    references = (set(_refs(candidate.model_dump(mode="python"))) | candidate.intent_status_changes.keys()
                  | candidate.slip_updates.keys() | candidate.next_turn_at.keys())
    if milestone_refs.intersection(references):
        return "critical"
    if candidate.effects.relation_changes:
        level = max(level, IMPACT_LEVELS.index("moderate"))
    transitions = list(_transitions(runtime, candidate))
    for ref, path, before, after in transitions:
        if before == after:
            continue
        node = runtime.world.get_node(ref)
        kind = str(node.node_type) if node else ""
        old, new = before.get("state", {}), after.get("state", {})
        if not isinstance(old, dict) or not isinstance(new, dict):
            continue
        changed = {field for field in old.keys() | new.keys() if old.get(field) != new.get(field)}
        major = (
            (kind == "character" and "alive_status" in changed and new.get("alive_status") == "dead"
             and runtime.world.get_state(ref).get("actor_tier") in {"major", "minor"})
            or (kind in {"faction", "organization", "group", "site_state"}
                and before.get("is_active") is not False and after.get("is_active") is False)
            or (kind in {"faction", "organization", "group"} and "controlled_by_id" in changed)
            or (before.get("sensitivity_level") in HIDDEN_SENSITIVITY and after.get("sensitivity_level") == "public")
        )
        moderate = (
            (kind in {"item", "resource"} and "holder_id" in changed)
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
    # EffectEvent has no dedicated fact_refs field; inspect its structural references.
    if any(event.visibility == "public" and hidden.intersection(event.participant_refs) for event in candidate.effects.events):
        level = max(level, IMPACT_LEVELS.index("major"))
    return IMPACT_LEVELS[level]


def _owner(runtime: BookStateRuntime, candidate: TickCandidate) -> str:
    if candidate.actor_id:
        return candidate.actor_id
    intent = runtime.world.get_node(candidate.intent_id)
    return str(intent.profile.get("owner_ref") or "") if intent else ""


def _restraint(payload: dict[str, Any]) -> str:
    state = payload.get("state") or {}
    restraint = state.get("restraint") if isinstance(state, dict) else None
    return str((restraint.get("state") if isinstance(restraint, dict) else None) or "free")


def check_hard_rules(
    runtime: BookStateRuntime,
    lexicon: BookLexicon,
    candidate: TickCandidate,
    *,
    now: int,
    calendar: StoryCalendar,
    window_start: int,
) -> list[ConstraintIssue]:
    """Check against the pre-tick runtime using the caller's calendar and tick window."""
    # The review gate imports world-engine types; defer this import to avoid a cycle.
    from forwin.book_state.reviewer import _MINUTE_EPSILON

    issues: list[ConstraintIssue] = []

    def issue(code: str, ref: str, message: str) -> None:
        issues.append(ConstraintIssue(code, message, ref))

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
    for transition in _transitions(runtime, candidate):
        ref, path, before, after = transition
        if not isinstance(before.get("state", {}), dict) or not isinstance(after.get("state", {}), dict):
            issue("invalid_state", ref, "Entity state must be a dict")
            continue
        transitions.append(transition)
    movers = {move.entity_ref for move in candidate.effects.movements}
    for ref, path, before, after in transitions:
        old, new = before.get("state", {}), after.get("state", {})
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
        if _path(runtime, change.ref, change.field) == "state.holder_id":
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
    for move in candidate.effects.movements:
        if not window_start <= move.depart_at <= move.arrive_at <= now:
            issue("time_not_monotonic", move.entity_ref,
                  f"Movement [{move.depart_at}, {move.arrive_at}] is outside [{window_start}, {now}]")
    for event in candidate.effects.events:
        if not window_start <= event.at <= now:
            issue("time_not_monotonic", candidate.candidate_id,
                  f"Event time {event.at} is outside [{window_start}, {now}]")
    return issues


def _at(payload: dict[str, Any], path: str) -> Any:
    value: Any = payload
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def check_narrative_boundaries(
    runtime: BookStateRuntime, candidate: TickCandidate, *, next_chapter: int,
) -> list[BoundaryViolation]:
    transitions = list(_transitions(runtime, candidate))
    knowledge = {(item.observer_ref, item.fact_ref) for item in candidate.effects.knowledge
                 if item.state in {"knows", "believes"}}
    knowledge.update((observer, f"fact:new:{fact.key}") for fact in candidate.effects.new_facts for observer in fact.known_by)
    violations: list[BoundaryViolation] = []
    for node in sorted(runtime.narrative.nodes_by_id.values(), key=lambda node: node.id):
        if node.node_type != "review_constraint" or getattr(node, "status", "active") != "active":
            continue
        payload = node.payload
        until = payload.get("until_chapter", 0)
        until = until if type(until) is int else 0
        if until != 0 and until < next_chapter:
            continue
        ref, kind = str(payload.get("ref") or ""), str(payload.get("kind") or "")
        violated = False
        if kind == "must_not_know":
            observer, fact = payload.get("observer_ref"), payload.get("fact_ref")
            if not isinstance(observer, str) or not isinstance(fact, str) or not fact:
                continue
            fact = f"{FACT_REF_PREFIX}{fact.removeprefix(FACT_REF_PREFIX)}"
            violated = (observer, fact) in knowledge
        else:
            if kind == "must_hold_state":
                field = payload.get("field")
                if not isinstance(field, str) or not field.strip():
                    continue
                path, value = _path(runtime, ref, field), payload.get("value")
            else:
                path, value = {
                    "must_exist": ("is_active", True),
                    "must_be_alive": ("state.alive_status", "alive"),
                }.get(kind, ("", None))
            for changed, changed_path, before, after in transitions:
                if changed != ref or not path:
                    continue
                if not (changed_path == path or changed_path.startswith(path + ".") or path.startswith(changed_path + ".")):
                    continue
                old, new = _at(before, path), _at(after, path)
                if old == new:
                    continue
                if kind == "must_exist":
                    violated |= new is False
                elif kind == "must_be_alive":
                    violated |= new == "dead"
                else:
                    violated |= new != value
        if violated:
            violations.append(BoundaryViolation(node.id, kind, ref))
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
