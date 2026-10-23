"""Deterministic POV world view of a planned chapter, the writer's world context.

The view never carries the truth of a secret that the POV does not hold, the reader may not
learn in this chapter and no reveal directive of the chapter permits: such a secret appears
at most as a forbidden topic named by its subjects, and only when it concerns the chapter.
Author-only nodes (agendas, intents, events, secrets, clocks) never appear by name.
"""

from __future__ import annotations

import copy
import json
import logging
import math
from collections import defaultdict
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy.orm import Session

from forwin.book_state.book_frame_repository import BookLexiconRepository, StoryCalendarRepository
from forwin.book_state.cognition import CognitionView
from forwin.book_state.knowledge import FACT_REF_PREFIX, HIDDEN_SENSITIVITY
from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.map_graph import MapGraph
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.query import BookStateQuery
from forwin.book_state.reveal_schedule import earliest_reveal_chapters
from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph, resolve_world_node_location_id
from forwin.book_state.schema import ACTOR_NODE_TYPES
from forwin.book_state.story_clock import StoryCalendar, format_story_time
from forwin.map.visibility import is_writer_visible_map_edge
from forwin.models.project import ChapterPlan
from forwin.protocol.book_state import FactNode, ObserverType, WorldNode
from forwin.world_engine.travel import estimate_travel
from forwin.writer.prompt_core.world_view import forbidden_topic_text

from .types import (
    AllowedReveal, ChapterWorldView, ForbiddenTopic, KnownFact, OnstageEntity, ViewClock, ViewHint, ViewLocation,
)


logger = logging.getLogger(__name__)

VIEW_NEARBY_LOCATIONS = 8
_READER = ObserverType.READER.value
# States in which an observer holds a fact; disbelieving or being misled is not holding it.
_HELD_STATES = frozenset({"heard", "suspects", "believes", "knows"})
# Recorded reader states that settle a secret for the reader.
_READER_KNOWN_STATES = frozenset({"believes", "knows"})
# Node types whose names may stand for a secret's subjects; abstract nodes such as events or
# intents may be named after the secret itself.
_TOPIC_SUBJECT_TYPES = frozenset({*ACTOR_NODE_TYPES, "family", "institution", "item", "location", "region"})
_HELD_NODE_TYPES = frozenset({"item", "resource"})
# Node types a scene can show; other nodes carry author-only names such as an intent's summary.
_ONSTAGE_TYPES = frozenset({*ACTOR_NODE_TYPES, *_HELD_NODE_TYPES})
# The POV's node type, and so its cognition overlay's observer type.
_POV_TYPE = "character"
# Travel minutes absorb float noise before rounding up.
_MINUTE_EPSILON = 1e-6


def build_chapter_world_view(
    session: Session,
    *,
    project_id: str,
    chapter_plan: ChapterPlan,
    budget_chars: int,
    runtime: BookStateRuntime | None = None,
) -> ChapterWorldView:
    """The POV's view of canon as it stands before the planned chapter.

    allowed_reveals, hints, forbidden_topics and pov_location are never trimmed. The rest is
    trimmed in the order reader_known, locations (down to the chapter's), pov_knowledge (down
    to secrets and false beliefs), onstage held_items, until ``render_view_text`` fits
    ``budget_chars``. Without ``runtime`` only the POV's and the reader's cognition overlays load.
    """
    if budget_chars < 0:
        raise ValueError("budget_chars must be nonnegative")
    chapter_number = int(chapter_plan.chapter_number)
    as_of_chapter = max(chapter_number - 1, 0)
    pov_ref = str(chapter_plan.pov_ref or "")
    if runtime is None:
        runtime = BookStateProjection(session).load_runtime_as_of(
            project_id, as_of_chapter=as_of_chapter,
            observer_keys=[(_READER, _READER), *([(_POV_TYPE, pov_ref)] if pov_ref else [])],
        )
    calendar = StoryCalendarRepository(session).get(project_id)
    now = BookStateQuery(session).current_story_minute(project_id, as_of_chapter=as_of_chapter)
    if now is None:
        now = calendar.opening_minute if calendar is not None else 0
    world = runtime.world
    pov = world.get_node(pov_ref) if pov_ref else None
    if pov_ref and (pov is None or str(pov.node_type) != _POV_TYPE):
        # Safe defaults: no POV knowledge, location or insider clocks.
        logger.warning("Chapter %s of project %s plans POV %r, which is not a character; the view has no POV",
                       chapter_number, project_id, pov_ref)
        pov_ref, pov = "", None
    location_refs = _refs(chapter_plan.location_refs_json)
    held = _holdings(world)
    onstage = _onstage(runtime, _refs(chapter_plan.onstage_refs_json), pov_ref=pov_ref, held=held,
                       chapter_number=chapter_number)
    relevant_refs = {pov_ref, *(entity.ref for entity in onstage), *location_refs} - {""}

    allowed = _allowed_reveals(world, _directives(chapter_plan.reveal_directives_json), chapter_number)
    hint_directives = _directives(chapter_plan.hint_directives_json)
    knowledge, reader_known, forbidden, secret_refs = _fact_parts(
        runtime,
        pov_ref=pov_ref,
        chapter_number=chapter_number,
        schedule=earliest_reveal_chapters(session, project_id),
        permitted={reveal.fact_ref for reveal in allowed},
        reader_permitted={reveal.fact_ref for reveal in allowed if reveal.observer == _READER},
        relevant_refs=relevant_refs,
        topic_refs=relevant_refs | {item.id for entity in onstage for item in held.get(entity.ref, ())},
        hinted_refs={
            f"{FACT_REF_PREFIX}{fact.id}" for item in hint_directives
            if (fact := _fact_of(world.facts_by_id, item.get("fact_ref"))) is not None
        },
    )
    pov_location, locations = _locations(
        runtime, BookLexiconRepository(session).get(project_id), pov_ref=pov_ref, location_refs=location_refs,
    )
    span = chapter_plan.expected_span_minutes
    view = ChapterWorldView(
        pov_ref=pov_ref,
        pov_name=pov.name if pov is not None else "",
        onstage=onstage,
        pov_knowledge=knowledge,
        reader_known=reader_known,
        allowed_reveals=allowed,
        hints=_hints(world.facts_by_id, hint_directives, chapter_number),
        forbidden_topics=forbidden,
        locations=locations,
        pov_location=pov_location,
        now_label=format_story_time(calendar if calendar is not None else StoryCalendar("", 0, ()), now),
        expected_span_label=_duration_label(calendar, span) if type(span) is int and span >= 0 else "",
        pov_clocks=_pov_clocks(runtime, pov_ref=pov_ref, now=now, calendar=calendar),
    )
    if not _trim(view, budget_chars, chapter_location_ids=set(location_refs), secret_refs=secret_refs):
        logger.warning(
            "World view of chapter %s of project %s exceeds its budget of %s chars after trimming",
            chapter_number, project_id, budget_chars,
        )
    return view


# plan fields


def _plan_items(raw: str | None) -> list[Any]:
    try:
        value = json.loads(raw or "[]")
    except ValueError as exc:
        logger.warning("Chapter plan list field is not JSON: %s", exc)
        return []
    return value if isinstance(value, list) else []


def _refs(raw: str | None) -> list[str]:
    return list(dict.fromkeys(item for item in _plan_items(raw) if isinstance(item, str) and item))


def _directives(raw: str | None) -> list[Mapping[str, Any]]:
    return [item for item in _plan_items(raw) if isinstance(item, dict)]


def _fact_of(facts_by_id: Mapping[str, FactNode], ref: Any) -> FactNode | None:
    return facts_by_id.get(str(ref or "").strip().removeprefix(FACT_REF_PREFIX))


def _allowed_reveals(
    world: ObjectiveWorldGraph, directives: list[Mapping[str, Any]], chapter_number: int,
) -> list[AllowedReveal]:
    """Directives with an observer and a known fact; the observer's name only for an actor."""
    allowed: list[AllowedReveal] = []
    for item in directives:
        observer = str(item.get("observer") or "").strip()
        if not observer:
            logger.warning("Chapter %s reveal directive %r has no observer", chapter_number, item.get("id"))
            continue
        fact = _fact_of(world.facts_by_id, item.get("fact_ref"))
        if fact is None:
            logger.warning("Chapter %s reveal directive %r names an unknown fact %r",
                           chapter_number, item.get("id"), item.get("fact_ref"))
            continue
        node = world.get_node(observer)
        allowed.append(AllowedReveal(
            directive_id=str(item.get("id") or ""), fact_ref=f"{FACT_REF_PREFIX}{fact.id}",
            proposition=fact.proposition, observer=observer,
            observer_name=node.name if node is not None and str(node.node_type) in ACTOR_NODE_TYPES else "",
            target_state=str(item.get("target_state") or ""),
        ))
    return allowed


def _hints(
    facts_by_id: Mapping[str, FactNode], directives: list[Mapping[str, Any]], chapter_number: int,
) -> list[ViewHint]:
    """Hint texts only; a hint that states its fact's proposition is dropped."""
    hints: list[ViewHint] = []
    for item in directives:
        text = str(item.get("hint_text") or "")
        if not text.strip():
            continue
        fact = _fact_of(facts_by_id, item.get("fact_ref"))
        truth = " ".join(fact.proposition.split()) if fact is not None else ""
        if truth and truth in " ".join(text.split()):
            logger.warning("Chapter %s hint directive %r states its fact's proposition", chapter_number, item.get("id"))
            continue
        hints.append(ViewHint(directive_id=str(item.get("id") or ""), hint_text=text))
    return hints


# facts


def _observer_key(runtime: BookStateRuntime, ref: str) -> tuple[str, str] | None:
    """The cognition overlay key of a world node; node ids are unique across observer types."""
    if not ref:
        return None
    return next((key for key in runtime.cognition_by_observer if key[1] == ref and key[0] != _READER), None)


def _fact_parts(
    runtime: BookStateRuntime,
    *,
    pov_ref: str,
    chapter_number: int,
    schedule: Mapping[str, int],
    permitted: set[str],
    reader_permitted: set[str],
    relevant_refs: set[str],
    topic_refs: set[str],
    hinted_refs: set[str],
) -> tuple[list[KnownFact], list[str], list[ForbiddenTopic], set[str]]:
    """POV knowledge, reader knowledge and forbidden topics, newest fact first, and the secret refs.

    A secret is a true fact that is hidden or has a reveal target. Only recorded states count,
    for the POV and for the reader alike: an unrecorded public fact is not known by default.
    A forbidden topic needs a chapter-relevant secret: one whose subjects meet ``topic_refs``
    or that a hint directive names. Only a hinted secret without nameable subjects falls back
    to its fact id.
    """
    key = _observer_key(runtime, pov_ref)
    pov_view = runtime.cognition_by_observer[key] if key is not None else None
    pov_states = pov_view.knowledge_by_ref if pov_view is not None else {}
    false_refs = set(pov_view.false_beliefs(runtime.world.facts_by_id)) if pov_view is not None else set()
    reader = runtime.cognition_by_observer.get((_READER, _READER))
    reader_states = reader.knowledge_by_ref if reader is not None else {}
    knowledge: list[KnownFact] = []
    reader_known: list[str] = []
    forbidden: list[ForbiddenTopic] = []
    secret_refs: set[str] = set()
    for fact in sorted(runtime.world.facts_by_id.values(), key=lambda item: (-item.created_at_chapter, item.id)):
        ref = f"{FACT_REF_PREFIX}{fact.id}"
        secret = fact.truth_value != "false" and (
            fact.sensitivity_level in HIDDEN_SENSITIVITY or fact.id in schedule
        )
        state = pov_states.get(ref, "")
        pov_holds = state in _HELD_STATES
        reader_knows = reader_states.get(ref) in _READER_KNOWN_STATES
        reveal_due = schedule.get(fact.id, math.inf) <= chapter_number
        if secret:
            secret_refs.add(ref)
        if pov_holds:
            knowledge.append(KnownFact(
                fact_ref=ref, proposition=fact.proposition, state=state,
                pov_believes_false=ref in false_refs,
                pov_only_secret=secret and not (reader_knows or reveal_due or ref in reader_permitted),
            ))
        if reader_knows and relevant_refs.intersection(fact.related_node_refs):
            reader_known.append(f"{ref}: {fact.proposition}")
        hinted = ref in hinted_refs
        if (secret and not (pov_holds or reader_knows or reveal_due or ref in permitted)
                and (hinted or topic_refs.intersection(fact.related_node_refs))):
            names = _subject_names(runtime, fact)
            if names or hinted:
                forbidden.append(ForbiddenTopic(fact_ref=ref, topic=forbidden_topic_text(names or [fact.id])))
    return knowledge, reader_known, forbidden, secret_refs


def _subject_names(runtime: BookStateRuntime, fact: FactNode) -> list[str]:
    """Names of the fact's subjects that a scene could show: no abstract or concealed nodes."""
    names: list[str] = []
    for ref in fact.related_node_refs:
        node = runtime.world.get_node(ref)
        place = runtime.map.nodes_by_id.get(ref)
        if node is not None:
            if str(node.node_type) in _TOPIC_SUBJECT_TYPES and not _concealed(runtime.world, node):
                names.append(node.name.strip())
        elif place is not None:
            names.append(place.name.strip())
    return list(dict.fromkeys(name for name in names if name))


# onstage, map and time


def _holdings(world: ObjectiveWorldGraph) -> dict[str, list[WorldNode]]:
    """Active items and resources by holder, in id order."""
    held: dict[str, list[WorldNode]] = defaultdict(list)
    for node in sorted(world.nodes_by_id.values(), key=lambda item: item.id):
        holder = world.get_state(node.id).get("holder_id")
        if str(node.node_type) in _HELD_NODE_TYPES and node.is_active and isinstance(holder, str) and holder:
            held[holder].append(node)
    return held


def _onstage(
    runtime: BookStateRuntime, refs: list[str], *, pov_ref: str, held: Mapping[str, list[WorldNode]],
    chapter_number: int,
) -> list[OnstageEntity]:
    world = runtime.world
    entities: list[OnstageEntity] = []
    for ref in refs:
        node = world.get_node(ref)
        if node is None or str(node.node_type) not in _ONSTAGE_TYPES:
            logger.warning("Chapter %s onstage ref %r is not a perceptible world node; dropped", chapter_number, ref)
            continue
        state = world.get_state(ref)
        restraint = state.get("restraint")
        restraint = restraint.get("state") if isinstance(restraint, dict) else restraint
        location = runtime.map.nodes_by_id.get(resolve_world_node_location_id(world, ref))
        entities.append(OnstageEntity(
            ref=ref,
            name=node.name,
            location_name=location.name if location is not None else "",
            alive_status=_text(state.get("alive_status")),
            restraint="" if restraint in (None, "", "free") else _text(restraint),
            health=_text(state.get("health")),
            held_items=[
                item.name or item.id for item in held.get(ref, ()) if ref == pov_ref or not _concealed(world, item)
            ],
            public_identity=_text(node.profile.get("public_identity")),
        ))
    return entities


def _concealed(world: ObjectiveWorldGraph, node: WorldNode) -> bool:
    level = world.get_state(node.id).get("visibility_level") or node.profile.get("visibility_level")
    return str(level or "") in HIDDEN_SENSITIVITY


def _locations(
    runtime: BookStateRuntime, lexicon: BookLexicon, *, pov_ref: str, location_refs: list[str],
) -> tuple[ViewLocation | None, list[ViewLocation]]:
    """The POV's location, then the chapter's locations, then the nearest places it can reach.

    Travel runs on writer-visible routes only: a hidden route never reaches the writer, whether
    the POV knows it or has no cognition overlay to hide it.
    """
    key = _observer_key(runtime, pov_ref)
    cognition: dict[tuple[str, str], CognitionView] = {}
    if key is not None:
        # The POV's route beliefs still narrow its routes, but a believed false edge is a false
        # belief, never a writer-visible route.
        travel_view = copy.copy(runtime.cognition_by_observer[key])
        travel_view.false_edges = {}
        cognition[key] = travel_view
    graph = MapGraph(
        nodes=list(runtime.map.nodes_by_id.values()),
        # The graph rebuilds the reverse copies of bidirectional edges from their originals.
        edges=[
            edge for edge in runtime.map.edges_by_id.values()
            if "runtime_reverse_of" not in edge.metadata and is_writer_visible_map_edge(edge)
        ],
        cognition_by_observer=cognition,
    )
    origin = resolve_world_node_location_id(runtime.world, pov_ref) if pov_ref else ""
    minutes: dict[str, int] = {}
    if origin in graph.nodes_by_id:
        minutes[origin] = 0
        for node_id in graph.nodes_by_id:
            if node_id == origin:
                continue
            estimate = estimate_travel(
                runtime, lexicon, mover_id=pov_ref, from_location_id=origin, to_location_id=node_id, graph=graph,
            )
            if estimate.minutes is not None:
                minutes[node_id] = math.ceil(estimate.minutes - _MINUTE_EPSILON)
    nearest = sorted((value, node_id) for node_id, value in minutes.items() if node_id != origin)
    ordered = dict.fromkeys([origin, *location_refs, *(node_id for _, node_id in nearest[:VIEW_NEARBY_LOCATIONS])])
    locations = [
        ViewLocation(id=node_id, name=graph.nodes_by_id[node_id].name, minutes_from_pov=minutes.get(node_id))
        for node_id in ordered if node_id in graph.nodes_by_id
    ]
    if not origin:
        return None, locations
    place = graph.nodes_by_id.get(origin)
    return ViewLocation(id=origin, name=place.name if place is not None else "", minutes_from_pov=0), locations


def _pov_clocks(
    runtime: BookStateRuntime, *, pov_ref: str, now: int, calendar: StoryCalendar | None,
) -> list[ViewClock]:
    """Running public clocks, and secret ones the POV owns or shares through the owner's agenda secrecy_refs."""
    world = runtime.world
    insiders: dict[str, set[str]] = defaultdict(set)
    for node in world.nodes_by_id.values():
        if str(node.node_type) == "agenda":
            secrecy_refs = world.get_state(node.id).get("secrecy_refs")
            if isinstance(secrecy_refs, list):
                insiders[str(node.profile.get("owner_ref") or "")].update(
                    ref for ref in secrecy_refs if isinstance(ref, str) and ref
                )
    clocks: list[tuple[int | float, str, ViewClock]] = []
    for node in world.nodes_by_id.values():
        state = world.get_state(node.id)
        if str(node.node_type) != "clock" or not node.is_active or state.get("status") != "running":
            continue
        owner = str(node.profile.get("owner_ref") or "")
        insider = bool(pov_ref) and bool(owner) and (owner == pov_ref or pov_ref in insiders.get(owner, ()))
        # A clock without a declared scope is secret.
        if node.profile.get("scope") != "public" and not insider:
            continue
        due_at = state.get("due_at")
        due_at = due_at if type(due_at) is int else None
        clocks.append((due_at if due_at is not None else math.inf, node.id, ViewClock(
            label=str(node.profile.get("label") or node.name),
            remaining_label=_duration_label(calendar, max(0, due_at - now)) if due_at is not None else "",
            secret=node.profile.get("scope") != "public",
        )))
    return [clock for *_key, clock in sorted(clocks, key=lambda item: item[:2])]


def _duration_label(calendar: StoryCalendar | None, minutes: int) -> str:
    """A span in the calendar's units, without the epoch label."""
    return format_story_time(StoryCalendar("", 0, calendar.units if calendar is not None else ()), minutes)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# budget


def _trim(
    view: ChapterWorldView, budget_chars: int, *, chapter_location_ids: set[str], secret_refs: set[str],
) -> bool:
    """Drop trimmable entries in place, last (least relevant) first, until the rendering fits.

    Returns whether it fits.
    """

    def fits() -> bool:
        return len(render_view_text(view)) <= budget_chars

    def mark(key: str) -> None:
        if key not in view.trimmed:
            view.trimmed.append(key)

    if fits():
        return True
    steps: tuple[tuple[str, list[Any], Callable[[Any], bool]], ...] = (
        ("reader_known", view.reader_known, lambda item: False),
        ("locations", view.locations, lambda location: location.id in chapter_location_ids),
        ("pov_knowledge", view.pov_knowledge, lambda fact: fact.pov_believes_false or fact.fact_ref in secret_refs),
    )
    for key, items, keep in steps:
        for index in reversed(range(len(items))):
            if keep(items[index]):
                continue
            del items[index]
            mark(key)
            if fits():
                return True
    for entity in reversed(view.onstage):
        if entity.held_items:
            entity.held_items.clear()
            mark("held_items")
            if fits():
                return True
    return False


def render_view_text(view: ChapterWorldView) -> str:
    """The view as plain structural text: the measure of its budget."""
    location = view.pov_location
    lines = [
        f"pov: {view.pov_name} ({view.pov_ref})",
        f"pov_location: {location.name} ({location.id})" if location is not None else "pov_location: none",
        f"now: {view.now_label}",
        f"expected_span: {view.expected_span_label}",
    ]
    sections: dict[str, list[str]] = {
        "onstage": [
            _row(f"{entity.name} ({entity.ref})", location=entity.location_name, alive=entity.alive_status,
                 restraint=entity.restraint, health=entity.health, identity=entity.public_identity,
                 items=", ".join(entity.held_items))
            for entity in view.onstage
        ],
        "pov_knowledge": [
            _row(f"{fact.fact_ref}: {fact.proposition}", state=fact.state,
                 false_belief="yes" if fact.pov_believes_false else "",
                 pov_only_secret="yes" if fact.pov_only_secret else "")
            for fact in view.pov_knowledge
        ],
        "reader_known": list(view.reader_known),
        "allowed_reveals": [
            _row(f"{reveal.fact_ref}: {reveal.proposition}", directive=reveal.directive_id,
                 observer=reveal.observer, observer_name=reveal.observer_name, target_state=reveal.target_state)
            for reveal in view.allowed_reveals
        ],
        "hints": [_row(hint.hint_text, directive=hint.directive_id) for hint in view.hints],
        "forbidden_topics": [_row(topic.topic, fact=topic.fact_ref) for topic in view.forbidden_topics],
        "locations": [
            _row(f"{place.name} ({place.id})", minutes=place.minutes_from_pov) for place in view.locations
        ],
        "pov_clocks": [
            _row(clock.label, remaining=clock.remaining_label, secret="yes" if clock.secret else "")
            for clock in view.pov_clocks
        ],
    }
    for key, rows in sections.items():
        if rows:
            lines.extend([f"[{key}]", *(f"- {row}" for row in rows)])
    return "\n".join(lines)


def _row(head: str, **fields: Any) -> str:
    """``head | key=value | ...`` without the empty fields."""
    return " | ".join([head, *(f"{key}={value}" for key, value in fields.items() if value is not None and value != "")])


__all__ = ["VIEW_NEARBY_LOCATIONS", "build_chapter_world_view", "render_view_text"]
