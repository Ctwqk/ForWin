"""World tick step: news of events spreads by objective map travel time, through an in-transit queue."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.book_state.extraction.translator import _OBSERVER_TYPES
from forwin.book_state.knowledge import FACT_REF_PREFIX
from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.runtime import BookStateRuntime, resolve_world_node_location_id
from forwin.book_state.schema import ACTOR_NODE_TYPES
from forwin.models.book_state import GraphDeltaRow
from forwin.protocol.book_state import GraphDelta

from .delta_builder import tick_event_fact_ref
from .travel import _MINUTES_PER_HOUR
from .types import WORLD_ENGINE_SOURCE_TYPE, EffectEvent, EffectKnowledge, NewsItem, TickCandidate


# An observer holding a fact in one of these states already knows it: news adds nothing.
_AWARE_STATES = frozenset({"knows", "believes"})
_WITNESS_STATE, _WITNESS_CHANNEL = "knows", "witnessed"
_NEWS_STATE, _NEWS_CHANNEL = "heard", "rumor"
# Arrival minutes absorb float noise before rounding up.
_MINUTE_EPSILON = 1e-6


@dataclass(frozen=True)
class PropagationResult:
    knowledge: list[EffectKnowledge]
    # The queue the tick writes forward, by arrive_at, fact_ref and observer_ref.
    in_transit: list[NewsItem]


def latest_news_in_transit(session: Session, project_id: str) -> list[NewsItem]:
    """The queue the latest world_engine delta wrote in ``metadata["news_in_transit"]``."""
    raw = session.scalar(
        select(GraphDeltaRow.metadata_json)
        .where(GraphDeltaRow.project_id == project_id, GraphDeltaRow.source_type == WORLD_ENGINE_SOURCE_TYPE)
        .order_by(GraphDeltaRow.chapter_number.desc(), GraphDeltaRow.created_at.desc(), GraphDeltaRow.id.desc())
        .limit(1)
    )
    metadata = json.loads(raw or "{}")
    items = metadata.get("news_in_transit") if isinstance(metadata, dict) else None
    return [NewsItem.model_validate(item) for item in items or [] if isinstance(item, dict)]


def chapter_events(delta: GraphDelta) -> list[tuple[EffectEvent, str]]:
    """The events a chapter_extraction delta creates, each with its existing ``fact:<id>``.

    A chapter records no minute for its events, so each happens at the chapter's end: its news
    never arrives before the chapter could have shown it.
    """
    at = delta.story_time_end if delta.story_time_end is not None else delta.story_time_start or 0
    events: list[tuple[EffectEvent, str]] = []
    for patch in delta.node_patches:
        if str(patch.op) != "create" or str(patch.node_type) != "event" or not isinstance(patch.new_value, dict):
            continue
        profile = patch.new_value.get("profile") or {}
        state = patch.new_value.get("state") or {}
        fact_refs = [str(ref) for ref in state.get("result_refs") or [] if str(ref).startswith(FACT_REF_PREFIX)]
        if not fact_refs:
            continue
        events.append((
            EffectEvent(
                summary=str(patch.new_value.get("summary") or ""),
                participant_refs=[str(ref) for ref in state.get("participant_ids") or []],
                location_id=str(profile.get("location_id") or ""),
                visibility="public" if state.get("visibility_level") == "public" else "secret",
                at=at,
            ),
            fact_refs[0],
        ))
    return events


def tick_events(
    project_id: str, tick_id: str, candidates: Sequence[TickCandidate],
) -> list[tuple[EffectEvent, str]]:
    """The candidates' events, each with the ``fact:<id>`` the tick builder creates for it."""
    return [
        (event, tick_event_fact_ref(project_id, tick_id, candidate.candidate_id, index))
        for candidate in candidates
        for index, event in enumerate(candidate.effects.events)
    ]


def propagate_news(
    runtime: BookStateRuntime,
    lexicon: BookLexicon,
    *,
    events: Sequence[EffectEvent],
    in_transit: Sequence[NewsItem],
    now: int,
    fact_ref_for_event: Mapping[int, str],
) -> PropagationResult:
    """Who learns of the events and the queued news by now, and what news is still on its way.

    Queued news arriving by now is heard. An event's participants and the actors at its location
    witness it. A public event is heard by every other actor with a location at ``at`` plus the
    objective shortest route's minutes divided by ``lexicon.news_speed_multiplier``; news arriving
    after now joins the queue, and an unreachable actor hears nothing. Only living, active actors
    learn anything, and an actor already holding what it would learn, or knowing or believing
    the fact, learns nothing new. ``fact_ref_for_event`` maps an event's position in ``events``
    to its fact: a chapter event's ``fact:<id>``, or ``tick_event_fact_ref`` for a tick event.
    Locations are the ones in ``runtime``.
    """
    news = _News(runtime, now)
    for item in in_transit:
        if item.fact_ref.removeprefix(FACT_REF_PREFIX) in runtime.world.facts_by_id:
            news.hear(item.observer_ref, item.fact_ref, item.arrive_at)
    for position, event in enumerate(events):
        fact_ref = fact_ref_for_event.get(position)
        if fact_ref is None:
            continue
        witnesses = sorted({
            actor_id for actor_id, location_id in news.recipients.items()
            if actor_id in event.participant_refs or (event.location_id and location_id == event.location_id)
        })
        for actor_id in witnesses:
            news.learn(actor_id, fact_ref, _WITNESS_STATE, _WITNESS_CHANNEL)
        if event.visibility != "public" or event.location_id not in runtime.map.nodes_by_id:
            continue
        for actor_id, location_id in sorted(news.recipients.items()):
            if actor_id in witnesses or not location_id:
                continue
            route = runtime.map.shortest_path(event.location_id, location_id, metric="travel_time")
            if route.reachable:
                minutes = route.total_travel_time * _MINUTES_PER_HOUR / lexicon.news_speed_multiplier
                news.hear(actor_id, fact_ref, event.at + math.ceil(minutes - _MINUTE_EPSILON))
    return news.result()


class _News:
    """Knowledge delivered and news queued in one propagation, at most one of each per observer and fact."""

    def __init__(self, runtime: BookStateRuntime, now: int) -> None:
        self.runtime = runtime
        self.now = now
        world = runtime.world
        # Living, active actors and their locations ("" for none).
        self.recipients = {
            node_id: resolve_world_node_location_id(world, node_id)
            for node_id, node in world.nodes_by_id.items()
            if str(node.node_type) in ACTOR_NODE_TYPES and node.is_active
            and world.get_state(node_id).get("alive_status") != "dead"
        }
        self.delivered: dict[tuple[str, str], EffectKnowledge] = {}
        self.pending: dict[tuple[str, str], NewsItem] = {}

    def learn(self, observer_id: str, fact_ref: str, state: str, channel: str) -> None:
        key = (observer_id, fact_ref)
        current = self.delivered.get(key)
        # Witnessing outranks hearing, so it replaces news delivered earlier in the same tick.
        if current is not None and current.state in {_WITNESS_STATE, state}:
            return
        if not self._settled(observer_id, fact_ref, state):
            self.delivered[key] = EffectKnowledge(
                observer_ref=observer_id, fact_ref=fact_ref, state=state, channel=channel,
            )

    def hear(self, observer_id: str, fact_ref: str, arrive_at: int) -> None:
        if arrive_at <= self.now:
            self.learn(observer_id, fact_ref, _NEWS_STATE, _NEWS_CHANNEL)
            return
        key = (observer_id, fact_ref)
        current = self.pending.get(key)
        if (current is None or arrive_at < current.arrive_at) and not self._settled(observer_id, fact_ref, _NEWS_STATE):
            self.pending[key] = NewsItem(fact_ref=fact_ref, observer_ref=observer_id, arrive_at=arrive_at)

    def result(self) -> PropagationResult:
        # News already delivered by another way leaves the queue.
        queue = [item for key, item in self.pending.items() if key not in self.delivered]
        return PropagationResult(
            knowledge=list(self.delivered.values()),
            in_transit=sorted(queue, key=lambda item: (item.arrive_at, item.fact_ref, item.observer_ref)),
        )

    def _settled(self, observer_id: str, fact_ref: str, state: str) -> bool:
        """Not a recipient, or already holding the state or knowing the fact before the tick."""
        if observer_id not in self.recipients:
            return True
        node_type = str(self.runtime.world.nodes_by_id[observer_id].node_type)
        view = self.runtime.cognition_by_observer.get((_OBSERVER_TYPES[node_type], observer_id))
        recorded = view.knowledge_by_ref.get(fact_ref) if view is not None else None
        return recorded in _AWARE_STATES or recorded == state


__all__ = [
    "PropagationResult",
    "chapter_events",
    "latest_news_in_transit",
    "propagate_news",
    "tick_events",
]
