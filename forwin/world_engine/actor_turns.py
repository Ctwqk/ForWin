"""World tick step: due actors take turns from their own cognition, in batched structured calls."""

from __future__ import annotations

import inspect
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.book_state.knowledge import EVENT_FACT_TYPE
from forwin.book_state.story_clock import MINUTE_EPSILON
from forwin.book_state.cognition import CognitionView
from forwin.book_state.extraction.chapter_delta_schema import Duration
from forwin.book_state.delta_helpers import OBSERVER_TYPES
from forwin.book_state.knowledge import FACT_REF_PREFIX
from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.runtime import BookStateRuntime, resolve_world_node_location_id
from forwin.book_state.schema import ACTOR_NODE_TYPES, ACTOR_TIERS
from forwin.book_state.story_clock import StoryCalendar, StoryClockError, duration_to_minutes
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.observability.llm_trace import (
    build_workflow_trace, drain_workflow_attempts,
)
from forwin.protocol.book_state import CognitionOverlay, MapNode, WorldNode
from forwin.runtime.policy import WorldPolicy
from forwin.utils import parse_llm_json
from forwin.world_engine.constraints import ConstraintIssue, is_living_actor
from forwin.world_engine.effect_validation import scoped_effect_reference_issues
from forwin.world_engine.travel import estimate_travel_from
from forwin.world_engine.types import (
    AGENDA_PRIORITY_MAX, AGENDA_PRIORITY_MIN, INVALID_OUTPUT, LLM_UNAVAILABLE, NEW_AGENDA_REF, PROMPT_TOO_LARGE,
    ActorTurnBatch, ActorTurnResult, TickCandidate, TickContext, WorldEffects,
)
from forwin.writer.prompt_core.world_engine import MAX_ACTOR_TURN_PROMPT_CHARS, build_actor_turn_messages


_STAGE_KEY = "world.actor_turn"
_PARSER_NAME = "ActorTurnRunner JSON parser"
MAX_REACHABLE_PLACES = 12
MAX_RECENT_VETOES = 5
MAX_PENDING_INTENTS = 10
# Active agendas a view shows, highest priority first; agenda changes one turn may make.
MAX_ACTOR_AGENDAS = 8
MAX_AGENDA_CHANGES_PER_TURN = 3
# Newest first (recorded seed and static knowledge ahead of event news), so a long book's views
# stay well under the prompt guard.
MAX_KNOWN_FACTS = 60
MAX_PUBLIC_FACTS = 30
# The knowledge states in which an actor holds a fact and acts on it.
_HELD_STATES = frozenset({"heard", "suspects", "believes", "knows"})
_PENDING_INTENT_STATUSES = frozenset({"planned", "due", "held"})
# News of an event reaches an actor only through propagation's recorded knowledge.
_VETO_OPTION = "veto_world"
# The priority an agenda without one ranks at, as the tick's budget reads it.
_DEFAULT_AGENDA_PRIORITY = 3
# Travel minutes absorb float noise before rounding up.


class ActorView(BaseModel):
    """What one actor knows when it takes its turn; list entries are JSON-ready dicts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_id: str
    name: str = ""
    node_type: str
    actor_tier: str = ""
    location_id: str = ""
    location_name: str = ""
    resources: list[Any] = Field(default_factory=list)
    # Its restraint, when it is not free.
    restraint: dict[str, Any] = Field(default_factory=dict)
    # {id, node_type, name} of the items and resources it holds.
    holdings: list[dict[str, Any]] = Field(default_factory=list)
    # {id, goal, motivation, horizon, status, priority, means_refs, constraints, secrecy_refs}: active agendas,
    # highest priority first.
    agendas: list[dict[str, Any]] = Field(default_factory=list)
    # {id, summary, action_type, location_id, scheduled_at, status, target_refs}: its planned, due and held
    # intents, soonest first.
    pending_intents: list[dict[str, Any]] = Field(default_factory=list)
    # {ref, proposition, state, related_refs?}: facts it holds a recorded state for, without truth values,
    # newest first.
    facts: list[dict[str, Any]] = Field(default_factory=list)
    # {ref, proposition, related_refs?}: static public facts it knows without a record, newest first.
    public_facts: list[dict[str, Any]] = Field(default_factory=list)
    # {id, node_type, name} of the other nodes the entries above name.
    entities: list[dict[str, Any]] = Field(default_factory=list)
    # {location_id, name, minutes}: the nearest places it knows a route to.
    reachable_places: list[dict[str, Any]] = Field(default_factory=list)
    # {id, hardness, statement}
    rules: list[dict[str, Any]] = Field(default_factory=list)
    # {id, label, scope, due_at, remaining_minutes, owner_ref?, on_expire?}: running clocks it knows.
    clocks: list[dict[str, Any]] = Field(default_factory=list)
    # Refs it must not touch, without the reasons.
    boundary_refs: list[str] = Field(default_factory=list)
    # {summary, intent_summaries}: recent vetoes of its intents, newest first.
    vetoes: list[dict[str, Any]] = Field(default_factory=list)

    def place_ids(self) -> set[str]:
        """Where its intents may happen: its location and the reachable places."""
        return {self.location_id, *(place["location_id"] for place in self.reachable_places)} - {""}

    def references(self) -> set[str]:
        """The refs its output may name."""
        return {
            self.actor_id,
            *(item["id"] for item in [
                *self.holdings, *self.agendas, *self.pending_intents, *self.entities, *self.clocks,
            ]),
            *(fact["ref"] for fact in [*self.facts, *self.public_facts]),
        } | self.place_ids()


@dataclass(frozen=True)
class ActorTurnCall:
    actor_id: str
    result: ActorTurnResult | None
    # Shared by the actors of one batch.
    prompt_trace: dict
    issues: list[str]
    # Without a result: LLM_UNAVAILABLE when no call returned an answer, INVALID_OUTPUT when the answers
    # never held a valid turn for the actor, PROMPT_TOO_LARGE; empty with a result.
    failure_reason: str = ""


# ranking


def _living_actors(runtime: BookStateRuntime) -> list[WorldNode]:
    return [node for node in runtime.world.nodes_by_id.values() if is_living_actor(runtime.world, node)]


def rank_actors(
    runtime: BookStateRuntime, *, now: int, limit: int, onstage_refs: frozenset[str] = frozenset(),
) -> list[WorldNode]:
    """Living, active actors with ``next_turn_at <= now``, by tier, relevance and id.

    Relevance is the actor's active world edges to major characters plus the major characters
    at its location; more relevant actors come first.
    """
    world = runtime.world
    actors = _living_actors(runtime)
    majors = {
        node.id for node in actors
        if str(node.node_type) == "character" and world.get_state(node.id).get("actor_tier") == "major"
    }
    major_locations = {ref: resolve_world_node_location_id(world, ref) for ref in majors}

    def relevance(node_id: str) -> int:
        linked = 0
        for edge_id in {*world.outgoing_edges.get(node_id, ()), *world.incoming_edges.get(node_id, ())}:
            edge = world.edges_by_id.get(edge_id)
            if edge is None or not edge.is_active:
                continue
            other = edge.target_id if edge.source_id == node_id else edge.source_id
            linked += other != node_id and other in majors
        location = resolve_world_node_location_id(world, node_id)
        colocated = sum(
            ref != node_id and bool(location) and where == location for ref, where in major_locations.items()
        )
        return linked + colocated

    def order(node: WorldNode) -> tuple[int, int, str]:
        tier = world.get_state(node.id).get("actor_tier")
        return (ACTOR_TIERS.index(tier) if tier in ACTOR_TIERS else len(ACTOR_TIERS), -relevance(node.id), node.id)

    due = [
        node for node in actors
        if node.id not in onstage_refs
        and type(turn_at := world.get_state(node.id).get("next_turn_at")) is int and turn_at <= now
    ]
    return sorted(due, key=order)[:max(0, limit)]


# views


def build_actor_view(
    runtime: BookStateRuntime,
    lexicon: BookLexicon,
    *,
    actor_id: str,
    now: int,
    vetoes: Sequence[WorldPlanConflictRow],
    boundary_refs: Sequence[str],
) -> ActorView:
    """The actor's own view: no truth values, reader cognition or facts it holds no state for."""
    world = runtime.world
    actor = world.get_node(actor_id)
    if actor is None or str(actor.node_type) not in ACTOR_NODE_TYPES:
        raise ValueError(f"{actor_id!r} is not an actor node")
    state = world.get_state(actor_id)
    location_id = resolve_world_node_location_id(world, actor_id)
    location = runtime.map.nodes_by_id.get(location_id)
    restraint = state.get("restraint")
    resources = state.get("resources")
    agendas = sorted(
        (node for node in world.nodes_by_id.values()
         if str(node.node_type) == "agenda" and node.is_active and node.profile.get("owner_ref") == actor_id
         and world.get_state(node.id).get("status") == "active"),
        key=lambda node: (-_agenda_priority(world.get_state(node.id).get("priority")), node.id),
    )[:MAX_ACTOR_AGENDAS]
    pending_intents = _pending_intents(runtime, actor_id)
    holdings = sorted(
        (node for node in world.nodes_by_id.values()
         if str(node.node_type) in {"item", "resource"} and world.get_state(node.id).get("holder_id") == actor_id),
        key=lambda node: node.id,
    )
    facts, public_facts = _known_facts(runtime, _cognition(runtime, actor))
    clocks = _known_clocks(runtime, actor_id, now)
    named = {
        *(ref for fact in [*facts, *public_facts] for ref in fact.get("related_refs", ())),
        *(ref for node in agendas for key in ("means_refs", "secrecy_refs")
          for ref in _strings(world.get_state(node.id).get(key))),
        *(ref for intent in pending_intents for ref in [*intent["target_refs"], intent["location_id"]]),
        # An insider clock's on_expire may name a node; free text matches none and is dropped below.
        *(ref for clock in clocks for ref in (clock.get("owner_ref"), clock.get("on_expire"))
          if isinstance(ref, str)),
        *([restraint["by_ref"]] if isinstance(restraint, dict) and isinstance(restraint.get("by_ref"), str) else []),
    } - {actor_id, "", *(item.id for item in [*agendas, *holdings]), *(item["id"] for item in pending_intents)}
    return ActorView(
        actor_id=actor_id,
        name=actor.name,
        node_type=str(actor.node_type),
        actor_tier=str(state.get("actor_tier") or ""),
        location_id=location_id,
        location_name=location.name if location is not None else "",
        resources=resources if isinstance(resources, list) else [resources] if resources else [],
        restraint=restraint if isinstance(restraint, dict) and restraint.get("state", "free") != "free" else {},
        holdings=[_summary(node) for node in holdings],
        agendas=[_agenda(runtime, node) for node in agendas],
        pending_intents=pending_intents,
        facts=facts,
        public_facts=public_facts,
        entities=[entry for ref in sorted(named) if (entry := _entity(runtime, ref)) is not None],
        reachable_places=_reachable_places(runtime, lexicon, actor_id, location_id),
        rules=[
            {"id": node.id, "hardness": node.profile.get("hardness"), "statement": node.profile.get("statement")}
            for node in sorted(world.nodes_by_id.values(), key=lambda node: node.id)
            if str(node.node_type) == "rule" and node.is_active
        ],
        clocks=clocks,
        boundary_refs=sorted(set(boundary_refs)),
        vetoes=_recent_vetoes(runtime, actor_id, vetoes),
    )


def _agenda_priority(value: Any) -> int:
    return value if type(value) is int else _DEFAULT_AGENDA_PRIORITY


def _strings(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str) and item] if isinstance(value, list) else []


def _summary(node: WorldNode | MapNode) -> dict[str, Any]:
    return {"id": node.id, "node_type": str(node.node_type), "name": node.name}


def _entity(runtime: BookStateRuntime, ref: str) -> dict[str, Any] | None:
    node = runtime.world.get_node(ref) or runtime.map.nodes_by_id.get(ref)
    return _summary(node) if node is not None else None


def _agenda(runtime: BookStateRuntime, node: WorldNode) -> dict[str, Any]:
    state = runtime.world.get_state(node.id)
    return {
        "id": node.id,
        **{key: node.profile.get(key) for key in ("goal", "motivation", "horizon")},
        **{key: state.get(key) for key in ("status", "priority", "means_refs", "constraints", "secrecy_refs")},
    }


def _pending_intents(runtime: BookStateRuntime, actor_id: str) -> list[dict[str, Any]]:
    """Its own planned, due and held intents, soonest first, so a turn need not propose them again."""
    world = runtime.world
    pending: list[tuple[int | float, str, dict]] = []
    for node in world.nodes_by_id.values():
        if str(node.node_type) != "intent" or node.profile.get("owner_ref") != actor_id:
            continue
        state = world.get_state(node.id)
        if state.get("status") not in _PENDING_INTENT_STATUSES:
            continue
        scheduled_at = state.get("scheduled_at")
        scheduled_at = scheduled_at if type(scheduled_at) is int else None
        pending.append((scheduled_at if scheduled_at is not None else math.inf, node.id, {
            "id": node.id,
            "summary": str(node.profile.get("summary") or node.summary or node.name),
            "action_type": node.profile.get("action_type"),
            "location_id": str(node.profile.get("location_id") or ""),
            "scheduled_at": scheduled_at,
            "status": state.get("status"),
            "target_refs": _strings(node.profile.get("target_refs")),
        }))
    return [entry for *_key, entry in sorted(pending, key=lambda item: item[:2])[:MAX_PENDING_INTENTS]]


def _cognition(runtime: BookStateRuntime, actor: WorldNode) -> CognitionView:
    key = (OBSERVER_TYPES[str(actor.node_type)], actor.id)
    view = runtime.cognition_by_observer.get(key)
    if view is not None:
        return view
    # No overlay yet: the actor knows exactly the public facts.
    return CognitionView(CognitionOverlay(
        id="", project_id=runtime.project_id, observer_type=key[0], observer_id=actor.id,
    ))


def _known_facts(runtime: BookStateRuntime, cognition: CognitionView) -> tuple[list[dict], list[dict]]:
    """Facts with a recorded held state, and static public facts known by default, each list newest first.

    Recorded non-event facts (seed and static knowledge) come before event news, so news never
    crowds them out of the cap. A false belief reads as held. An unrecorded event fact stays
    unknown: its news arrives through propagation, which records the knowledge.
    """
    known = set(runtime.world.nodes_by_id) | set(runtime.map.nodes_by_id)
    recorded: list[tuple[bool, int, str, dict]] = []
    public: list[tuple[int, str, dict]] = []
    for fact in runtime.world.facts_by_id.values():
        ref = f"{FACT_REF_PREFIX}{fact.id}"
        if ref not in cognition.knowledge_by_ref and fact.fact_type == EVENT_FACT_TYPE:
            continue
        state = cognition.knowledge_state(fact)
        if state not in _HELD_STATES:
            continue
        entry: dict[str, Any] = {"ref": ref, "proposition": fact.proposition}
        related = [item for item in dict.fromkeys(fact.related_node_refs) if item in known and item != "reader"]
        if related:
            entry["related_refs"] = related
        if ref in cognition.knowledge_by_ref:
            is_event = fact.fact_type == EVENT_FACT_TYPE
            recorded.append((is_event, -fact.created_at_chapter, ref, {**entry, "state": state}))
        else:
            public.append((-fact.created_at_chapter, ref, entry))
    return (
        [entry for *_key, entry in sorted(recorded, key=lambda item: item[:3])[:MAX_KNOWN_FACTS]],
        [entry for *_key, entry in sorted(public, key=lambda item: item[:2])[:MAX_PUBLIC_FACTS]],
    )


def _reachable_places(
    runtime: BookStateRuntime, lexicon: BookLexicon, actor_id: str, location_id: str,
) -> list[dict[str, Any]]:
    if location_id not in runtime.map.nodes_by_id:
        return []
    estimates = estimate_travel_from(runtime, lexicon, mover_id=actor_id, from_location_id=location_id)
    found: list[tuple[float, str, str]] = []
    for node in runtime.map.nodes_by_id.values():
        if node.id == location_id:
            continue
        estimate = estimates[node.id]
        # minutes is None when only an objective path exists, not one the actor knows.
        if estimate.minutes is not None:
            found.append((estimate.minutes, node.id, node.name))
    return [
        {"location_id": ref, "name": name, "minutes": math.ceil(minutes - MINUTE_EPSILON)}
        for minutes, ref, name in sorted(found)[:MAX_REACHABLE_PLACES]
    ]


def _known_clocks(runtime: BookStateRuntime, actor_id: str, now: int) -> list[dict[str, Any]]:
    """Running public clocks, and secret ones it owns or shares through the owner's agenda secrecy_refs."""
    world = runtime.world
    confidants: dict[str, set[str]] = {}
    for node in world.nodes_by_id.values():
        if str(node.node_type) == "agenda":
            owner = str(node.profile.get("owner_ref") or "")
            confidants.setdefault(owner, set()).update(_strings(world.get_state(node.id).get("secrecy_refs")))
    clocks: list[tuple[int | float, str, dict]] = []
    for node in world.nodes_by_id.values():
        if str(node.node_type) != "clock":
            continue
        state = world.get_state(node.id)
        if state.get("status") != "running":
            continue
        owner = str(node.profile.get("owner_ref") or "")
        insider = bool(owner) and (owner == actor_id or actor_id in confidants.get(owner, ()))
        # A clock without a declared scope is secret.
        public = node.profile.get("scope") == "public"
        if not (public or insider):
            continue
        due_at = state.get("due_at")
        due_at = due_at if type(due_at) is int else None
        entry: dict[str, Any] = {
            "id": node.id, "label": str(node.profile.get("label") or node.name),
            "scope": "public" if public else "secret", "due_at": due_at,
            "remaining_minutes": due_at - now if due_at is not None else None,
        }
        # Only insiders know whose clock it is and what follows its expiry.
        if insider:
            entry.update(owner_ref=owner, on_expire=node.profile.get("on_expire"))
        clocks.append((due_at if due_at is not None else math.inf, node.id, entry))
    return [entry for *_key, entry in sorted(clocks, key=lambda item: item[:2])]


def recent_vetoes(
    session: Session, project_id: str, *, actor_ids: Sequence[str],
) -> dict[str, list[WorldPlanConflictRow]]:
    """Each actor's newest MAX_RECENT_VETOES resolved veto_world conflicts, newest first.

    Read per actor in SQL, so a tick never loads the book's whole conflict history. A veto a historical
    rewrite superseded is left out.
    """
    return {
        actor_id: list(session.scalars(
            select(WorldPlanConflictRow)
            .where(
                WorldPlanConflictRow.project_id == project_id,
                WorldPlanConflictRow.actor_ref == actor_id,
                WorldPlanConflictRow.status == "resolved",
                WorldPlanConflictRow.decision_option == _VETO_OPTION,
                WorldPlanConflictRow.superseded_by_delta.is_(None),
            )
            .order_by(
                WorldPlanConflictRow.story_time.desc(), WorldPlanConflictRow.detected_after_chapter.desc(),
                WorldPlanConflictRow.id.desc(),
            )
            .limit(MAX_RECENT_VETOES)
        ))
        for actor_id in actor_ids
    }


def _recent_vetoes(
    runtime: BookStateRuntime, actor_id: str, vetoes: Sequence[WorldPlanConflictRow],
) -> list[dict[str, Any]]:
    """Resolved conflicts decided ``veto_world`` for this actor, newest first."""
    rows = [
        row for row in vetoes
        if row.status == "resolved" and row.actor_ref == actor_id
        and row.decision_option == _VETO_OPTION and not row.superseded_by_delta
    ]
    rows.sort(key=lambda row: (row.story_time, row.detected_after_chapter, row.id), reverse=True)
    return [
        {"summary": row.summary, "intent_summaries": _vetoed_intent_summaries(runtime, row)}
        for row in rows[:MAX_RECENT_VETOES]
    ]


def _vetoed_intent_summaries(runtime: BookStateRuntime, row: WorldPlanConflictRow) -> list[str]:
    """The vetoed intent's summary, or those of the new intents its held candidate proposed."""
    intent = runtime.world.get_node(row.intent_ref) if row.intent_ref else None
    if intent is not None:
        summary = str(intent.profile.get("summary") or intent.summary or intent.name)
        return [summary] if summary else []
    proposed = json.loads(row.proposed_change_json or "{}")
    intents = proposed.get("new_intents") if isinstance(proposed, dict) else None
    return [
        str(item["summary"]) for item in intents or []
        if isinstance(item, dict) and isinstance(item.get("summary"), str) and item["summary"]
    ]


# turns


def _calendar(calendar: StoryCalendar | None) -> StoryCalendar:
    # Without a calendar only the lexicon's aliases name units, as in the tick builder.
    return calendar if calendar is not None else StoryCalendar("", 0, ())


def _minutes(wait: Duration, calendar: StoryCalendar | None, lexicon: BookLexicon) -> int:
    """A wait in the book's units; a negative wait counts as none, a non-finite one is invalid."""
    if not math.isfinite(wait.amount):
        raise StoryClockError("Duration amount must be finite")
    return duration_to_minutes(_calendar(calendar), lexicon, amount=max(wait.amount, 0.0), unit_name=wait.unit)


def _turn_issues(
    turn: ActorTurnResult, view: ActorView, *, policy: WorldPolicy, calendar: StoryCalendar | None,
    lexicon: BookLexicon,
) -> list[str]:
    """Everything the tick would reject in a turn: caps, units, refs outside the view, agenda rules."""
    issues: list[str] = []
    refs, places = view.references(), view.place_ids()
    agendas = {agenda["id"] for agenda in view.agendas}
    modes = {mode.id for mode in lexicon.travel_modes}
    cap = policy.max_new_intents_per_actor
    if len(turn.new_intents) > cap:
        issues.append(f"too_many_intents: {len(turn.new_intents)} new_intents, at most {cap} allowed")
    if len(turn.agenda_changes) > MAX_AGENDA_CHANGES_PER_TURN:
        issues.append(
            f"too_many_agenda_changes: {len(turn.agenda_changes)} agenda_changes, "
            f"at most {MAX_AGENDA_CHANGES_PER_TURN} allowed"
        )

    def duration(wait: Duration, path: str) -> None:
        try:
            _minutes(wait, calendar, lexicon)
        except StoryClockError as exc:
            issues.append(f"invalid_duration at {path}: {exc}")

    def check(ref: str, path: str, allowed: set[str]) -> None:
        if ref not in allowed:
            issues.append(f"unknown_ref at {path}: {ref!r}")

    new_agendas = sum(change.agenda_ref == NEW_AGENDA_REF for change in turn.agenda_changes)
    for index, change in enumerate(turn.agenda_changes):
        path = f"agenda_changes[{index}]"
        if change.agenda_ref != NEW_AGENDA_REF:
            check(change.agenda_ref, f"{path}.agenda_ref", agendas)
        if change.priority is not None and not AGENDA_PRIORITY_MIN <= change.priority <= AGENDA_PRIORITY_MAX:
            issues.append(
                f"invalid_priority at {path}.priority: {change.priority}, "
                f"use {AGENDA_PRIORITY_MIN} to {AGENDA_PRIORITY_MAX}"
            )
    for index, intent in enumerate(turn.new_intents):
        path = f"new_intents[{index}]"
        if intent.agenda_ref == NEW_AGENDA_REF:
            # The tick builder resolves it to the turn's one new agenda.
            if new_agendas != 1:
                issues.append(
                    f"invalid_agenda_ref at {path}.agenda_ref: {NEW_AGENDA_REF!r} needs exactly one new agenda "
                    f"in agenda_changes, found {new_agendas}"
                )
        elif intent.agenda_ref:
            check(intent.agenda_ref, f"{path}.agenda_ref", agendas)
        for target_index, ref in enumerate(intent.target_refs):
            check(ref, f"{path}.target_refs[{target_index}]", refs)
        if intent.location_id:
            check(intent.location_id, f"{path}.location_id", places)
        duration(intent.scheduled_in, f"{path}.scheduled_in")
        issues.extend(
            f"{path}.expected_effects: {issue}" for issue in scoped_effect_reference_issues(
                intent.expected_effects, refs=refs, location_ids=places, travel_mode_ids=modes,
            )
        )
    duration(turn.next_turn_in, "next_turn_in")
    return issues


class ActorTurnRunner:
    def __init__(
        self, llm_client, *, max_attempts: int = 2, max_tokens_per_actor: int = 6000, max_tokens: int = 16000,
    ):
        self.llm_client = llm_client
        self.max_attempts = max(1, int(max_attempts))
        # A call's budget grows with the actors it asks, up to max_tokens.
        self.max_tokens_per_actor = max_tokens_per_actor
        self.max_tokens = max_tokens
        self._chat_parameters = inspect.signature(llm_client.chat).parameters

    def run(
        self, *, context: TickContext, views: Sequence[ActorView], policy: WorldPolicy,
        calendar: StoryCalendar | None, lexicon: BookLexicon,
        feedback: Mapping[str, Sequence[ConstraintIssue]] | None = None,
    ) -> list[ActorTurnCall]:
        """One call per batch of ``policy.actor_turn_batch_size`` views, one ActorTurnCall per view in order.

        A turn that breaks a rule is retried with its errors; after ``max_attempts`` it has no
        result, and its actor's ``next_turn_at`` stays as it was.
        """
        if len({view.actor_id for view in views}) != len(views):
            raise ValueError("views repeat an actor")
        size = policy.actor_turn_batch_size
        calls: list[ActorTurnCall] = []
        for start in range(0, len(views), size):
            calls.extend(self._run_batch(
                context=context, views=views[start:start + size], batch_index=start // size, policy=policy,
                calendar=calendar, lexicon=lexicon, feedback=feedback,
            ))
        return calls

    def _run_batch(
        self, *, context: TickContext, views: Sequence[ActorView], batch_index: int, policy: WorldPolicy,
        calendar: StoryCalendar | None, lexicon: BookLexicon,
        feedback: Mapping[str, Sequence[ConstraintIssue]] | None,
    ) -> list[ActorTurnCall]:
        by_actor = {view.actor_id: view for view in views}
        actor_ids = list(by_actor)
        pending = list(actor_ids)
        results: dict[str, ActorTurnResult] = {}
        issues: dict[str, list[str]] = {actor_id: [] for actor_id in actor_ids}
        attempts: list[dict[str, Any]] = []
        business_retries: list[dict[str, Any]] = []
        retry_feedback: list[str] = []
        # The last prompt sent, which the trace records. Only when nothing was sent does the trace record the
        # unsent prompt, with no attempts and failure_reason prompt_too_large.
        messages: list[dict] = []
        too_large = False
        # Whether any call returned an answer, valid or not.
        answered = False
        for attempt_no in range(1, self.max_attempts + 1):
            # A retry asks again only for the actors still without a valid turn.
            prompt = build_actor_turn_messages(
                context=context, views=[by_actor[actor_id] for actor_id in pending], calendar=calendar,
                lexicon=lexicon, max_new_intents=policy.max_new_intents_per_actor,
                max_agenda_changes=MAX_AGENDA_CHANGES_PER_TURN, feedback=feedback, retry_feedback=retry_feedback,
            )
            if len(prompt[-1]["content"]) > MAX_ACTOR_TURN_PROMPT_CHARS:
                too_large = True
                for actor_id in pending:
                    issues[actor_id].append("prompt_too_large")
                messages = messages or prompt
                break
            messages = prompt
            retry_feedback = []
            raw = ""
            call_error: Exception | None = None
            failure_reason = ""
            try:
                raw = self._call_chat(messages, actor_count=len(pending))
                answered = True
                try:
                    parsed = ActorTurnBatch.model_validate(parse_llm_json(raw, error_prefix=_PARSER_NAME))
                except Exception as exc:  # noqa: BLE001
                    retry_feedback = [str(exc)]
                    raise
            except Exception as exc:  # noqa: BLE001
                call_error = exc
                # A transport error's message never reaches the model or the trace.
                failure_reason = "\n".join(retry_feedback) if retry_feedback else type(exc).__name__
            finally:
                attempts.extend(drain_workflow_attempts(
                    self.llm_client, attempt_no=attempt_no, stage_key=_STAGE_KEY, task_family="world_engine",
                    messages=messages, raw=raw, call_error=call_error, error_message=failure_reason,
                    replace_error_message=not retry_feedback,
                    parse_error=call_error if retry_feedback else None,
                    parser_name=_PARSER_NAME, schema_name="ActorTurnBatch",
                ))

            if call_error is None:
                seen: set[str] = set()
                for turn in parsed.turns:
                    if turn.actor_id not in pending:
                        continue
                    if turn.actor_id in seen:
                        issues[turn.actor_id].append(f"duplicate turn for actor_id {turn.actor_id!r}")
                        continue
                    seen.add(turn.actor_id)
                    issues[turn.actor_id] = _turn_issues(
                        turn, by_actor[turn.actor_id], policy=policy, calendar=calendar, lexicon=lexicon,
                    )
                    if not issues[turn.actor_id]:
                        results[turn.actor_id] = turn
                for actor_id in pending:
                    if actor_id not in seen:
                        issues[actor_id] = [f"missing turn for actor_id {actor_id!r}"]
                pending = [actor_id for actor_id in pending if actor_id not in results]
                retry_feedback = [f"{actor_id}: {issue}" for actor_id in pending for issue in issues[actor_id]]

            if pending and attempt_no < self.max_attempts:
                business_retries.append({
                    "stage": _STAGE_KEY, "attempt_no": attempt_no, "max_attempts": self.max_attempts,
                    "reason_class": type(call_error).__name__ if call_error else "ActorTurnValidation",
                    "reason": ("\n".join(retry_feedback) or failure_reason)[:500], "retry_kind": "business_validation",
                })
                continue
            if call_error is not None:
                for actor_id in pending:
                    issues[actor_id].append(f"Actor turn failed after {attempt_no} attempts: {failure_reason}")
            break

        prompt_trace = self._build_prompt_trace(
            messages=messages, context=context, actor_ids=actor_ids, batch_index=batch_index, attempts=attempts,
            business_retries=business_retries, results=results, issues=issues,
            failure_reason="prompt_too_large" if too_large else "",
        )
        failure_reason = PROMPT_TOO_LARGE if too_large else INVALID_OUTPUT if answered else LLM_UNAVAILABLE
        return [
            ActorTurnCall(
                actor_id, results.get(actor_id), prompt_trace, issues[actor_id],
                "" if actor_id in results else failure_reason,
            )
            for actor_id in actor_ids
        ]

    def _call_chat(self, messages: list[dict], *, actor_count: int) -> str:
        kwargs: dict[str, Any] = {
            "temperature": 0.5, "max_tokens": min(self.max_tokens_per_actor * actor_count, self.max_tokens),
            "response_format": {"type": "json_object"}, "task_family": "world_engine",
            "stage_key": _STAGE_KEY, "output_schema": ActorTurnBatch.model_json_schema(),
        }
        if not any(param.kind == inspect.Parameter.VAR_KEYWORD for param in self._chat_parameters.values()):
            kwargs = {key: value for key, value in kwargs.items() if key in self._chat_parameters}
        return self.llm_client.chat(messages, **kwargs)

    def _build_prompt_trace(
        self, *, messages: list[dict], context: TickContext, actor_ids: list[str], batch_index: int,
        attempts: list[dict[str, Any]], business_retries: list[dict[str, Any]],
        results: Mapping[str, ActorTurnResult], issues: Mapping[str, list[str]], failure_reason: str,
    ) -> dict[str, Any]:
        missing = [actor_id for actor_id in actor_ids if actor_id not in results]
        return build_workflow_trace(
            self.llm_client, messages=messages, attempts=attempts,
            trace_scope="world_engine",
            stage_key=_STAGE_KEY,
            template_id="world_engine:actor_turn",
            input_snapshot={**context.model_dump(mode="json"), "actor_ids": actor_ids, "batch_index": batch_index,
                               "stage_key": _STAGE_KEY, "selected_skills": []},
            output_summary={
                "actor_turn_ok": not missing,
                "issues": [f"{actor_id}: {issue}" for actor_id in actor_ids for issue in issues[actor_id]],
                "missing_actor_ids": missing, "failure_reason": failure_reason,
                "turn_count": len(results), "skill_summary": [], "business_retry_events": business_retries,
            },
        )


# candidates


def turn_to_candidate(
    call: ActorTurnCall, *, now: int, calendar: StoryCalendar | None, lexicon: BookLexicon,
) -> TickCandidate:
    """The turn as a tick candidate; the tick builder schedules each new intent from ``scheduled_in``."""
    if call.result is None:
        raise ValueError(f"actor {call.actor_id!r} has no turn result")
    result = call.result
    return TickCandidate(
        candidate_id=f"actor_turn:{call.actor_id}",
        origin="actor_turn",
        actor_id=call.actor_id,
        summary="\n".join(text for intent in result.new_intents if (text := intent.summary.strip())),
        effects=WorldEffects(),
        new_intents=list(result.new_intents),
        agenda_changes=list(result.agenda_changes),
        # The next turn comes after this one even for a zero or negative wait.
        next_turn_at={call.actor_id: max(now + _minutes(result.next_turn_in, calendar, lexicon), now + 1)},
    )


__all__ = [
    "MAX_ACTOR_AGENDAS",
    "MAX_AGENDA_CHANGES_PER_TURN",
    "MAX_REACHABLE_PLACES",
    "MAX_RECENT_VETOES",
    "ActorTurnCall",
    "ActorTurnRunner",
    "ActorView",
    "build_actor_view",
    "rank_actors",
    "recent_vetoes",
    "turn_to_candidate",
]
