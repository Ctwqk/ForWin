"""Deterministic, author-visible world context for planning prompts only."""

from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from forwin.book_state.knowledge import READER_OVERLAY_KEY
from forwin.book_state.book_frame_repository import BookLexiconRepository, StoryCalendarRepository
from forwin.book_state.knowledge import FACT_REF_PREFIX, is_secret_fact
from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.reveal_schedule import earliest_reveal_chapters
from forwin.book_state.runtime import BookStateRuntime, resolve_world_node_location_id
from forwin.book_state.schema import ACTOR_NODE_TYPES
from forwin.book_state.story_clock import StoryCalendar, format_story_time
from forwin.book_state.thread_sampling import sample_active_threads
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.protocol.book_state import WorldEdge, WorldNode
from forwin.world_engine.propagation import latest_news_in_transit
from forwin.world_engine.travel import estimate_travel
from forwin.world_engine.types import WORLD_ENGINE_SOURCE_TYPE
from forwin.writer.prompt_core.planning import WORLD_DIGEST_TITLES


logger = logging.getLogger(__name__)

DIGEST_SECTION_KEYS = (
    "protagonist", "actors", "news_in_transit", "offscreen_events", "secrets_progress",
    "tensions", "clocks", "world_conditions", "stalled_threads_obligations",
)
DIGEST_TOP_ACTORS = 8
DIGEST_INTENTS_PER_ACTOR = 3
DIGEST_SECRETS_LIMIT = 12
DIGEST_TENSIONS_LIMIT = 10
DIGEST_TENSION_EDGE_TYPES = frozenset({"enemy_of", "rival_of", "distrusts", "owes_debt_to", "opposes"})
DIGEST_TEXT_FIELD_LIMIT = 60
_PROTAGONIST_RELATIONS_LIMIT = 6
_RECENT_CHAPTERS = 3
_STALE_THREAD_WINDOW = 3
_THREAD_LIMIT = 20
_PENDING_INTENTS = frozenset({"planned", "due", "held"})


@dataclass(frozen=True)
class DigestSection:
    key: str
    title: str
    lines: list[str]


@dataclass(frozen=True)
class WorldDigest:
    sections: list[DigestSection]
    text: str
    trimmed_keys: list[str]


def build_world_digest(
    session: Session,
    *,
    project_id: str,
    as_of_chapter: int,
    protagonist_ref: str,
    window_end_minute: int,
    budget_chars: int,
    runtime: BookStateRuntime | None = None,
) -> WorldDigest:
    """Build nine sections, then shed lower-ranked lines while preserving section coverage.

    Hidden intents and objective secrets are included: this digest must never be supplied
    to the chapter writer. Equal relevance is resolved by node id, independent of row order.
    trimmed_keys records each affected section once, in first-removal order.
    """
    if budget_chars < 0:
        raise ValueError("budget_chars must be nonnegative")
    if runtime is None:
        runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=as_of_chapter)
    protagonist = runtime.world.get_node(protagonist_ref)
    if protagonist is None or str(protagonist.node_type) != "character":
        raise ValueError("protagonist_ref must name a character")
    lexicon = BookLexiconRepository(session).get(project_id)
    calendar = StoryCalendarRepository(session).get(project_id)
    location_id = resolve_world_node_location_id(runtime.world, protagonist_ref)
    owned = _index_owned(runtime)
    lines: dict[str, list[str]] = {key: [] for key in DIGEST_SECTION_KEYS}
    protagonist_state = runtime.world.get_state(protagonist_ref)
    resources_and_rank = {key: protagonist_state[key] for key in
                          ("resources", "resource_summary", "rank_title", "power_level") if key in protagonist_state}
    lines["protagonist"] = [_actor_line(runtime, protagonist, calendar, owned)
                            + (" | " + _fields(**resources_and_rank) if resources_and_rank else "")]
    relations = sorted((edge for edge in runtime.world.edges_by_id.values() if edge.is_active),
                       key=lambda edge: (protagonist_ref not in (edge.source_id, edge.target_id), edge.id))
    lines["protagonist"].extend(_relation_line(runtime, edge) for edge in
                               [edge for edge in relations if protagonist_ref in (edge.source_id, edge.target_id)]
                               [:_PROTAGONIST_RELATIONS_LIMIT])
    lines["tensions"] = [_relation_line(runtime, edge) for edge in relations
                         if edge.edge_type in DIGEST_TENSION_EDGE_TYPES][:DIGEST_TENSIONS_LIMIT]
    lines["actors"] = _actors(runtime, lexicon, calendar, protagonist_ref, location_id, owned)

    for news in sorted(latest_news_in_transit(session, project_id),
                       key=lambda item: (item.arrive_at, item.fact_ref, item.observer_ref)):
        if news.arrive_at > window_end_minute or not location_id:
            continue
        if resolve_world_node_location_id(runtime.world, news.observer_ref) != location_id:
            continue
        fact = runtime.world.facts_by_id.get(news.fact_ref.removeprefix(FACT_REF_PREFIX))
        if fact is None:
            continue
        lines["news_in_transit"].append(_line(
            fact.proposition, observer=_name(runtime, news.observer_ref),
            location=_name(runtime, location_id), arrive_at=_time(calendar, news.arrive_at),
        ))

    events = [node for node in runtime.world.nodes_by_id.values()
              if node.is_active and str(node.node_type) == "event"
              and node.metadata.get("source") == WORLD_ENGINE_SOURCE_TYPE
              and max(0, as_of_chapter - _RECENT_CHAPTERS + 1) <= node.created_at_chapter <= as_of_chapter]
    for event in sorted(events, key=lambda node: (-node.created_at_chapter, node.id)):
        lines["offscreen_events"].append(_line(
            _text_field(event.summary or event.profile.get("public_summary") or event.name or event.id),
            location=_name(runtime, event.profile.get("location_id") or resolve_world_node_location_id(runtime.world, event.id)) or "unknown",
            chapter=event.created_at_chapter,
        ))

    reader = runtime.cognition_by_observer.get(READER_OVERLAY_KEY)
    recorded = reader.knowledge_by_ref if reader is not None else {}
    targets = earliest_reveal_chapters(session, project_id)
    secrets = [fact for fact in runtime.world.facts_by_id.values()
               if is_secret_fact(fact.sensitivity_level, fact_id=fact.id, protected=targets)
               or f"{FACT_REF_PREFIX}{fact.id}" in recorded]
    for fact in sorted(secrets, key=lambda item: (
        recorded.get(f"{FACT_REF_PREFIX}{item.id}") == "knows", targets.get(item.id) or float("inf"), item.id,
    ))[:DIGEST_SECRETS_LIMIT]:
        ref = f"{FACT_REF_PREFIX}{fact.id}"
        lines["secrets_progress"].append(_line(
            fact.proposition, sensitivity=fact.sensitivity_level,
            reader_state=recorded.get(ref, "unknown"), reveal_target=targets.get(fact.id),
        ))

    for node in sorted(runtime.world.nodes_by_id.values(), key=lambda item: item.id):
        if not node.is_active or str(node.node_type) != "clock":
            continue
        state = runtime.world.get_state(node.id)
        if state.get("status") == "running":
            due_at = state.get("due_at")
            remaining = max(0, due_at - window_end_minute) if type(due_at) is int else None
            lines["clocks"].append(_line(
                _text_field(node.profile.get("label") or node.name or node.id),
                remaining=_duration(calendar, remaining), scope=node.profile.get("scope"),
            ))

    for node in sorted(runtime.world.nodes_by_id.values(), key=lambda item: item.id):
        if not node.is_active or node.node_type != "world_variable":
            continue
        state = runtime.world.get_state(node.id)
        scale, level = node.profile.get("scale", []), state.get("level")
        if state.get("status") == "active" and type(level) is int and 0 <= level < len(scale):
            lines["world_conditions"].append(_line(
                _text_field(node.profile.get("label") or node.name or node.id), level=_text_field(scale[level]),
            ))

    sampled = sample_active_threads(session=session, project_id=project_id, chapter_number=as_of_chapter,
                                    limit=_THREAD_LIMIT, stale_window=_STALE_THREAD_WINDOW, runtime=runtime)
    for thread in sampled.threads:
        beat = sampled.latest_beats.get(thread.id)
        last_active = beat.chapter_number if beat is not None else thread.opened_at_chapter
        if as_of_chapter - last_active >= _STALE_THREAD_WINDOW:
            lines["stalled_threads_obligations"].append(_line(
                thread.name, description=thread.description,
                last_active_chapter=last_active, priority=thread.priority,
            ))
    obligations = NarrativeObligationRepository(session).list_active_for_context(
        project_id, chapter_number=as_of_chapter + 1,
    )
    for item in sorted(obligations, key=lambda item: (item.deadline_chapter, item.priority, item.id)):
        lines["stalled_threads_obligations"].append(_line(
            item.summary, deadline_chapter=item.deadline_chapter,
            priority=item.priority, subjects=[_name(runtime, ref) for ref in item.subject_refs], payoff_test=item.payoff_test,
        ))
    sections = [DigestSection(key, WORLD_DIGEST_TITLES[key], lines[key]) for key in DIGEST_SECTION_KEYS]
    trimmed_keys: list[str] = []
    # Reserve one other section's best line when the budget can hold it beside the protagonist.
    protagonist_line = DigestSection(sections[0].key, sections[0].title, sections[0].lines[:1])
    companion = next((section.key for section in sections[1:] if section.lines and len(render_digest_sections([
        protagonist_line, DigestSection(section.key, section.title, section.lines[:1]),
    ])) <= budget_chars), None)
    text = render_digest_sections(sections)
    while sections and len(text) > budget_chars:
        # Later lines have lower relevance. Preserve one line per populated section
        # before removing any section's last line; section order breaks equal ranks.
        index = max((i for i, section in enumerate(sections)
                     if section.key != companion or len(section.lines) > 1), key=lambda i: (
            not sections[i].lines, len(sections[i].lines) > 1,
            len(sections[i].lines), i,
        ))
        section = sections[index]
        if section.key not in trimmed_keys:
            trimmed_keys.append(section.key)
        if len(section.lines) > 1:
            sections[index] = DigestSection(section.key, section.title, section.lines[:-1])
        else:
            sections.pop(index)
            if section.key == "protagonist":
                logger.warning("World digest protagonist section trimmed for project %s at budget %s", project_id, budget_chars)
        text = render_digest_sections(sections)
    return WorldDigest(sections, text, trimmed_keys)


def _actors(runtime: BookStateRuntime, lexicon: BookLexicon, calendar: StoryCalendar | None,
            protagonist_ref: str, location_id: str, owned: dict[tuple[str, str], list[WorldNode]]) -> list[str]:
    adjacency: dict[str, set[str]] = {}
    for edge in runtime.world.edges_by_id.values():
        if edge.is_active:
            adjacency.setdefault(edge.source_id, set()).add(edge.target_id)
            adjacency.setdefault(edge.target_id, set()).add(edge.source_id)
    distances = {protagonist_ref: 0}
    queue = deque([protagonist_ref])
    while queue:
        current = queue.popleft()
        for other in sorted(adjacency.get(current, ())):
            if other not in distances:
                distances[other] = distances[current] + 1
                queue.append(other)
    related = {protagonist_ref, *adjacency.get(protagonist_ref, ())}
    ranked = []
    for actor in runtime.world.nodes_by_id.values():
        if (actor.id == protagonist_ref or str(actor.node_type) not in ACTOR_NODE_TYPES or not actor.is_active
                or runtime.world.get_state(actor.id).get("alive_status") == "dead"):
            continue
        origin = resolve_world_node_location_id(runtime.world, actor.id)
        minutes = None
        if origin and location_id:
            minutes = estimate_travel(runtime, lexicon, mover_id=actor.id,
                                      from_location_id=origin, to_location_id=location_id).minutes
        targets_related = any(related.intersection(intent.profile.get("target_refs") or [])
                              for intent in owned.get((actor.id, "intent"), ()))
        distance = distances.get(actor.id)
        rank = (distance if distance is not None else float("inf"),
                minutes if minutes is not None else float("inf"), not targets_related, actor.id)
        ranked.append((rank, actor, distance, minutes, targets_related))
    return [
        _actor_line(runtime, actor, calendar, owned) + " | " + _fields(
            relationship_hops=distance, travel_minutes=minutes, targets_related=targets_related,
        )
        for _, actor, distance, minutes, targets_related in sorted(ranked, key=lambda item: item[0])[:DIGEST_TOP_ACTORS]
    ]


def _index_owned(runtime: BookStateRuntime) -> dict[tuple[str, str], list[WorldNode]]:
    owned: dict[tuple[str, str], list[WorldNode]] = defaultdict(list)
    for node in runtime.world.nodes_by_id.values():
        kind = str(node.node_type)
        if not node.is_active or kind not in {"agenda", "intent"}:
            continue
        statuses = _PENDING_INTENTS if kind == "intent" else {"active"}
        if runtime.world.get_state(node.id).get("status") in statuses:
            owned[(node.profile.get("owner_ref", ""), kind)].append(node)
    for (_, kind), nodes in owned.items():
        nodes.sort(key=lambda node: (
            runtime.world.get_state(node.id).get("scheduled_at", float("inf")) if kind == "intent" else 0, node.id,
        ))
    return owned


def _actor_line(runtime: BookStateRuntime, actor: WorldNode, calendar: StoryCalendar | None,
                owned: dict[tuple[str, str], list[WorldNode]]) -> str:
    state = runtime.world.get_state(actor.id)
    restraint = state.get("restraint")
    return _line(
        actor.name or actor.id,
        location=_name(runtime, resolve_world_node_location_id(runtime.world, actor.id)) or "unknown",
        alive_status=state.get("alive_status"),
        restraint=restraint.get("state") if isinstance(restraint, dict) else restraint,
        tier=state.get("actor_tier"),
        agendas=[_text_field(node.profile.get("goal")) for node in owned.get((actor.id, "agenda"), ())],
        intents=[{"action_type": node.profile.get("action_type"), "summary": _text_field(node.profile.get("summary") or node.summary),
                  "targets": [_name(runtime, ref) for ref in node.profile.get("target_refs", ())],
                  "scheduled_at": _time(calendar, runtime.world.get_state(node.id).get("scheduled_at"))}
                 for node in owned.get((actor.id, "intent"), ())[:DIGEST_INTENTS_PER_ACTOR]],
    )


def _relation_line(runtime: BookStateRuntime, edge: WorldEdge) -> str:
    return " — ".join(_value(value) for value in (_name(runtime, edge.source_id), edge.edge_type,
                                                  _name(runtime, edge.target_id)))


def _name(runtime: BookStateRuntime, ref: str) -> str:
    node = runtime.world.get_node(ref) or runtime.map.nodes_by_id.get(ref)
    return (node.name or ref) if node is not None else ref


def _time(calendar: StoryCalendar | None, minute: Any) -> str:
    if type(minute) is not int:
        return "unknown"
    return f"{minute} ({format_story_time(calendar, minute)})" if calendar is not None else str(minute)


def _duration(calendar: StoryCalendar | None, minutes: int | None) -> str:
    if calendar is not None:
        calendar = StoryCalendar("", 0, calendar.units)
    return _time(calendar, minutes)


def _fields(**fields: Any) -> str:
    return " | ".join(f"{key}={_value(value)}" for key, value in fields.items())


def _text_field(value: str | None) -> str | None:
    if value is None or len(value) <= DIGEST_TEXT_FIELD_LIMIT:
        return value
    return value[:DIGEST_TEXT_FIELD_LIMIT - 1] + "…"


def _value(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, str):
        return value.replace("\n", " ").replace("\r", " ")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _line(name: str, **fields: Any) -> str:
    return f"{_value(name)} | {_fields(**fields)}"


def render_digest_sections(sections: list[DigestSection]) -> str:
    """Render sections with the same separators used for budget accounting."""
    return "\n\n".join("\n".join([section.title, *section.lines]) for section in sections)


__all__ = [
    "DIGEST_SECTION_KEYS", "DIGEST_TOP_ACTORS", "DIGEST_INTENTS_PER_ACTOR", "DIGEST_SECRETS_LIMIT",
    "DIGEST_TENSIONS_LIMIT", "DIGEST_TENSION_EDGE_TYPES", "DIGEST_TEXT_FIELD_LIMIT",
    "DigestSection", "WorldDigest", "build_world_digest", "render_digest_sections",
]
