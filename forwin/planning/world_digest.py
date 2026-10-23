"""Deterministic, author-visible world context for planning prompts only."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from forwin.book_state.book_frame_repository import BookLexiconRepository, StoryCalendarRepository
from forwin.book_state.knowledge import FACT_REF_PREFIX
from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.reveal_schedule import earliest_reveal_chapters
from forwin.book_state.runtime import BookStateRuntime, resolve_world_node_location_id
from forwin.book_state.schema import ACTOR_NODE_TYPES
from forwin.book_state.story_clock import StoryCalendar, format_story_time
from forwin.book_state.thread_sampling import sample_active_threads
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.protocol.book_state import WorldNode
from forwin.world_engine.propagation import latest_news_in_transit
from forwin.world_engine.travel import estimate_travel
from forwin.world_engine.types import WORLD_ENGINE_SOURCE_TYPE
from forwin.writer.prompt_core.planning import WORLD_DIGEST_TITLES


DIGEST_TOP_ACTORS = 8
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
    """Build eight sections, then remove whole sections from the end to fit the budget.

    Hidden intents and objective secrets are included: this digest must never be supplied
    to the chapter writer. Equal relevance is resolved by node id, independent of row order.
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
    lines: dict[str, list[str]] = {key: [] for key in WORLD_DIGEST_TITLES}
    lines["protagonist"] = [_actor_line(runtime, protagonist, calendar)]
    lines["actors"] = _actors(runtime, lexicon, calendar, protagonist_ref, location_id)

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
            fact.proposition, fact_ref=news.fact_ref, observer_ref=news.observer_ref,
            location=_name(runtime, location_id), arrive_at=_time(calendar, news.arrive_at),
        ))

    events = [node for node in runtime.world.nodes_by_id.values()
              if str(node.node_type) == "event" and node.metadata.get("source") == WORLD_ENGINE_SOURCE_TYPE
              and max(0, as_of_chapter - _RECENT_CHAPTERS + 1) <= node.created_at_chapter <= as_of_chapter]
    for event in sorted(events, key=lambda node: (-node.created_at_chapter, node.id)):
        state = runtime.world.get_state(event.id)
        lines["offscreen_events"].append(_line(
            event.name, ref=event.id, chapter=event.created_at_chapter, summary=event.summary,
            at=_time(calendar, state.get("actual_story_time")), profile=event.profile, state=state,
        ))

    reader = runtime.cognition_by_observer.get(("reader", "reader"))
    recorded = reader.knowledge_by_ref if reader is not None else {}
    targets = earliest_reveal_chapters(session, project_id)
    for fact in sorted(runtime.world.facts_by_id.values(), key=lambda item: item.id):
        ref = f"{FACT_REF_PREFIX}{fact.id}"
        lines["secrets_progress"].append(_line(
            fact.proposition, fact_ref=ref, sensitivity=fact.sensitivity_level,
            reader_state=recorded.get(ref, "unknown"), reveal_target=targets.get(fact.id),
        ))

    for node in sorted(runtime.world.nodes_by_id.values(), key=lambda item: item.id):
        state = runtime.world.get_state(node.id)
        if not node.is_active:
            continue
        if str(node.node_type) == "conflict" and state.get("status", node.status) not in {"resolved", "closed"}:
            lines["tensions"].append(_line(node.name, ref=node.id, summary=node.summary,
                                             profile=node.profile, state=state))
        if str(node.node_type) == "clock" and state.get("status") == "running":
            lines["clocks"].append(_line(node.name, ref=node.id, profile=node.profile,
                                          due_at=_time(calendar, state.get("due_at")), state=state))

    sampled = sample_active_threads(session=session, project_id=project_id, chapter_number=as_of_chapter,
                                    limit=_THREAD_LIMIT, stale_window=_STALE_THREAD_WINDOW)
    for thread in sampled.threads:
        beat = sampled.latest_beats.get(thread.id)
        last_active = beat.chapter_number if beat is not None else thread.opened_at_chapter
        if as_of_chapter - last_active >= _STALE_THREAD_WINDOW:
            lines["stalled_threads_obligations"].append(_line(
                thread.name, thread_ref=thread.id, description=thread.description,
                last_active_chapter=last_active, priority=thread.priority,
            ))
    obligations = NarrativeObligationRepository(session).list_active_for_context(
        project_id, chapter_number=as_of_chapter + 1,
    )
    for item in sorted(obligations, key=lambda item: (item.deadline_chapter, item.priority, item.id)):
        lines["stalled_threads_obligations"].append(_line(
            item.summary, obligation_ref=item.id, deadline_chapter=item.deadline_chapter,
            priority=item.priority, subject_refs=item.subject_refs, payoff_test=item.payoff_test,
        ))
    sections = [DigestSection(key, title, lines[key]) for key, title in WORLD_DIGEST_TITLES.items()]
    trimmed_keys: list[str] = []
    text = _render(sections)
    while sections and len(text) > budget_chars:
        trimmed_keys.append(sections.pop().key)
        text = _render(sections)
    return WorldDigest(sections, text, trimmed_keys)


def _actors(runtime: BookStateRuntime, lexicon: BookLexicon, calendar: StoryCalendar | None,
            protagonist_ref: str, location_id: str) -> list[str]:
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
                              for intent in _owned(runtime, actor.id, "intent"))
        distance = distances.get(actor.id)
        rank = (distance if distance is not None else float("inf"),
                minutes if minutes is not None else float("inf"), not targets_related, actor.id)
        ranked.append((rank, actor, distance, minutes, targets_related))
    return [
        _actor_line(runtime, actor, calendar) + " | " + _fields(
            relationship_hops=distance, travel_minutes=minutes, targets_related=targets_related,
        )
        for _, actor, distance, minutes, targets_related in sorted(ranked, key=lambda item: item[0])[:DIGEST_TOP_ACTORS]
    ]


def _owned(runtime: BookStateRuntime, owner_ref: str, kind: str) -> list[WorldNode]:
    statuses = _PENDING_INTENTS if kind == "intent" else {"active"}
    return sorted(
        (node for node in runtime.world.nodes_by_id.values()
         if str(node.node_type) == kind and node.is_active and node.profile.get("owner_ref") == owner_ref
         and runtime.world.get_state(node.id).get("status") in statuses),
        key=lambda node: (runtime.world.get_state(node.id).get("scheduled_at", 0), node.id),
    )


def _actor_line(runtime: BookStateRuntime, actor: WorldNode, calendar: StoryCalendar | None) -> str:
    return _line(
        actor.name, ref=actor.id,
        location=_name(runtime, resolve_world_node_location_id(runtime.world, actor.id)),
        state=runtime.world.get_state(actor.id),
        agendas=[{"ref": node.id, "name": node.name, "profile": node.profile,
                  "state": runtime.world.get_state(node.id)} for node in _owned(runtime, actor.id, "agenda")],
        intents=[{"ref": node.id, "name": node.name, "profile": node.profile,
                  "state": runtime.world.get_state(node.id),
                  "scheduled_at": _time(calendar, runtime.world.get_state(node.id).get("scheduled_at"))}
                 for node in _owned(runtime, actor.id, "intent")],
    )


def _name(runtime: BookStateRuntime, ref: str) -> str:
    node = runtime.world.get_node(ref) or runtime.map.nodes_by_id.get(ref)
    return node.name if node is not None else ref


def _time(calendar: StoryCalendar | None, minute: Any) -> str:
    if type(minute) is not int:
        return "unknown"
    return f"{minute} ({format_story_time(calendar, minute)})" if calendar is not None else str(minute)


def _fields(**fields: Any) -> str:
    return " | ".join(f"{key}={_value(value)}" for key, value in fields.items())


def _value(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, str):
        return value.replace("\n", " ").replace("\r", " ")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _line(name: str, **fields: Any) -> str:
    return f"{_value(name)} | {_fields(**fields)}"


def _render(sections: list[DigestSection]) -> str:
    return "\n\n".join("\n".join([section.title, *section.lines]) for section in sections)


__all__ = ["DIGEST_TOP_ACTORS", "DigestSection", "WorldDigest", "build_world_digest"]
