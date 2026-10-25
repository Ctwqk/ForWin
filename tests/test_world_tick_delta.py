from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy import select

from forwin.audit.events import DecisionEventType
from forwin.book_state import reviewer
from forwin.book_state.knowledge import KnowledgeEvent, knowledge_event_to_patch
from forwin.book_state.projection import BookStateProjection
from forwin.canon import CanonAdmissionService, CanonStaleVersion, CanonWorldTickOutcome, CanonWriteFailure
from forwin.models.audit import DecisionEvent
from forwin.models.outbox import OutboxEvent
from forwin.protocol.book_state import ApprovedGraphDeltaSet, GraphDelta, NodePatch
from forwin.world_engine.delta_builder import TickDeltaBuild, TickIssue, WorldTickDeltaBuilder
from forwin.world_engine.types import WORLD_ENGINE_SOURCE_TYPE, NewsItem, TickCandidate, TickContext, tick_seed
from tests.test_world_gate_v6 import (
    _codes,
    _commit,
    _delta,
    _knowledge,
    _move,
    _review,
    _seed_world,
    _session_factory,
    _time,
    _World,
)


def _chapter_one(Session, world: _World, fields: dict[str, Any] | None = None) -> GraphDelta:
    """Commit a one-day chapter 1; the tick then runs on its committed state."""
    delta = _delta(Session, world, 1, {**_time(1, "日"), **(fields or {})})
    _commit(Session, world, delta)
    return delta


def _context(world: _World, chapter: GraphDelta) -> TickContext:
    return TickContext(
        project_id=world.project_id,
        chapter_number=chapter.chapter_number,
        now=chapter.story_time_end,
        window_start=chapter.story_time_start,
        seed=tick_seed(world.project_id, chapter.chapter_number),
    )


def _candidate(candidate_id: str, **fields: Any) -> TickCandidate:
    return TickCandidate.model_validate({
        "candidate_id": candidate_id, "origin": "adjudication", "summary": f"summary {candidate_id}",
        "effects": {}, **fields,
    })


def _intent(**overrides: Any) -> dict[str, Any]:
    return {
        "agenda_ref": "", "action_type": "negotiate", "summary": "intent-summary", "target_refs": [],
        "location_id": "", "scheduled_in": {"amount": 1, "unit": "时"}, "requires_onstage": False,
        "impact_level": "minor", "expected_effects": {}, **overrides,
    }


def _walk(entity: str, to_location: str, depart: int, minutes: int) -> dict[str, Any]:
    return {"entity_ref": entity, "to_location_id": to_location, "depart_at": depart, "arrive_at": depart + minutes}


def _build(
    Session,
    context: TickContext,
    candidates: Sequence[TickCandidate],
    *,
    tick_id: str = "tick-1",
    news: Sequence[NewsItem] = (),
) -> TickDeltaBuild:
    with Session() as session:
        return WorldTickDeltaBuilder(session).build(
            context=context, candidates=candidates, news_in_transit=news, tick_id=tick_id,
        )


def _built(Session, context: TickContext, candidates: Sequence[TickCandidate], **kwargs: Any) -> GraphDelta:
    build = _build(Session, context, candidates, **kwargs)
    assert build.delta is not None, build.issues
    return build.delta


def _commit_tick(Session, world: _World, delta: GraphDelta, *, tick_id: str, chapter: int = 1) -> CanonWorldTickOutcome:
    with Session.begin() as session:
        return CanonAdmissionService().commit_world_tick(
            session=session,
            project_id=world.project_id,
            chapter_number=chapter,
            approved_changes=ApprovedGraphDeltaSet(
                project_id=world.project_id, chapter_number=chapter, graph_deltas=[delta],
            ),
            tick_id=tick_id,
        )


def _issues(build: TickDeltaBuild) -> list[tuple[str, str, str]]:
    return [(issue.severity, issue.code, issue.candidate_id) for issue in build.issues]


def _patches(delta: GraphDelta) -> list[tuple[str, str, str, Any, Any]]:
    return [
        (patch.node_id, str(patch.op), patch.field_path, patch.old_value, patch.new_value)
        for patch in delta.node_patches if str(patch.op) != "create"
    ]


@pytest.mark.parametrize("node_type", ["character", "group"])
def test_adjudicated_outcome_builds_minor_actor_nodes(node_type):
    from forwin.protocol.book_state import WorldNode
    from forwin.world_engine.delta_builder import _TickBuilder
    from tests.test_world_adjudicator import CALENDAR, CONTEXT, LEXICON, _runtime
    from tests.test_world_engine_contracts import _new_actor

    runtime = _runtime()
    runtime.world.add_node(WorldNode(id="faction", project_id="p", node_type="faction", name="Faction"))
    candidate = _candidate("arrival", effects={
        "new_actors": [_new_actor(node_type=node_type, affiliation_refs=["faction"])],
        "relation_changes": [{"source_ref": "actor:new:arrival", "target_ref": "owner",
                              "edge_type": "trusts", "change": "create"}],
        "item_transfers": [{"item_ref": "item", "from_ref": "owner", "to_ref": "actor:new:arrival"}],
        "events": [{"summary": "Arrival", "participant_refs": ["actor:new:arrival"],
                    "location_id": "site", "visibility": "public", "at": CONTEXT.now}],
        "new_facts": [{"key": "known", "proposition": "A fact.", "truth_value": True,
                       "sensitivity": "hidden", "known_by": ["actor:new:arrival"]}],
        "knowledge": [{"observer_ref": "owner", "fact_ref": "fact:new:known", "state": "knows",
                       "channel": "told", "source_ref": "actor:new:arrival"}],
    })

    def build():
        return _TickBuilder(context=CONTEXT, tick_id="tick-1", runtime=runtime,
                            calendar=CALENDAR, lexicon=LEXICON).build([candidate], [])

    result = build()
    assert result.delta is not None, result.issues
    from forwin.book_state.schema import validate_graph_delta
    assert validate_graph_delta(result.delta, strict=True).ok
    assert result.delta.model_dump() == build().delta.model_dump()
    [actor] = [p for p in result.delta.node_patches if p.op == "create" and p.node_type == node_type]
    assert actor.new_value["state"]["actor_tier"] == "minor"
    assert actor.new_value["state"]["next_turn_at"] == CONTEXT.now
    if node_type == "character":
        assert actor.new_value["state"]["alive_status"] == "alive"
    assert actor.new_value["state"]["location_id"] == "site"
    assert {(p.source_id, p.target_id, p.edge_type) for p in result.delta.edge_patches} == {
        (actor.node_id, "faction", "member_of"), (actor.node_id, "owner", "trusts"),
    }
    [transfer] = [p for p in result.delta.node_patches if p.node_id == "item"]
    assert (transfer.old_value, transfer.new_value) == ("owner", actor.node_id)
    assert all("old_value" in p.model_fields_set for p in result.delta.node_patches if p.op != "create")
    assert all("old_value" in p.model_fields_set for p in result.delta.cognition_patches)
    assert "actor:new:" not in json.dumps(result.delta.model_dump(mode="json"))


@pytest.mark.parametrize("source", ["node_name", "node_alias", "map_alias", "reserved"])
def test_new_actor_name_collision_is_a_build_error(source):
    from forwin.world_engine.delta_builder import _TickBuilder
    from tests.test_world_adjudicator import CALENDAR, CONTEXT, LEXICON, _runtime
    from tests.test_world_engine_contracts import _new_actor

    runtime = _runtime()
    reserved = []
    if source == "node_name":
        runtime.world.get_node("owner").name = "New Actor"
    elif source == "node_alias":
        runtime.world.get_node("owner").aliases = ["New Actor"]
    elif source == "map_alias":
        runtime.map.nodes_by_id["site"].aliases = ["New Actor"]
    else:
        reserved = ["New Actor"]
    result = _TickBuilder(context=CONTEXT, tick_id="tick-1", runtime=runtime,
                          calendar=CALENDAR, lexicon=LEXICON, reserved_actor_names=reserved).build([
        _candidate("arrival", effects={"new_actors": [_new_actor()]}),
    ], [])
    assert result.delta is None
    assert _issues(result) == [("error", "new_actor_name_taken", "arrival")]


@pytest.mark.parametrize("node_type", ["character", "group"])
def test_new_actor_creation_passes_world_gate_without_database(node_type):
    from forwin.protocol.book_state import WorldNode
    from forwin.world_engine.delta_builder import _TickBuilder
    from tests.test_world_adjudicator import CALENDAR, CONTEXT, LEXICON, _runtime
    from tests.test_world_engine_contracts import _new_actor

    runtime = _runtime()
    runtime.world.add_node(WorldNode(id="faction", project_id="p", node_type="faction"))
    built = _TickBuilder(context=CONTEXT, tick_id="tick-1", runtime=runtime,
                         calendar=CALENDAR, lexicon=LEXICON).build([
        _candidate("arrival", effects={"new_actors": [_new_actor(node_type=node_type, affiliation_refs=["faction"])]}),
    ], [])
    assert built.delta is not None, built.issues
    verdict = reviewer.BookStateReviewGate(None).review(
        ApprovedGraphDeltaSet(project_id="p", chapter_number=CONTEXT.chapter_number, graph_deltas=[built.delta]),
        runtime=runtime, base_chapter=CONTEXT.chapter_number,
    )
    assert verdict.accepted, verdict.issues


@pytest.mark.parametrize("source", ["entity_name", "entity_alias", "entity_aliases_json"])
def test_new_actor_entity_name_collision_postgres(source):
    from forwin.models.entity import Entity, EntityAlias
    from tests.test_world_engine_contracts import _new_actor

    engine, Session = _session_factory()
    world = _seed_world(Session)
    chapter = _chapter_one(Session, world)
    with Session.begin() as session:
        entity = Entity(project_id=world.project_id, kind="character",
                        name="New Actor" if source == "entity_name" else "Registered Actor",
                        aliases_json=json.dumps(["New Actor"] if source == "entity_aliases_json" else []))
        session.add(entity)
        session.flush()
        if source == "entity_alias":
            session.add(EntityAlias(project_id=world.project_id, entity_id=entity.id, alias="New Actor"))
    result = _build(Session, _context(world, chapter), [
        _candidate("arrival", effects={"new_actors": [_new_actor(location_id=world.loc(1))]}),
    ])
    assert result.delta is None
    assert _issues(result) == [("error", "new_actor_name_taken", "arrival")]
    engine.dispose()


def test_builder_writes_gate_compatible_movement_metadata() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    item_transfer = {
        "new_item": {"name": "item-a", "kind": "item"}, "from_ref": "", "to_ref": world.hero, "evidence": "e",
    }
    chapter = _chapter_one(Session, world, {"item_transfers": [item_transfer]})
    item = chapter.metadata["item_transfers"][0]["item_id"]
    context = _context(world, chapter)
    start = context.window_start
    told = {
        "observer_ref": world.rival, "fact_ref": world.fact("fact-public"), "state": "knows", "channel": "told",
        "source_ref": world.hero,
    }

    def candidate(minutes: int, depart: int = start) -> TickCandidate:
        # loc 1 -> loc 2 is a 2 h road the hero walks in 120 minutes.
        return _candidate("c-1", impact_level="moderate", effects={
            "movements": [_walk(world.hero, world.loc(2), depart, minutes)],
            "item_transfers": [{"item_ref": item, "from_ref": world.hero, "to_ref": world.rival}],
            "knowledge": [told],
        })

    build = _build(Session, context, [candidate(120)], news=[
        NewsItem(fact_ref=world.fact("fact-public"), observer_ref=world.faction, arrive_at=context.now + 30),
    ])
    delta = build.delta

    assert build.issues == []
    assert delta is not None
    assert (delta.source_type, delta.chapter_number, delta.story_time_start, delta.story_time_end) == (
        WORLD_ENGINE_SOURCE_TYPE, 1, context.window_start, context.now,
    )
    assert delta.metadata["movements"] == [{
        "entity_id": world.hero, "from_id": world.loc(1), "to_id": world.loc(2), "depart_minute": start,
        "arrive_minute": start + 120, "travel_mode_id": "", "evidence": "summary c-1",
    }]
    assert delta.metadata["item_transfers"] == [
        {"item_id": item, "from_id": world.hero, "to_id": world.rival, "evidence": "summary c-1"},
    ]
    assert delta.metadata["knowledge_tags"] == [{
        "observer_type": "character", "observer_id": world.rival, "fact_ref": world.fact("fact-public"),
        "state": "knows", "channel": "told", "source_id": world.hero, "touches_secret_ref": "",
        "contradicts_ref": "", "flashback": False, "evidence": "summary c-1",
    }]
    assert {key: delta.metadata[key] for key in (
        "tick_id", "seed", "source_chapter", "candidate_ids", "candidate_impacts", "news_in_transit",
    )} == {
        "tick_id": "tick-1",
        "seed": context.seed,
        "source_chapter": 1,
        "candidate_ids": ["c-1"],
        "candidate_impacts": {"c-1": "moderate"},
        "news_in_transit": [
            {"fact_ref": world.fact("fact-public"), "observer_ref": world.faction, "arrive_at": context.now + 30},
        ],
    }
    # The entries carry exactly the keys the translator writes for a chapter.
    translated = _delta(Session, world, 2, {
        **_time(1, "日"),
        "movements": [_move(world.hero, world.loc(1), world.loc(2))],
        "item_transfers": [{"item_ref": item, "from_ref": world.hero, "to_ref": world.rival, "evidence": "e"}],
        "knowledge_events": [_knowledge(world.rival, world.fact("fact-public"), channel="told", source_ref=world.hero)],
    })
    for key in ("movements", "item_transfers", "knowledge_tags"):
        assert set(delta.metadata[key][0]) == set(translated.metadata[key][0])
    assert _patches(delta) == [
        (world.hero, "set", "state.location_id", world.loc(1), world.loc(2)),
        (item, "set", "state.holder_id", world.hero, world.rival),
    ]
    assert [(patch.observer_id, patch.old_value, patch.new_value) for patch in delta.cognition_patches] == [
        (world.rival, {world.fact("fact-public"): None}, {world.fact("fact-public"): "knows"}),
    ]
    # The gate reads the tick's movements: arriving in an hour outruns the road.
    assert _codes(_review(Session, world, delta, base_chapter=1)) == []
    too_fast = _built(Session, context, [candidate(60)])
    assert _codes(_review(Session, world, too_fast, base_chapter=1)) == [("error", "movement_too_fast")]
    # The tick's window is its story_time_start to story_time_end: arriving after now is out of range.
    late = _built(Session, context, [candidate(120, depart=context.now - 60)])
    assert _codes(_review(Session, world, late, base_chapter=1)) == [("error", "time_offset_out_of_range")]
    engine.dispose()


def test_builder_creates_intents_with_absolute_schedule_from_calendar_units() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    context = _context(world, _chapter_one(Session, world))
    now = context.now
    turn = _candidate(
        "c-1",
        origin="actor_turn",
        actor_id=world.hero,
        agenda_changes=[{"agenda_ref": "new", "goal": "goal-1", "motivation": "motivation-1", "priority": 9}],
        new_intents=[
            _intent(
                agenda_ref="new", scheduled_in={"amount": 2, "unit": "时"},
                target_refs=[world.rival, world.fact("fact-public"), world.loc(3)], location_id=world.loc(2),
            ),
            # A lexicon alias: one 刻 is 15 minutes.
            _intent(scheduled_in={"amount": 3, "unit": "刻"}),
            # A negative or zero wait schedules the intent now.
            _intent(scheduled_in={"amount": -1, "unit": "日"}),
            _intent(scheduled_in={"amount": 0, "unit": "日"}),
        ],
        next_turn_at={world.hero: now + 60},
    )

    delta = _built(Session, context, [turn])
    agenda = next(patch for patch in delta.node_patches if patch.node_type == "agenda")
    intents = [patch for patch in delta.node_patches if patch.node_type == "intent"]

    assert agenda.new_value["profile"] == {"owner_ref": world.hero, "goal": "goal-1", "motivation": "motivation-1"}
    # Priority is clamped to the agenda scale 1-5.
    assert agenda.new_value["state"] == {
        "status": "active", "priority": 5, "means_refs": [], "constraints": [], "secrecy_refs": [],
    }
    assert [patch.new_value["state"] for patch in intents] == [
        {"status": "planned", "scheduled_at": now + 120, "slip_count": 0},
        {"status": "planned", "scheduled_at": now + 45, "slip_count": 0},
        {"status": "planned", "scheduled_at": now, "slip_count": 0},
        {"status": "planned", "scheduled_at": now, "slip_count": 0},
    ]
    first = intents[0].new_value["profile"]
    assert {key: first[key] for key in ("owner_ref", "agenda_ref", "target_refs", "location_id", "action_type")} == {
        "owner_ref": world.hero,
        "agenda_ref": agenda.node_id,
        "target_refs": [world.rival, world.fact("fact-public"), world.loc(3)],
        "location_id": world.loc(2),
        "action_type": "negotiate",
    }
    assert intents[1].new_value["profile"]["agenda_ref"] == ""
    assert len({patch.node_id for patch in intents}) == 4
    assert _patches(delta) == [(world.hero, "set", "state.next_turn_at", context.window_start, now + 60)]

    assert _commit_tick(Session, world, delta, tick_id="tick-1").idempotent is False
    follow_up = _candidate(
        "c-2",
        agenda_changes=[{"agenda_ref": agenda.node_id, "status": "achieved", "goal": "goal-2"}],
        intent_status_changes={intents[0].node_id: "due"},
        slip_updates={intents[1].node_id: 1},
    )
    updates = _built(Session, context, [follow_up], tick_id="tick-2")

    # A slip counts once more and reschedules the intent to now.
    assert _patches(updates) == [
        (agenda.node_id, "set", "state.status", "active", "achieved"),
        (agenda.node_id, "set", "profile.goal", "goal-1", "goal-2"),
        (intents[0].node_id, "set", "state.status", "planned", "due"),
        (intents[1].node_id, "set", "state.slip_count", 0, 1),
        (intents[1].node_id, "set", "state.scheduled_at", now + 45, now),
    ]
    unknown_unit = _build(Session, context, [
        _candidate("c-3", actor_id=world.hero, new_intents=[_intent(scheduled_in={"amount": 1, "unit": "fortnight"})]),
    ])
    assert unknown_unit.delta is None
    assert _issues(unknown_unit) == [("error", "invalid_duration", "c-3")]
    engine.dispose()


def test_builder_reports_unresolved_ref_per_candidate() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    context = _context(world, _chapter_one(Session, world))
    good = _candidate("c-good", effects={"movements": [_walk(world.hero, world.loc(2), context.window_start, 120)]})
    bad = _candidate(
        "c-bad",
        effects={
            "entity_changes": [{"ref": "node-missing", "field": "mood", "new_value": "calm"}],
            "movements": [_walk(world.rival, "loc-missing", context.window_start, 60)],
            "knowledge": [
                {"observer_ref": world.rival, "fact_ref": "fact:missing", "state": "knows", "channel": "rumor"},
            ],
        },
        intent_status_changes={"intent-missing": "due"},
    )
    # An intent needs an owner: the candidate names no actor.
    ownerless = _candidate("c-ownerless", new_intents=[_intent()])

    build = _build(Session, context, [good, bad, ownerless])
    rebuilt = _build(Session, context, [good])

    assert build.delta is None
    assert _issues(build) == [("error", "unresolved_ref", "c-bad")] * 4 + [("error", "unresolved_ref", "c-ownerless")]
    assert all(isinstance(issue, TickIssue) and issue.message for issue in build.issues)
    assert rebuilt.issues == []
    assert rebuilt.delta is not None
    assert rebuilt.delta.metadata["candidate_ids"] == ["c-good"]
    engine.dispose()


def test_builder_namespaces_new_facts_per_candidate() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    context = _context(world, _chapter_one(Session, world))

    def candidate(candidate_id: str, proposition: str) -> TickCandidate:
        # Both candidates name their new fact k1.
        return _candidate(candidate_id, effects={
            "new_facts": [{
                "key": "k1", "proposition": proposition, "truth_value": True, "sensitivity": "secret",
                "known_by": [world.hero],
            }],
            "knowledge": [
                {"observer_ref": world.rival, "fact_ref": "fact:new:k1", "state": "suspects", "channel": "inferred"},
            ],
        })

    delta = _built(Session, context, [candidate("c-1", "proposition-1"), candidate("c-2", "proposition-2")])
    first, second = (f"fact:{patch.fact_id}" for patch in delta.fact_patches)

    assert first != second
    assert [patch.proposition for patch in delta.fact_patches] == ["proposition-1", "proposition-2"]
    assert [(patch.observer_id, patch.new_value) for patch in delta.cognition_patches] == [
        (world.hero, {first: "knows"}),
        (world.rival, {first: "suspects"}),
        (world.hero, {second: "knows"}),
        (world.rival, {second: "suspects"}),
    ]
    engine.dispose()


def test_gate_flags_dead_actor_and_restrained_mover_in_world_engine_delta() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    # Chapter 1 kills the rival and imprisons the hero; the tick runs on that committed state.
    chapter = _chapter_one(Session, world, {"entity_changes": [
        {"ref": world.rival, "field": "alive_status", "new_value": "dead", "evidence": "e"},
        {"ref": world.hero, "field": "restraint.state", "new_value": "imprisoned", "evidence": "e"},
    ]})
    context = _context(world, chapter)
    start = context.window_start
    hero_walks = _walk(world.hero, world.loc(2), start, 120)
    moves = _built(Session, context, [_candidate("c-1", effects={
        "movements": [hero_walks, _walk(world.rival, world.loc(1), start, 120)],
    })])
    released = _built(Session, context, [_candidate("c-2", effects={
        "entity_changes": [{"ref": world.hero, "field": "restraint.state", "new_value": "free"}],
        "movements": [hero_walks],
    })])

    flagged = [("error", "restrained_entity_moved"), ("error", "dead_entity_acts")]
    assert _codes(_review(Session, world, moves, base_chapter=1)) == flagged
    # Without a base chapter the gate extends the compiled chapter, the same as-of N.
    assert _codes(_review(Session, world, moves)) == flagged
    assert _codes(_review(Session, world, released, base_chapter=1)) == []
    engine.dispose()


def test_gate_rejects_reader_knowledge_in_world_engine_delta() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    context = _context(world, _chapter_one(Session, world))
    public = world.fact("fact-public")
    delta = _built(Session, context, [_candidate("c-1", effects={
        "knowledge": [{"observer_ref": world.rival, "fact_ref": public, "state": "knows", "channel": "rumor"}],
    })])
    reader_patch = knowledge_event_to_patch(KnowledgeEvent(
        observer_type="reader", observer_id="reader", fact_ref=public, state="knows", channel="inferred",
    ))
    leaky = delta.model_copy(update={"cognition_patches": [*delta.cognition_patches, reader_patch]})
    chapter_reveal = _delta(Session, world, 2, {"knowledge_events": [_knowledge("reader", public, channel="inferred")]})
    reader_observer = _build(Session, context, [_candidate("c-2", effects={
        "knowledge": [{"observer_ref": "reader", "fact_ref": public, "state": "knows", "channel": "inferred"}],
    })])

    assert reviewer.WORLD_ENGINE_SOURCE_TYPE == WORLD_ENGINE_SOURCE_TYPE
    assert _codes(_review(Session, world, delta, base_chapter=1)) == []
    leaky_verdict = _review(Session, world, leaky, base_chapter=1)
    assert leaky_verdict.accepted is False
    assert _codes(leaky_verdict) == [("error", "world_engine_reader_knowledge")]
    # Only chapter extraction advances the reader.
    assert _codes(_review(Session, world, chapter_reveal, base_chapter=1)) == []
    # The builder knows no reader node, so a tick never names the reader as an observer.
    assert reader_observer.delta is None
    assert _issues(reader_observer) == [("error", "unresolved_ref", "c-2")]
    engine.dispose()


def test_gate_guards_rule_definitions_in_world_engine_delta() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    rule_id = f"rule-{world.project_id}"
    _commit(Session, world, GraphDelta(
        id=f"rule-seed-{world.project_id}",
        project_id=world.project_id,
        chapter_number=1,
        source_type="genesis_seed",
        node_patches=[NodePatch(
            node_id=rule_id, node_type="rule", op="create",
            new_value={
                "project_id": world.project_id, "name": "rule",
                "profile": {"statement": "statement-1", "hardness": "hard", "checkable": None},
                "state": {"status": "active"},
            },
        )],
    ))
    chapter = _chapter_one(Session, world)
    delta = _built(Session, _context(world, chapter), [_candidate("c-1", effects={
        "entity_changes": [{"ref": rule_id, "field": "statement", "new_value": "statement-2"}],
    })])

    assert _codes(_review(Session, world, delta, base_chapter=1)) == [("error", "immutable_rule_definition_conflict")]
    engine.dispose()


def test_commit_world_tick_is_idempotent_by_tick_id() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    context = _context(world, _chapter_one(Session, world))
    delta = _built(Session, context, [_candidate("c-1", effects={
        "movements": [_walk(world.hero, world.loc(2), context.window_start, 120)],
    })])

    first = _commit_tick(Session, world, delta, tick_id="tick-1")
    # A delta rebuilt for the same tick returns what the tick committed.
    again = _commit_tick(Session, world, delta.model_copy(update={"id": "rebuilt-delta"}), tick_id="tick-1")

    assert first == CanonWorldTickOutcome(graph_delta_ids=(delta.id,), idempotent=False)
    assert again == CanonWorldTickOutcome(graph_delta_ids=(delta.id,), idempotent=True)
    projection_event = f"canon-world-tick:tick-1:{delta.id}:projection"
    with Session() as session:
        committed = session.scalars(select(DecisionEvent).where(
            DecisionEvent.project_id == world.project_id,
            DecisionEvent.event_type == DecisionEventType.WORLD_TICK_COMMITTED,
        )).all()
        outbox = session.scalars(select(OutboxEvent).where(OutboxEvent.event_id == projection_event)).all()
        runtime = BookStateProjection(session).load_runtime_as_of(world.project_id, as_of_chapter=1)

    assert [(event.chapter_number, json.loads(event.payload_json)) for event in committed] == [
        (1, {"tick_id": "tick-1", "graph_delta_ids": [delta.id]}),
    ]
    assert [(event.event_type, json.loads(event.payload_json)) for event in outbox] == [(
        "knowledge.projection.refresh_requested",
        {
            "project_id": world.project_id, "projection_kind": "all", "as_of_chapter": 1,
            "trigger": WORLD_ENGINE_SOURCE_TYPE, "tick_id": "tick-1",
        },
    )]
    assert runtime.world.get_state(world.hero)["location_id"] == world.loc(2)
    engine.dispose()


def test_commit_world_tick_rejects_stale_chapter_and_foreign_source_type() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    context = _context(world, _chapter_one(Session, world))

    def hero_walks(tick_id: str) -> GraphDelta:
        return _built(Session, context, [_candidate("c-1", effects={
            "movements": [_walk(world.hero, world.loc(2), context.window_start, 120)],
        })], tick_id=tick_id)

    earlier, later = hero_walks("tick-a"), hero_walks("tick-b")
    _commit_tick(Session, world, earlier, tick_id="tick-a")
    # The later tick still expects the hero at loc 1: its CAS fails.
    with pytest.raises(CanonStaleVersion):
        _commit_tick(Session, world, later, tick_id="tick-b")
    # A delta of another tick does not commit under this tick's id.
    with pytest.raises(CanonWriteFailure):
        _commit_tick(Session, world, hero_walks("tick-c"), tick_id="tick-d")

    _commit(Session, world, _delta(Session, world, 2, _time(1, "时")))
    with pytest.raises(CanonWriteFailure):
        _commit_tick(Session, world, hero_walks("tick-e"), tick_id="tick-e", chapter=1)
    world_edit = GraphDelta(
        id=f"world-edit-{world.project_id}", project_id=world.project_id, chapter_number=2,
        source_type="world_edit", metadata={"tick_id": "tick-f"},
    )
    with pytest.raises(CanonWriteFailure):
        _commit_tick(Session, world, world_edit, tick_id="tick-f", chapter=2)
    with Session() as session:
        committed = session.scalars(select(DecisionEvent.payload_json).where(
            DecisionEvent.project_id == world.project_id,
            DecisionEvent.event_type == DecisionEventType.WORLD_TICK_COMMITTED,
        )).all()
    assert [json.loads(payload)["tick_id"] for payload in committed] == ["tick-a"]
    engine.dispose()


def test_secret_tick_event_leaves_reader_overlay_unchanged() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    # The reader infers a public fact in chapter 1, which gives it an overlay.
    chapter = _chapter_one(Session, world, {"knowledge_events": [
        _knowledge("reader", world.fact("fact-public"), channel="inferred"),
    ]})
    context = _context(world, chapter)

    def reader_knowledge() -> dict[str, str]:
        with Session() as session:
            runtime = BookStateProjection(session).load_runtime_as_of(world.project_id, as_of_chapter=1)
        return dict(runtime.cognition_by_observer[("reader", "reader")].knowledge_by_ref)

    before = reader_knowledge()
    delta = _built(Session, context, [_candidate("c-1", effects={
        "events": [{
            "summary": "secret-meeting", "participant_refs": [world.hero, world.rival], "location_id": world.loc(1),
            "visibility": "secret", "at": context.window_start + 60,
        }],
        "new_facts": [{
            "key": "plot", "proposition": "plot-proposition", "truth_value": True, "sensitivity": "secret",
            "known_by": [world.hero],
        }],
        "knowledge": [{
            "observer_ref": world.rival, "fact_ref": "fact:new:plot", "state": "knows", "channel": "told",
            "source_ref": world.hero,
        }],
    })])
    plot = next(f"fact:{patch.fact_id}" for patch in delta.fact_patches if patch.proposition == "plot-proposition")
    event = next(patch for patch in delta.node_patches if patch.node_type == "event")

    assert _codes(_review(Session, world, delta, base_chapter=1)) == []
    _commit_tick(Session, world, delta, tick_id="tick-1")
    with Session() as session:
        runtime = BookStateProjection(session).load_runtime_as_of(world.project_id, as_of_chapter=1)

    assert before == {world.fact("fact-public"): "knows"}
    assert reader_knowledge() == before
    assert runtime.cognition_by_observer[("character", world.hero)].knowledge_by_ref[plot] == "knows"
    assert runtime.cognition_by_observer[("character", world.rival)].knowledge_by_ref[plot] == "knows"
    assert runtime.world.get_state(event.node_id)["visibility_level"] == "secret"
    assert runtime.world.get_state(event.node_id)["actual_story_time"] == context.window_start + 60
    assert runtime.world.facts_by_id[plot.removeprefix("fact:")].sensitivity_level == "secret"
    engine.dispose()


def test_delta_builder_imports_first() -> None:
    # The gate is imported on the way in, so a fresh import must not cycle back into the builder.
    result = subprocess.run(
        [sys.executable, "-c", "import forwin.world_engine.delta_builder"], capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
