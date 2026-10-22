from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from forwin.book_state.book_frame_repository import BookLexiconRepository
from forwin.book_state.cognition import CognitionView
from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.map_graph import MapGraph
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
from forwin.protocol.book_state import CognitionOverlay, FactNode, MapEdge, MapNode, WorldNode
from forwin.world_engine.clocks import expire_clocks
from forwin.world_engine.conflict_types import ConflictDraft
from forwin.world_engine.delta_builder import tick_event_fact_ref
from forwin.world_engine.intents import (
    OnstageResult,
    apply_intent_interactions,
    due_onstage_intents,
    onstage_bookkeeping,
    select_due_intents,
)
from forwin.world_engine.propagation import (
    chapter_events,
    latest_news_in_transit,
    propagate_news,
    tick_events,
)
from forwin.world_engine.types import (
    EffectEntityChange,
    EffectEvent,
    EffectKnowledge,
    NewsItem,
    TickCandidate,
    WorldEffects,
)
from tests import test_world_gate_v6
from tests.test_world_gate_v6 import (
    _codes,
    _commit,
    _delta,
    _event,
    _review,
    _seed_world,
    _session_factory,
    _time,
    _World,
)
from tests.test_world_tick_delta import _built, _candidate, _chapter_one, _commit_tick, _context


PROJECT = "project-steps"
NOW = 50_000


# an in-memory runtime: sites 1-5 and the nodes a test needs


def _site(number: int) -> str:
    return f"site-{number}"


def _runtime(
    *nodes: WorldNode,
    edges: Sequence[tuple[int, int, float]] = (),
    facts: Sequence[str] = (),
    knowledge: dict[str, dict[str, str]] | None = None,
) -> BookStateRuntime:
    """``edges`` are two-way roads (from site, to site, hours); ``knowledge`` maps a character to its states."""
    map_graph = MapGraph(
        nodes=[
            MapNode(id=_site(number), project_id=PROJECT, node_type="site", name=_site(number))
            for number in range(1, 6)
        ],
        edges=[
            MapEdge(
                id=f"road-{start}-{end}", project_id=PROJECT, from_node_id=_site(start), to_node_id=_site(end),
                edge_type="road", bidirectional=True, travel_time=hours,
            )
            for start, end, hours in edges
        ],
    )
    cognition = {
        ("character", observer): CognitionView(CognitionOverlay(
            id=f"overlay-{observer}", project_id=PROJECT, observer_type="character", observer_id=observer,
            knowledge_by_ref=states,
        ))
        for observer, states in (knowledge or {}).items()
    }
    world = ObjectiveWorldGraph(
        nodes=list(nodes),
        facts=[FactNode(id=fact_id, project_id=PROJECT, proposition=fact_id) for fact_id in facts],
    )
    return BookStateRuntime(
        project_id=PROJECT, as_of_chapter=3, world=world, map_graph=map_graph, cognition_by_observer=cognition,
    )


def _actor(node_id: str, *, location: str = "", tier: str = "minor", **state: Any) -> WorldNode:
    return WorldNode(
        id=node_id, project_id=PROJECT, node_type="character", name=node_id,
        state={"actor_tier": tier, "location_id": location, "alive_status": "alive", **state},
    )


def _intent(
    node_id: str,
    owner: str,
    *,
    at: int,
    status: str = "planned",
    onstage: bool = False,
    impact: str = "minor",
    slips: int = 0,
) -> WorldNode:
    return WorldNode(
        id=node_id, project_id=PROJECT, node_type="intent", name=node_id,
        profile={
            "owner_ref": owner, "action_type": "negotiate", "summary": f"summary {node_id}",
            "requires_onstage": onstage, "impact_level": impact,
        },
        state={"status": status, "scheduled_at": at, "slip_count": slips},
    )


def _clock(
    node_id: str, *, due_at: int, owner: str = "", status: str = "running", scope: str = "public", on_expire: str = "",
) -> WorldNode:
    return WorldNode(
        id=node_id, project_id=PROJECT, node_type="clock", name=node_id,
        profile={"label": f"label {node_id}", "owner_ref": owner, "scope": scope, "on_expire": on_expire},
        state={"due_at": due_at, "status": status},
    )


def _interaction(intent_id: str, effect: str) -> dict[str, str]:
    return {"intent_id": intent_id, "effect": effect, "evidence": f"evidence {intent_id}"}


def _commit_bookkeeping(runtime: BookStateRuntime, candidates: Sequence[TickCandidate], now: int) -> None:
    """What committing the candidates does to the intents, as the tick builder writes it."""
    for candidate in candidates:
        for intent_id, status in candidate.intent_status_changes.items():
            runtime.world.states_by_node_id[intent_id]["status"] = status
        for intent_id, slips in candidate.slip_updates.items():
            runtime.world.states_by_node_id[intent_id].update(slip_count=slips, scheduled_at=now)


def _heard(observer: str, fact_ref: str) -> EffectKnowledge:
    return EffectKnowledge(observer_ref=observer, fact_ref=fact_ref, state="heard", channel="rumor")


def _witnessed(observer: str, fact_ref: str) -> EffectKnowledge:
    return EffectKnowledge(observer_ref=observer, fact_ref=fact_ref, state="knows", channel="witnessed")


# clocks


def test_running_clock_past_due_expires_and_schedules_named_intent() -> None:
    runtime = _runtime(
        _actor("actor-1", location=_site(2)),
        _intent("intent-1", "actor-1", at=NOW + 600),
        _clock("clock-b", due_at=NOW - 30, on_expire="intent-1"),
        _clock("clock-a", due_at=NOW, owner="actor-1", on_expire="on-expire a"),
        _clock("clock-later", due_at=NOW + 1, on_expire="intent-1"),
        _clock("clock-expired", due_at=NOW - 100, status="expired", on_expire="intent-1"),
        _clock("clock-resolved", due_at=NOW - 100, status="resolved", on_expire="intent-1"),
    )

    candidates = expire_clocks(runtime, now=NOW)

    # Due order: the earlier clock first.
    assert [(candidate.candidate_id, candidate.origin) for candidate in candidates] == [
        ("clock:clock-b", "clock"), ("clock:clock-a", "clock"),
    ]
    named, texted = candidates
    # An on_expire naming an intent brings the intent forward to now instead of making an event.
    assert named.effects == WorldEffects(entity_changes=[
        EffectEntityChange(ref="clock-b", field="status", new_value="expired"),
        EffectEntityChange(ref="intent-1", field="scheduled_at", new_value=NOW),
    ])
    assert named.summary == "label clock-b"
    assert texted.effects == WorldEffects(
        entity_changes=[EffectEntityChange(ref="clock-a", field="status", new_value="expired")],
        events=[EffectEvent(
            summary="label clock-a: on-expire a", participant_refs=["actor-1"], location_id=_site(2),
            visibility="public", at=NOW,
        )],
    )
    assert texted.summary == "label clock-a: on-expire a"


def test_expired_secret_clock_event_is_secret() -> None:
    runtime = _runtime(
        _actor("actor-1", location=_site(3)),
        _actor("actor-dead", location=_site(4), alive_status="dead"),
        _clock("clock-secret", due_at=NOW - 5, owner="actor-1", scope="secret", on_expire="on-expire secret"),
        _clock("clock-ownerless", due_at=NOW - 4, scope="public", on_expire="on-expire ownerless"),
        _clock("clock-dead-owner", due_at=NOW - 3, owner="actor-dead", scope="public", on_expire="on-expire dead"),
    )

    events = [candidate.effects.events for candidate in expire_clocks(runtime, now=NOW)]

    assert events == [
        [EffectEvent(
            summary="label clock-secret: on-expire secret", participant_refs=["actor-1"], location_id=_site(3),
            visibility="secret", at=NOW,
        )],
        [EffectEvent(
            summary="label clock-ownerless: on-expire ownerless", participant_refs=[], location_id="",
            visibility="public", at=NOW,
        )],
        # A dead owner takes part in nothing; the event still happens where the owner lies.
        [EffectEvent(
            summary="label clock-dead-owner: on-expire dead", participant_refs=[], location_id=_site(4),
            visibility="public", at=NOW,
        )],
    ]


def test_clock_event_happens_when_the_clock_ran_out_within_the_window() -> None:
    runtime = _runtime(
        _actor("actor-1", location=_site(1)),
        _clock("clock-in-window", due_at=NOW - 30, scope="public", on_expire="on-expire in window"),
        _clock("clock-before-window", due_at=NOW - 500, scope="public", on_expire="on-expire before window"),
        _clock("clock-named", due_at=NOW - 20, on_expire="intent-1"),
        _intent("intent-1", "actor-1", at=NOW + 600),
    )

    candidates = expire_clocks(runtime, now=NOW, window_start=NOW - 100)

    # A clock that ran out inside the window happens then; one left running from before it lands at its start.
    assert [(candidate.candidate_id, [event.at for event in candidate.effects.events]) for candidate in candidates] == [
        ("clock:clock-before-window", [NOW - 100]),
        ("clock:clock-in-window", [NOW - 30]),
        ("clock:clock-named", []),
    ]
    # A named intent is still brought forward to now.
    assert candidates[2].effects.entity_changes[1] == EffectEntityChange(
        ref="intent-1", field="scheduled_at", new_value=NOW,
    )


# intents


def test_intent_interactions_disrupt_and_resolve() -> None:
    runtime = _runtime(
        _actor("actor-1"),
        _intent("intent-a", "actor-1", at=NOW),
        _intent("intent-b", "actor-1", at=NOW, status="due", onstage=True),
        _intent("intent-c", "actor-1", at=NOW),
        # The chapter's own delta already set this one's status.
        _intent("intent-d", "actor-1", at=NOW, status="disrupted"),
        _intent("intent-e", "actor-1", at=NOW),
    )

    candidates = apply_intent_interactions(runtime, interactions=[
        _interaction("intent-a", "disrupted"),
        _interaction("intent-b", "fulfilled"),
        _interaction("intent-c", "advanced"),
        _interaction("intent-d", "disrupted"),
        _interaction("intent-e", "disrupted"),
        # The chapter's last effect on an intent wins, as in the chapter's delta.
        _interaction("intent-e", "fulfilled"),
        _interaction("actor-1", "disrupted"),
        _interaction("intent-missing", "disrupted"),
    ])

    assert [
        (candidate.candidate_id, candidate.origin, candidate.actor_id, candidate.intent_id,
         candidate.intent_status_changes, candidate.summary)
        for candidate in candidates
    ] == [
        ("interaction:intent-a", "intent_interaction", "actor-1", "intent-a", {"intent-a": "disrupted"},
         "evidence intent-a"),
        ("interaction:intent-b", "intent_interaction", "actor-1", "intent-b", {"intent-b": "resolved"},
         "evidence intent-b"),
        ("interaction:intent-e", "intent_interaction", "actor-1", "intent-e", {"intent-e": "resolved"},
         "evidence intent-e"),
    ]
    assert all(candidate.effects == WorldEffects() for candidate in candidates)


def test_due_selection_orders_by_time_tier_and_id() -> None:
    runtime = _runtime(
        _actor("actor-major", tier="major"),
        _actor("actor-minor", tier="minor"),
        _actor("actor-background", tier="background"),
        _intent("intent-early", "actor-background", at=NOW - 100),
        _intent("intent-3", "actor-background", at=NOW - 50),
        _intent("intent-2b", "actor-minor", at=NOW - 50),
        _intent("intent-2a", "actor-minor", at=NOW - 50),
        _intent("intent-1", "actor-major", at=NOW - 50),
        _intent("intent-ownerless", "", at=NOW - 50),
        _intent("intent-later", "actor-major", at=NOW + 1),
        _intent("intent-resolved", "actor-major", at=NOW - 50, status="resolved"),
        _intent("intent-onstage", "actor-minor", at=NOW, onstage=True),
        _intent("intent-onstage-major", "actor-major", at=NOW, onstage=True),
        _intent("intent-due", "actor-minor", at=NOW - 10, status="due", onstage=True),
        _intent("intent-due-early", "actor-minor", at=NOW - 20, status="due", onstage=True),
    )

    due = select_due_intents(runtime, now=NOW)

    assert [node.id for node in due.offstage] == [
        "intent-early", "intent-1", "intent-2a", "intent-2b", "intent-3", "intent-ownerless",
    ]
    assert [node.id for node in due.onstage] == ["intent-onstage-major", "intent-onstage"]
    # Intents already due are the bookkeeping's other input, in the same order.
    assert [node.id for node in due_onstage_intents(runtime)] == ["intent-due-early", "intent-due"]


def test_onstage_intent_slips_until_conflict() -> None:
    runtime = _runtime(
        _actor("actor-1"),
        _intent("intent-1", "actor-1", at=NOW - 60, onstage=True, impact="major"),
        _intent("intent-touched", "actor-1", at=NOW - 60, status="due", onstage=True),
    )
    # The chapter shows the touched intent, so planning took it up and it does not slip.
    interactions = [_interaction("intent-touched", "advanced")]

    def bookkeeping(now: int) -> OnstageResult:
        result = onstage_bookkeeping(
            runtime,
            onstage_due=select_due_intents(runtime, now=now).onstage,
            already_due=due_onstage_intents(runtime),
            interactions=interactions,
            now=now,
            max_slips=2,
            chapter_number=7,
        )
        _commit_bookkeeping(runtime, result.candidates, now)
        return result

    def changes(result: OnstageResult) -> list[tuple[str, str, dict[str, str], dict[str, int]]]:
        return [
            (candidate.candidate_id, candidate.origin, candidate.intent_status_changes, candidate.slip_updates)
            for candidate in result.candidates
        ]

    first = bookkeeping(NOW)
    assert changes(first) == [("onstage:intent-1", "onstage", {"intent-1": "due"}, {})]
    assert first.conflicts == []
    # A bookkeeping change is not itself a major world change.
    assert first.candidates[0].impact_level == "minor"

    for slips, now in ((1, NOW + 100), (2, NOW + 200)):
        result = bookkeeping(now)
        assert changes(result) == [("onstage:intent-1", "onstage", {}, {"intent-1": slips})]
        assert result.conflicts == []
        assert runtime.world.get_state("intent-1") == {"status": "due", "scheduled_at": now, "slip_count": slips}

    third = bookkeeping(NOW + 300)
    # The third slip passes max_slips=2: it is held in a conflict, not committed.
    assert third.candidates == []
    held = TickCandidate(
        candidate_id="onstage:intent-1", origin="onstage", actor_id="actor-1", intent_id="intent-1",
        summary="summary intent-1", effects=WorldEffects(), slip_updates={"intent-1": 3},
    )
    assert third.conflicts == [ConflictDraft(
        project_id=PROJECT,
        source="world_tick",
        detected_after_chapter=7,
        story_time=NOW + 300,
        involved_refs=["intent-1", "actor-1"],
        violated="onstage_slip",
        impact_level="major",
        summary="summary intent-1",
        proposed_change=held.model_dump(mode="json"),
        intent_ref="intent-1",
        actor_ref="actor-1",
        blocks_from_chapter=8,
    )]
    assert runtime.world.get_state("intent-1")["slip_count"] == 2


# news


def test_public_news_reaches_far_actor_later_and_queues() -> None:
    # site 1 -1h- site 2; site 1 -4h- site 3 -6h- site 4; site 5 is cut off.
    runtime = _runtime(
        _actor("actor-participant", location=_site(3)),
        _actor("actor-bystander", location=_site(1)),
        _actor("actor-near", location=_site(2)),
        _actor("actor-far", location=_site(4)),
        _actor("actor-cut-off", location=_site(5)),
        _actor("actor-nowhere"),
        _actor("actor-dead", location=_site(2), alive_status="dead"),
        _actor("actor-knower", location=_site(2)),
        _actor("actor-believer", location=_site(2)),
        _actor("actor-heard", location=_site(2)),
        edges=[(1, 2, 1.0), (1, 3, 4.0), (3, 4, 6.0)],
        knowledge={
            "actor-knower": {"fact:fact-1": "knows"},
            "actor-believer": {"fact:fact-1": "believes"},
            "actor-heard": {"fact:fact-1": "heard"},
        },
    )
    # A four-hour tick window; news travels twice as fast as people.
    start = NOW - 240
    event = EffectEvent(
        summary="event", participant_refs=["actor-participant"], location_id=_site(1), visibility="public", at=start,
    )

    result = propagate_news(
        runtime, BookLexicon(news_speed_multiplier=2.0),
        events=[event], in_transit=[], now=NOW, fact_ref_for_event={0: "fact:fact-1"},
    )

    assert result.knowledge == [
        # Those who took part or stood there witness it.
        _witnessed("actor-bystander", "fact:fact-1"),
        _witnessed("actor-participant", "fact:fact-1"),
        # One hour away is thirty minutes of news: heard before now.
        _heard("actor-near", "fact:fact-1"),
    ]
    # Ten hours away is 300 minutes of news, past now: the news waits in the queue.
    assert result.in_transit == [NewsItem(fact_ref="fact:fact-1", observer_ref="actor-far", arrive_at=start + 300)]


def test_secret_event_only_reaches_participants_and_witnesses() -> None:
    runtime = _runtime(
        _actor("actor-participant"),
        _actor("actor-witness", location=_site(1)),
        _actor("actor-believer", location=_site(1)),
        _actor("actor-near", location=_site(2)),
        _actor("actor-dead", location=_site(1), alive_status="dead"),
        WorldNode(id="item-1", project_id=PROJECT, node_type="item", name="item-1"),
        edges=[(1, 2, 1.0)],
        knowledge={"actor-believer": {"fact:fact-1": "believes"}},
    )
    event = EffectEvent(
        summary="event", participant_refs=["actor-participant", "item-1"], location_id=_site(1),
        visibility="secret", at=NOW - 120,
    )

    result = propagate_news(
        runtime, BookLexicon(), events=[event], in_transit=[], now=NOW, fact_ref_for_event={0: "fact:fact-1"},
    )

    assert result.knowledge == [
        _witnessed("actor-participant", "fact:fact-1"),
        _witnessed("actor-witness", "fact:fact-1"),
    ]
    assert result.in_transit == []


def test_queue_delivers_arrived_news_and_drops_what_is_settled() -> None:
    runtime = _runtime(
        _actor("actor-1", location=_site(1)),
        _actor("actor-2", location=_site(2)),
        _actor("actor-knower", location=_site(2)),
        _actor("actor-dead", location=_site(2), alive_status="dead"),
        edges=[(1, 2, 3.0)],
        facts=["fact-1", "fact-2"],
        knowledge={"actor-knower": {"fact:fact-1": "knows", "fact:fact-2": "knows"}},
    )

    result = propagate_news(
        runtime, BookLexicon(),
        events=[EffectEvent(summary="event", location_id=_site(1), visibility="public", at=NOW - 60)],
        in_transit=[
            NewsItem(fact_ref="fact:fact-2", observer_ref="actor-2", arrive_at=NOW + 50),
            NewsItem(fact_ref="fact:fact-1", observer_ref="actor-2", arrive_at=NOW),
            NewsItem(fact_ref="fact:fact-1", observer_ref="actor-knower", arrive_at=NOW - 10),
            NewsItem(fact_ref="fact:fact-1", observer_ref="actor-dead", arrive_at=NOW - 10),
            NewsItem(fact_ref="fact:fact-1", observer_ref="actor-gone", arrive_at=NOW - 10),
            NewsItem(fact_ref="fact:fact-gone", observer_ref="actor-1", arrive_at=NOW - 10),
        ],
        now=NOW,
        fact_ref_for_event={0: "fact:fact-2"},
    )

    assert result.knowledge == [
        _heard("actor-2", "fact:fact-1"),
        _witnessed("actor-1", "fact:fact-2"),
    ]
    # The event's own news to actor-2 arrives at NOW + 120; the queued copy arrives sooner and stays.
    assert result.in_transit == [NewsItem(fact_ref="fact:fact-2", observer_ref="actor-2", arrive_at=NOW + 50)]


# a tick's events and the database


def test_tick_event_fact_ref_names_the_fact_the_builder_creates() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    chapter = _chapter_one(Session, world)
    context = _context(world, chapter)
    event = {
        "summary": "event-summary", "participant_refs": [world.hero], "location_id": world.loc(1),
        "visibility": "public", "at": context.now,
    }
    candidates = [
        _candidate("c-1", effects={"events": [event]}),
        _candidate("c-2", effects={"events": [event, {**event, "visibility": "secret"}]}),
    ]

    delta = _built(Session, context, candidates, tick_id="tick-1")

    refs = [ref for _event_item, ref in tick_events(world.project_id, "tick-1", candidates)]
    assert refs == [
        tick_event_fact_ref(world.project_id, "tick-1", "c-1", 0),
        tick_event_fact_ref(world.project_id, "tick-1", "c-2", 0),
        tick_event_fact_ref(world.project_id, "tick-1", "c-2", 1),
    ]
    assert len(set(refs)) == 3
    assert [f"fact:{patch.fact_id}" for patch in delta.fact_patches] == refs
    assert [
        patch.new_value["state"]["result_refs"] for patch in delta.node_patches if patch.node_type == "event"
    ] == [[ref] for ref in refs]
    engine.dispose()


def _seed_steps_world(Session, monkeypatch: pytest.MonkeyPatch) -> _World:
    """The gate world plus a hero agenda, an onstage intent due in an hour, a later intent and a public clock."""
    original = test_world_gate_v6._pack

    def pack(**kwargs: Any) -> dict[str, Any]:
        built = original(**kwargs)
        seed = built["world_seed"]
        intent = {
            "agenda_id": "agenda-1", "action_type": "confront", "target_refs": ["opp-1"], "location_ref": "node-1",
            "impact_level": "moderate",
        }
        seed["actors"][0]["agendas"] = [{
            "id": "agenda-1", "goal": "goal-1", "motivation": "", "horizon": "", "priority": 3,
            "means_refs": [], "constraints": [], "secrecy_refs": [],
        }]
        seed["actors"][0]["intents"] = [
            {**intent, "id": "intent-onstage", "summary": "summary-onstage", "requires_onstage": True,
             "scheduled_in": {"amount": 1, "unit": "时"}},
            {**intent, "id": "intent-later", "summary": "summary-later", "requires_onstage": False,
             "scheduled_in": {"amount": 3, "unit": "日"}},
        ]
        seed["clocks"] = [{
            "id": "clock-1", "label": "label-1", "owner_ref": "cast-1", "scope": "public",
            "on_expire": "on-expire-1", "due_in": {"amount": 2, "unit": "时"},
        }]
        return built

    monkeypatch.setattr(test_world_gate_v6, "_pack", pack)
    return _seed_world(Session)


def _tick_inputs(Session, world: _World, chapter: int) -> tuple[BookStateRuntime, BookLexicon, list[NewsItem]]:
    with Session() as session:
        return (
            BookStateProjection(session).load_runtime_as_of(world.project_id, as_of_chapter=chapter),
            BookLexiconRepository(session).get(world.project_id),
            latest_news_in_transit(session, world.project_id),
        )


def _news_candidate(knowledge: list[EffectKnowledge]) -> TickCandidate:
    return TickCandidate(
        candidate_id="propagation", origin="propagation", summary="news", effects=WorldEffects(knowledge=knowledge),
    )


def test_queued_news_is_delivered_by_a_later_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, Session = _session_factory()
    world = _seed_steps_world(Session, monkeypatch)
    onstage_id = world.node("intent", "cast-1/intent-onstage")
    later_id = world.node("intent", "cast-1/intent-later")
    chapter_one = _delta(Session, world, 1, {
        **_time(1, "日"),
        # The rival does something in public at loc 2, two hours from the hero and the faction at loc 1.
        "events": [_event([world.rival], location_ref=world.loc(2))],
        "intent_interactions": [{"intent_ref": later_id, "effect": "disrupted", "evidence": "e"}],
    })
    _commit(Session, world, chapter_one)
    now = chapter_one.story_time_end
    runtime, lexicon, queue = _tick_inputs(Session, world, 1)
    interactions = chapter_one.metadata["intent_interactions"]

    clocks = expire_clocks(runtime, now=now)
    due = select_due_intents(runtime, now=now)
    onstage = onstage_bookkeeping(
        runtime, onstage_due=due.onstage, already_due=due_onstage_intents(runtime),
        interactions=interactions, now=now, max_slips=2, chapter_number=1,
    )
    sources = [*chapter_events(chapter_one), *tick_events(world.project_id, "tick-1", clocks)]
    news = propagate_news(
        runtime, lexicon, events=[event for event, _ref in sources], in_transit=queue, now=now,
        fact_ref_for_event={index: ref for index, (_event_item, ref) in enumerate(sources)},
    )

    # The chapter's own delta already disrupted the later intent.
    assert apply_intent_interactions(runtime, interactions=interactions) == []
    assert (queue, due.offstage, [node.id for node in due.onstage]) == ([], [], [onstage_id])
    [(chapter_event, chapter_fact)] = chapter_events(chapter_one)
    assert chapter_event == EffectEvent(
        summary="event-summary", participant_refs=[world.rival], location_id=world.loc(2), visibility="public", at=now,
    )
    [chapter_event_patch] = [patch for patch in chapter_one.node_patches if patch.node_type == "event"]
    assert chapter_fact == chapter_event_patch.new_value["state"]["result_refs"][0]
    clock_fact = tick_event_fact_ref(world.project_id, "tick-1", f"clock:{world.node('clock', 'clock-1')}", 0)
    # Witnesses come per event in id order: the hero's character id sorts before the faction's.
    assert news.knowledge == [
        _witnessed(world.rival, chapter_fact),
        _witnessed(world.hero, clock_fact),
        _witnessed(world.faction, clock_fact),
    ]
    arrive = now + 120
    assert news.in_transit == sorted([
        NewsItem(fact_ref=chapter_fact, observer_ref=world.faction, arrive_at=arrive),
        NewsItem(fact_ref=chapter_fact, observer_ref=world.hero, arrive_at=arrive),
        NewsItem(fact_ref=clock_fact, observer_ref=world.rival, arrive_at=arrive),
    ], key=lambda item: (item.arrive_at, item.fact_ref, item.observer_ref))

    tick_one = _built(
        Session, _context(world, chapter_one), [*clocks, *onstage.candidates, _news_candidate(news.knowledge)],
        tick_id="tick-1", news=news.in_transit,
    )
    assert _codes(_review(Session, world, tick_one, base_chapter=1)) == []
    _commit_tick(Session, world, tick_one, tick_id="tick-1")

    chapter_two = _delta(Session, world, 2, _time(1, "日"))
    _commit(Session, world, chapter_two)
    later = chapter_two.story_time_end
    runtime, lexicon, queue = _tick_inputs(Session, world, 2)
    assert queue == news.in_transit
    assert expire_clocks(runtime, now=later) == []

    onstage = onstage_bookkeeping(
        runtime, onstage_due=select_due_intents(runtime, now=later).onstage,
        already_due=due_onstage_intents(runtime), interactions=chapter_two.metadata["intent_interactions"],
        now=later, max_slips=2, chapter_number=2,
    )
    news = propagate_news(runtime, lexicon, events=[], in_transit=queue, now=later, fact_ref_for_event={})

    # Everything queued has arrived, in queue order.
    assert news.knowledge == [_heard(item.observer_ref, item.fact_ref) for item in queue]
    assert news.in_transit == []
    tick_two = _built(
        Session, _context(world, chapter_two), [*onstage.candidates, _news_candidate(news.knowledge)],
        tick_id="tick-2", news=news.in_transit,
    )
    assert _codes(_review(Session, world, tick_two, base_chapter=2)) == []
    _commit_tick(Session, world, tick_two, tick_id="tick-2", chapter=2)

    runtime, _lexicon, queue = _tick_inputs(Session, world, 2)
    assert queue == []
    rival = runtime.cognition_by_observer[("character", world.rival)]
    assert (rival.knowledge_by_ref[chapter_fact], rival.knowledge_by_ref[clock_fact]) == ("knows", "heard")
    # The onstage intent went unplanned in chapter 2 and slipped once.
    assert runtime.world.get_state(onstage_id) == {"status": "due", "scheduled_at": later, "slip_count": 1}
    engine.dispose()
