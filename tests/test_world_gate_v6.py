from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from forwin.book_state import BookStateCompiler, BookStateRepository
from forwin.book_state.extraction.chapter_delta_schema import ChapterDeltaExtraction
from forwin.book_state.extraction import world_review
from forwin.book_state.extraction.translator import ChapterDeltaTranslator, TranslationResult
from forwin.book_state import reveal_schedule
from forwin.book_state.reveal_schedule import earliest_reveal_chapters
from forwin.book_state.reviewer import BookStateReviewGate, BookStateReviewVerdict
from forwin.canon import preparation
from forwin.genesis.handoff.world_seed_committer import WorldSeedCommitter, seed_ref_id
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.genesis import BookGenesisRevision
from forwin.naming.types import EntityAdmissionPlan
from forwin.protocol.book_state import ApprovedGraphDeltaSet, GraphDelta, MapEdge, MapNode, NodePatch
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from tests.chapter_delta_helpers import chapter_delta
from tests.postgres import postgres_test_url


OPENING = 1000
SUBWORLD = "subworld-1"
REGION = "region-1"
WALK = {
    "id": "mode-walk", "name": "", "speed_multiplier": 1.0,
    "allowed_edge_types": ["road", "hidden_route", "mountain_pass"],
}
TWO_ARCS = [
    {"arc_number": 1, "chapter_start": 1, "chapter_end": 5},
    {"arc_number": 2, "chapter_start": 6, "chapter_end": 10},
]


def _pack(
    *,
    travel_modes: list[dict[str, Any]],
    hero_modes: list[str],
    reveal_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    story_engine = {
        "core_cast": [{
            "id": "cast-1", "name": "甲", "role": "role-cast", "desire": "desire-cast",
            "fear": "fear-cast", "culture_profile_id": "", "affiliated_faction": "", "current_base": "node-1",
        }],
        "opposition": [{
            "id": "opp-1", "name": "乙", "role": "role-opp", "desire": "desire-opp",
            "culture_profile_id": "", "backing_faction": "", "base_location": "node-2",
        }],
        "factions": [{
            "id": "faction-1", "name": "丙", "role": "role-faction", "goal": "goal-faction",
            "culture_profile_id": "", "base_location": "node-1",
        }],
    }
    seed = {
        "calendar": {
            "epoch_label": "",
            "opening_minute": OPENING,
            "units": [{"name": "日", "minutes": 1440}, {"name": "时", "minutes": 60}],
        },
        "lexicon": {
            "time_unit_aliases": [{"name": "刻", "minutes": 15}], "rank_ladders": [], "currencies": [],
            "appellations": [], "generic_character_refs": [], "travel_modes": travel_modes,
            "news_speed_multiplier": 1.0,
        },
        "actors": [
            {"ref": "cast-1", "actor_tier": "minor", "location_ref": "node-1", "travel_mode_ids": hero_modes,
             "resources": [], "agendas": [], "intents": []},
            {"ref": "opp-1", "actor_tier": "minor", "location_ref": "node-2", "travel_mode_ids": [],
             "resources": [], "agendas": [], "intents": []},
            {"ref": "faction-1", "actor_tier": "minor", "location_ref": "node-1", "travel_mode_ids": [],
             "resources": [], "agendas": [], "intents": []},
        ],
        "relations": [],
        "facts": [
            {"id": "fact-secret", "proposition": "proposition-secret", "truth_value": True,
             "sensitivity": "secret", "related_refs": ["cast-1"], "nobody_knows": False},
            {"id": "fact-public", "proposition": "proposition-public", "truth_value": True,
             "sensitivity": "public", "related_refs": [], "nobody_knows": False},
            {"id": "fact-unscheduled", "proposition": "proposition-unscheduled", "truth_value": True,
             "sensitivity": "secret", "related_refs": [], "nobody_knows": True},
            {"id": "fact-lie", "proposition": "proposition-lie", "truth_value": False,
             "sensitivity": "public", "related_refs": [], "nobody_knows": False},
        ],
        # The hero knowing a fact gives it a cognition overlay; the rival has none.
        "knowledge": [{"observer_ref": "cast-1", "fact_id": "fact-secret", "state": "knows", "channel": "seeded"}],
        "rules": [],
        "clocks": [],
        "reader_promises": [],
        "reveal_candidates": reveal_candidates,
    }
    return {
        "world": {
            "story_engine": story_engine,
            "map_atlas": {"nodes": [{"id": f"node-{number}"} for number in range(1, 6)]},
        },
        "world_seed": seed,
        "book_arc_blueprint": {"arcs": TWO_ARCS},
    }


@dataclass(frozen=True)
class _World:
    project_id: str

    def node(self, kind: str, ref: str) -> str:
        return seed_ref_id(self.project_id, kind, ref)

    def loc(self, number: int) -> str:
        return f"loc-{number}-{self.project_id}"

    def fact(self, ref: str) -> str:
        return f"fact:{self.node('fact', ref)}"

    @property
    def hero(self) -> str:
        return self.node("character", "cast-1")

    @property
    def rival(self) -> str:
        return self.node("character", "opp-1")

    @property
    def faction(self) -> str:
        return self.node("faction", "faction-1")


def _session_factory():
    engine = get_engine(postgres_test_url())
    init_db(engine)
    return engine, get_session_factory(engine)


def _seed_world(
    Session,
    *,
    travel_modes: list[dict[str, Any]] | None = None,
    hero_modes: list[str] | None = None,
    reveal_candidates: list[dict[str, Any]] | None = None,
    extra_edges: tuple[tuple[str, int, int, str, float, bool], ...] = (),
) -> _World:
    """Chapter 0: loc 1 -road 2h- loc 2 -hidden route 1h- loc 3; loc 2 -mountain pass 4h- loc 4; loc 5 isolated."""
    pack = _pack(
        travel_modes=travel_modes if travel_modes is not None else [WALK],
        hero_modes=hero_modes if hero_modes is not None else ["mode-walk"],
        reveal_candidates=reveal_candidates or [],
    )
    with Session.begin() as session:
        project = StateUpdater(session).create_project(
            title="world gate v6", premise="gate", genre="test",
            runtime_policy=RuntimePolicy.for_profile("standard"),
            creation_status="genesis_ready",
        )
        revision = BookGenesisRevision(project_id=project.id, pack_json=json.dumps(pack, ensure_ascii=False))
        session.add(revision)
        session.flush()
        project.active_genesis_revision_id = revision.id
        world = _World(project.id)
        repo = BookStateRepository(session)
        for number in range(1, 6):
            repo.create_map_node(MapNode(
                id=world.loc(number), project_id=project.id, subworld_id=SUBWORLD, region_id=REGION,
                node_type="site", name=f"site-{number}", metadata={"source_node_id": f"node-{number}"},
            ))
        for edge_id, from_number, to_number, edge_type, hours, discovered in (
            ("road", 1, 2, "road", 2.0, True),
            ("hidden", 2, 3, "hidden_route", 1.0, False),
            ("pass", 2, 4, "mountain_pass", 4.0, True),
            *extra_edges,
        ):
            repo.create_map_edge(MapEdge(
                id=f"{edge_id}-{project.id}", project_id=project.id, subworld_id=SUBWORLD,
                from_node_id=world.loc(from_number), to_node_id=world.loc(to_number), edge_type=edge_type,
                bidirectional=True, travel_time=hours, discovered_by_default=discovered,
            ))
        session.flush()
        # The seed itself passes through the gate: v6 must leave genesis_seed deltas alone.
        WorldSeedCommitter().commit(session=session, project=project, revision=revision, pack=pack)
    return world


def _time(amount: float, unit: str) -> dict[str, Any]:
    return {"time": {"elapsed": {"amount": amount, "unit": unit}, "evidence": "e"}}


def _move(entity: str, from_ref: str, to_ref: str, **extra: Any) -> dict[str, Any]:
    return {"entity_ref": entity, "from_ref": from_ref, "to_ref": to_ref, "evidence": "e", **extra}


def _day_of_moves(*movements: dict[str, Any]) -> dict[str, Any]:
    """A one-day chapter: ample time for any route on the test map."""
    return {**_time(1, "日"), "movements": list(movements)}


def _offset(amount: float, unit: str = "时") -> dict[str, Any]:
    return {"amount": amount, "unit": unit}


def _knowledge(observer: str, fact_ref: str = "", **extra: Any) -> dict[str, Any]:
    return {
        "observer_ref": observer, "fact_ref": fact_ref, "state": "knows", "channel": "witnessed",
        "evidence": "e", **extra,
    }


def _event(participants: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "summary": "event-summary", "participant_refs": participants, "significance": "minor",
        "visibility": "public", "evidence": "e", **extra,
    }


def _delta(Session, world: _World, chapter: int, fields: dict[str, Any]) -> GraphDelta:
    extraction = ChapterDeltaExtraction.model_validate({
        "body_sha256": f"{chapter:x}" * 64,
        "time": {"elapsed": None, "evidence": ""},
        **fields,
    })
    with Session() as session:
        result = ChapterDeltaTranslator(session).translate(
            project_id=world.project_id,
            chapter_number=chapter,
            extraction=extraction,
            admission_plan=EntityAdmissionPlan(
                project_id=world.project_id, chapter_number=chapter, candidate_fingerprint="fingerprint",
            ),
            chapter_summary=f"summary {chapter}",
        )
    assert result.delta is not None, result.issues
    return result.delta


def _commit(Session, world: _World, delta: GraphDelta) -> None:
    with Session.begin() as session:
        compiled = BookStateCompiler(session).compile(
            ApprovedGraphDeltaSet(
                project_id=world.project_id, chapter_number=delta.chapter_number, graph_deltas=[delta],
            )
        )
        assert compiled.committed, compiled.blocked_reasons


def _review(Session, world: _World, delta: GraphDelta, **kwargs: Any) -> BookStateReviewVerdict:
    with Session() as session:
        return BookStateReviewGate(session).review(
            ApprovedGraphDeltaSet(
                project_id=world.project_id, chapter_number=delta.chapter_number, graph_deltas=[delta],
            ),
            **kwargs,
        )


def _codes(verdict: BookStateReviewVerdict) -> list[tuple[str, str]]:
    return [(issue.severity, issue.code) for issue in verdict.issues]


def _check(Session, world: _World, chapter: int, fields: dict[str, Any], **kwargs: Any) -> list[tuple[str, str]]:
    return _codes(_review(Session, world, _delta(Session, world, chapter, fields), **kwargs))


def test_time_offset_out_of_range() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)

    def check(depart: float, arrive: float) -> list[tuple[str, str]]:
        return _check(Session, world, 1, {
            **_time(3, "时"),
            "movements": [_move(world.hero, world.loc(1), world.loc(2),
                                depart_offset=_offset(depart), arrive_offset=_offset(arrive))],
        })

    assert check(0, 4) == [("error", "time_offset_out_of_range")]
    assert check(2, 1) == [("error", "time_offset_out_of_range")]
    assert check(0, 2) == []
    engine.dispose()


def test_movement_unreachable() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)

    isolated = _check(Session, world, 1, _day_of_moves(_move(world.hero, world.loc(1), world.loc(5))))
    connected = _check(Session, world, 1, _day_of_moves(_move(world.hero, world.loc(1), world.loc(2))))

    assert isolated == [("error", "movement_unreachable")]
    assert connected == []
    engine.dispose()


def test_movement_into_a_site_created_this_chapter() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)

    into_new_site = _check(Session, world, 1, {
        **_time(1, "日"),
        "new_sites": [{"name": "site-new", "near_ref": world.loc(1), "evidence": "e"}],
        "movements": [_move(world.hero, world.loc(1), "new:site-new")],
    })

    assert into_new_site == []
    engine.dispose()


def test_movement_uses_movers_known_routes() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)

    # The hero has a cognition overlay and does not know the hidden route from loc 2 to loc 3.
    hero_unknown = _check(Session, world, 1, _day_of_moves(_move(world.hero, world.loc(1), world.loc(3))))
    # The rival has no overlay, so the objective map applies.
    rival_objective = _check(Session, world, 1, _day_of_moves(_move(world.rival, world.loc(2), world.loc(3))))
    # A discovery the prose evidences in this chapter makes the route known.
    hero_discovers = _check(Session, world, 1, {
        **_time(1, "日"),
        "movements": [_move(world.hero, world.loc(1), world.loc(3), discovers_route=True)],
    })

    assert hero_unknown == [("error", "movement_route_unknown_to_mover")]
    assert rival_objective == []
    assert hero_discovers == []
    engine.dispose()


def test_movement_speed_against_shortest_time_and_tolerance() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)

    def check(fields: dict[str, Any], **kwargs: Any) -> list[tuple[str, str]]:
        # loc 1 -> loc 2 is a 2 h road walked at 1.0: 120 minutes; 20% tolerance gives 96.
        offsets = fields.pop("offsets", {})
        return _check(Session, world, 1, {
            **fields, "movements": [_move(world.hero, world.loc(1), world.loc(2), **offsets)],
        }, **kwargs)

    assert check(_time(6, "刻")) == [("error", "movement_too_fast")]
    assert check(_time(7, "刻")) == [("warning", "movement_fast_within_tolerance")]
    assert check(_time(2, "时")) == []
    # Both offsets set: the claimed time is arrive - depart, not the chapter's span.
    assert check({**_time(1, "日"), "offsets": {
        "depart_offset": _offset(0), "arrive_offset": _offset(1),
    }}) == [("error", "movement_too_fast")]
    # Only one offset: the chapter's span is the claimed time.
    assert check({**_time(1, "日"), "offsets": {"arrive_offset": _offset(1)}}) == []
    assert check(_time(1, "时"), travel_tolerance=0.5) == [("warning", "movement_fast_within_tolerance")]
    # A claim exactly at the inclusive bound is a warning, even where 120 * (1 - 0.7) rounds above 36.
    assert check(_time(1.6, "时")) == [("warning", "movement_fast_within_tolerance")]
    assert check(_time(0.6, "时"), travel_tolerance=0.7) == [("warning", "movement_fast_within_tolerance")]
    engine.dispose()


def test_movement_total_against_chapter_span() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    # loc 1 <-> loc 2 is a 2 h road; the rival, who has no overlay, takes the 1 h route loc 2 <-> loc 3.
    hero_there_and_back = [_move(world.hero, world.loc(1), world.loc(2)), _move(world.hero, world.loc(2), world.loc(1))]
    rival_there_and_back = [_move(world.rival, world.loc(2), world.loc(3)), _move(world.rival, world.loc(3), world.loc(2))]

    def check(time: dict[str, Any], movements: list[dict[str, Any]]) -> list[tuple[str, str]]:
        return _check(Session, world, 1, {**time, "movements": movements})

    # 240 min of travel: each move fits the 180 min chapter, together they exceed 180 * 1.2.
    assert check(_time(3, "时"), hero_there_and_back) == [("error", "movement_total_too_fast")]
    # 240 min within 210 min * 1.2 is only a warning.
    assert check(_time(3.5, "时"), hero_there_and_back) == [("warning", "movement_total_fast_within_tolerance")]
    assert check(_time(3, "时"), rival_there_and_back) == []
    # An unknown span has no total to compare; each move warns on its own.
    assert check({}, hero_there_and_back) == [("warning", "movement_duration_unknown")] * 2
    engine.dispose()


def test_unknown_chapter_duration_checks_speed_only_on_both_offsets() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)

    def check(**offsets: Any) -> list[tuple[str, str]]:
        # No elapsed time: the chapter's end is unknown, not zero minutes after its start.
        return _check(Session, world, 1, {"movements": [_move(world.hero, world.loc(1), world.loc(2), **offsets)]})

    assert check() == [("warning", "movement_duration_unknown")]
    assert check(arrive_offset=_offset(3)) == [("warning", "movement_duration_unknown")]
    assert check(depart_offset=_offset(0), arrive_offset=_offset(3)) == []
    assert check(depart_offset=_offset(0), arrive_offset=_offset(1)) == [("error", "movement_too_fast")]
    assert check(depart_offset=_offset(2), arrive_offset=_offset(1)) == [
        ("error", "time_offset_out_of_range"), ("warning", "movement_duration_unknown"),
    ]
    engine.dispose()


def test_travel_mode_speed_requires_allowed_edge_types() -> None:
    engine, Session = _session_factory()
    horse = {"id": "mode-horse", "name": "", "speed_multiplier": 4.0, "allowed_edge_types": ["road"]}
    road_only = _seed_world(Session, travel_modes=[horse], hero_modes=["mode-horse"])
    with_pass = _seed_world(
        Session,
        travel_modes=[{**horse, "allowed_edge_types": ["road", "mountain_pass"]}],
        hero_modes=["mode-horse"],
    )

    def check(world: _World) -> list[tuple[str, str]]:
        # Road 2 h + mountain pass 4 h: 360 minutes at 1.0, 90 at the horse's 4.0.
        return _check(Session, world, 1, {
            **_time(6, "刻"), "movements": [_move(world.hero, world.loc(1), world.loc(4))],
        })

    assert check(road_only) == [("error", "movement_too_fast")]
    assert check(with_pass) == []
    engine.dispose()


def test_travel_mode_may_take_a_longer_route_it_is_allowed_on() -> None:
    engine, Session = _session_factory()
    horse = {"id": "mode-horse", "name": "", "speed_multiplier": 4.0, "allowed_edge_types": ["road"]}
    world = _seed_world(
        Session, travel_modes=[horse], hero_modes=["mode-horse"],
        extra_edges=(("long-road", 2, 4, "road", 10.0, True),),
    )

    def check(fields: dict[str, Any]) -> list[tuple[str, str]]:
        # On foot road 2 h + pass 4 h = 360 min; the horse on roads 2 h + 10 h = 12 h / 4 = 180 min.
        return _check(Session, world, 1, {**fields, "movements": [_move(world.hero, world.loc(1), world.loc(4))]})

    assert check(_time(4, "时")) == []
    assert check(_time(2, "时")) == [("error", "movement_too_fast")]
    engine.dispose()


def test_restrained_entity_moved_without_release() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    _commit(Session, world, _delta(Session, world, 1, {
        "entity_changes": [{"ref": world.hero, "field": "restraint.state", "new_value": "imprisoned", "evidence": "e"}],
    }))
    move = _move(world.hero, world.loc(1), world.loc(2))

    restrained = _check(Session, world, 2, {**_time(1, "日"), "movements": [move]})
    released = _check(Session, world, 2, {
        **_time(1, "日"),
        "entity_changes": [{"ref": world.hero, "field": "restraint.state", "new_value": "free", "evidence": "e"}],
        "movements": [move],
    })
    free_rival = _check(Session, world, 2, _day_of_moves(_move(world.rival, world.loc(2), world.loc(1))))

    assert restrained == [("error", "restrained_entity_moved")]
    assert released == []
    assert free_rival == []
    engine.dispose()


def test_restraint_release_anywhere_in_the_delta_counts() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    _commit(Session, world, _delta(Session, world, 1, {
        "entity_changes": [{"ref": world.hero, "field": "restraint.state", "new_value": "imprisoned", "evidence": "e"}],
    }))
    delta = _delta(Session, world, 2, {
        **_time(1, "日"),
        "entity_changes": [{"ref": world.hero, "field": "restraint.state", "new_value": "free", "evidence": "e"}],
        "movements": [_move(world.hero, world.loc(1), world.loc(2))],
    })
    # The release patch after the movement's location patch still frees the mover.
    reordered = delta.model_copy(update={"node_patches": list(reversed(delta.node_patches))})

    assert [patch.field_path for patch in reordered.node_patches] == ["state.location_id", "state.restraint.state"]
    assert _codes(_review(Session, world, reordered)) == []
    engine.dispose()


def test_dead_entity_acts() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    _commit(Session, world, _delta(Session, world, 1, {
        "entity_changes": [{"ref": world.rival, "field": "alive_status", "new_value": "dead", "evidence": "e"}],
    }))

    moves = _check(Session, world, 2, _day_of_moves(_move(world.rival, world.loc(2), world.loc(1))))
    participates = _check(Session, world, 2, {"events": [_event([world.hero, world.rival])]})
    observes = _check(Session, world, 2, {"knowledge_events": [_knowledge(world.rival, world.fact("fact-public"))]})
    tells = _check(Session, world, 2, {"knowledge_events": [_knowledge(
        world.hero, world.fact("fact-public"), channel="told", source_ref=world.rival,
    )]})
    living = _check(Session, world, 2, {
        **_time(1, "日"),
        "movements": [_move(world.hero, world.loc(1), world.loc(2))],
        "events": [_event([world.hero])],
        "knowledge_events": [_knowledge(world.hero, world.fact("fact-public"))],
    })

    assert moves == [("error", "dead_entity_acts")]
    assert participates == [("error", "dead_entity_acts")]
    assert observes == [("error", "dead_entity_acts")]
    assert tells == [("error", "dead_entity_acts")]
    assert living == []
    engine.dispose()


def test_flashback_dead_character_is_allowed() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    _commit(Session, world, _delta(Session, world, 1, {
        "entity_changes": [{"ref": world.rival, "field": "alive_status", "new_value": "dead", "evidence": "e"}],
    }))

    recalled = _check(Session, world, 2, {
        "events": [_event([world.hero, world.rival], flashback=True)],
        "knowledge_events": [
            _knowledge(world.rival, world.fact("fact-public"), flashback=True),
            _knowledge(world.hero, world.fact("fact-public"), channel="told", source_ref=world.rival, flashback=True),
        ],
    })

    assert recalled == []
    engine.dispose()


def test_entity_killed_in_its_own_chapter_event_is_not_dead_acting() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)

    killed = _check(Session, world, 1, {
        "entity_changes": [{"ref": world.rival, "field": "alive_status", "new_value": "dead", "evidence": "e"}],
        "events": [_event([world.hero, world.rival])],
    })

    assert killed == []
    engine.dispose()


def test_item_holder_mismatch_follows_transfers_in_order() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    first = _delta(Session, world, 1, {"item_transfers": [{
        "new_item": {"name": "item-a", "kind": "item"}, "from_ref": "", "to_ref": world.hero, "evidence": "e",
    }]})
    _commit(Session, world, first)
    item = first.metadata["item_transfers"][0]["item_id"]

    def check(*hops: tuple[str, str]) -> list[tuple[str, str]]:
        return _check(Session, world, 2, {"item_transfers": [
            {"item_ref": item, "from_ref": from_ref, "to_ref": to_ref, "evidence": "e"} for from_ref, to_ref in hops
        ]})

    assert check((world.rival, world.hero)) == [("error", "item_holder_mismatch")]
    assert check((world.hero, world.rival), (world.rival, world.hero)) == []
    assert check((world.hero, world.rival), (world.hero, world.rival)) == [("error", "item_holder_mismatch")]
    engine.dispose()


def test_told_by_unaware_source() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    secret, public = world.fact("fact-secret"), world.fact("fact-public")

    def told(observer: str, fact_ref: str, source: str) -> dict[str, Any]:
        return _knowledge(observer, fact_ref, channel="told", source_ref=source)

    unaware = _check(Session, world, 1, {"knowledge_events": [told(world.hero, secret, world.rival)]})
    aware = _check(Session, world, 1, {"knowledge_events": [told(world.rival, secret, world.hero)]})
    public_fact = _check(Session, world, 1, {"knowledge_events": [told(world.hero, public, world.rival)]})
    learned_first = _check(Session, world, 1, {"knowledge_events": [
        _knowledge(world.rival, secret), told(world.hero, secret, world.rival),
    ]})

    assert unaware == [("error", "told_by_unaware_source")]
    assert aware == []
    assert public_fact == []
    assert learned_first == []
    engine.dispose()


def test_unexplained_secret_knowledge_by_rumor() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)

    def rumor(fact_ref: str, state: str) -> list[tuple[str, str]]:
        return _check(Session, world, 1, {"knowledge_events": [
            _knowledge(world.rival, fact_ref, channel="rumor", state=state),
        ]})

    assert rumor(world.fact("fact-secret"), "knows") == [("warning", "unexplained_secret_knowledge")]
    assert rumor(world.fact("fact-secret"), "suspects") == []
    assert rumor(world.fact("fact-public"), "knows") == []
    engine.dispose()


def test_premature_reveal_uses_world_seed_schedule() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session, reveal_candidates=[{"fact_id": "fact-secret", "arc_number": 2, "note": ""}])
    secret = world.fact("fact-secret")
    reader_learns = {"knowledge_events": [_knowledge("reader", secret, channel="inferred")]}
    touches = {"knowledge_events": [_knowledge(
        "reader", world.fact("fact-public"), channel="inferred", touches_secret_ref=secret,
    )]}

    with Session() as session:
        schedule = earliest_reveal_chapters(session, world.project_id)

    assert schedule == {world.node("fact", "fact-secret"): 6}
    assert _check(Session, world, 1, reader_learns) == [("error", "premature_reveal")]
    assert _check(Session, world, 1, touches) == [("error", "premature_reveal")]
    assert _check(Session, world, 6, reader_learns) == []
    assert _check(Session, world, 6, touches) == []
    # A character learning the secret is not a reveal to the reader.
    assert _check(Session, world, 1, {"knowledge_events": [
        _knowledge(world.rival, secret, channel="told", source_ref=world.hero),
    ]}) == []
    engine.dispose()


def test_unscheduled_reveal() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)

    unscheduled = _check(Session, world, 1, {"knowledge_events": [
        _knowledge("reader", world.fact("fact-unscheduled"), channel="inferred"),
    ]})
    public = _check(Session, world, 1, {"knowledge_events": [
        _knowledge("reader", world.fact("fact-public"), channel="inferred"),
    ]})

    assert unscheduled == [("warning", "unscheduled_reveal")]
    assert public == []
    engine.dispose()


def test_contradicts_hidden_fact_only_when_presented_as_true() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    secret = world.fact("fact-secret")

    def new_fact(truth: bool, contradicts: str) -> list[tuple[str, str]]:
        return _check(Session, world, 1, {"knowledge_events": [_knowledge(
            world.rival, state="believes",
            new_fact={"proposition": f"claim-{truth}", "truth_value": truth, "sensitivity": "public"},
            contradicts_ref=contradicts,
        )]})

    def existing(fact_ref: str) -> list[tuple[str, str]]:
        return _check(Session, world, 1, {"knowledge_events": [_knowledge(
            world.rival, fact_ref, state="believes", contradicts_ref=secret,
        )]})

    assert new_fact(True, secret) == [("error", "contradicts_hidden_fact")]
    # A lie that contradicts the secret is misdirection.
    assert new_fact(False, secret) == []
    assert new_fact(True, world.fact("fact-public")) == []
    assert existing(world.fact("fact-public")) == [("error", "contradicts_hidden_fact")]
    assert existing(world.fact("fact-lie")) == []
    engine.dispose()


def test_extraction_location_change_without_movement_keeps_reachability_check() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)

    def relocate(location: str) -> list[tuple[str, str]]:
        return _check(Session, world, 1, {
            "entity_changes": [{"ref": world.faction, "field": "location_id", "new_value": location, "evidence": "e"}],
        })

    assert relocate(world.loc(5)) == [("error", "movement_unreachable")]
    assert relocate(world.loc(2)) == []
    engine.dispose()


def test_non_extraction_deltas_skip_v6_checks() -> None:
    engine, Session = _session_factory()
    # Seeding already ran the genesis_seed deltas through the gate.
    world = _seed_world(Session)
    v6_bait = {
        "movements": [{
            "entity_id": world.hero, "from_id": world.loc(1), "to_id": world.loc(5),
            "depart_minute": None, "arrive_minute": None, "travel_mode_id": "", "evidence": "e",
        }],
        "knowledge_tags": [{
            "observer_type": "reader", "observer_id": "reader", "fact_ref": world.fact("fact-secret"),
            "state": "knows", "channel": "rumor", "source_id": world.rival, "touches_secret_ref": "",
            "contradicts_ref": world.fact("fact-secret"), "flashback": False, "evidence": "e",
        }],
    }

    def world_edit(new_location: str) -> GraphDelta:
        return GraphDelta(
            id=f"world-edit-{new_location}",
            project_id=world.project_id,
            chapter_number=1,
            source_type="world_edit",
            node_patches=[NodePatch(
                node_id=world.hero, node_type="character", op="set", field_path="state.location_id",
                old_value=world.loc(1), new_value=new_location,
            )],
            metadata=v6_bait,
        )

    reachable = _review(Session, world, world_edit(world.loc(2)))
    # The legacy patch-based reachability check still applies to non-extraction deltas.
    isolated = _review(Session, world, world_edit(world.loc(5)))

    assert reachable.accepted is True
    assert _codes(reachable) == []
    assert _codes(isolated) == [("error", "movement_unreachable")]
    engine.dispose()


def test_reveal_schedule_logs_unusable_pack_and_arc_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session, reveal_candidates=[{"fact_id": "fact-secret", "arc_number": 2, "note": ""}])
    warnings: list[str] = []

    class Logger:
        # Other tests reconfigure global logging, so record the module's warnings directly.
        def warning(self, message: str, *args: Any) -> None:
            warnings.append(message % args)

    monkeypatch.setattr(reveal_schedule, "logger", Logger())

    def schedule(pack_json: str) -> dict[str, int]:
        with Session.begin() as session:
            revision = session.scalars(
                select(BookGenesisRevision).where(BookGenesisRevision.project_id == world.project_id)
            ).one()
            revision.pack_json = pack_json
        with Session() as session:
            return reveal_schedule.earliest_reveal_chapters(session, world.project_id)

    text_arc_pack = _pack(travel_modes=[WALK], hero_modes=["mode-walk"], reveal_candidates=[
        {"fact_id": "fact-secret", "arc_number": "2", "note": ""},
    ])

    assert schedule("{not json") == {}
    assert schedule(json.dumps(text_arc_pack)) == {}
    assert len(warnings) == 2
    assert "pack_json is not JSON" in warnings[0]
    assert "non-integer arc_number '2'" in warnings[1]
    engine.dispose()


def test_review_policy_travel_tolerance() -> None:
    policy = RuntimePolicy.for_profile("standard")
    stored = policy.model_dump(mode="json")
    del stored["review"]["travel_tolerance"]

    assert policy.review.travel_tolerance == 0.2
    assert RuntimePolicy.model_validate(stored).review.travel_tolerance == 0.2
    for invalid in (-0.1, 1.5):
        with pytest.raises(ValidationError):
            policy.review.model_validate({**policy.review.model_dump(), "travel_tolerance": invalid})


def test_canon_preparation_passes_policy_travel_tolerance(monkeypatch: pytest.MonkeyPatch) -> None:
    delta = GraphDelta(id="delta-3", project_id="project-1", chapter_number=3, source_type="chapter_extraction")
    calls: list[dict[str, Any]] = []

    class Translator:
        def __init__(self, _session: Any) -> None:
            pass

        def translate(self, **_kwargs: Any) -> TranslationResult:
            return TranslationResult(delta, ())

    class Gate:
        def __init__(self, _session: Any) -> None:
            pass

        def review(self, reviewed: ApprovedGraphDeltaSet, **kwargs: Any) -> BookStateReviewVerdict:
            calls.append({"approved_by": reviewed.approved_by, **kwargs})
            return BookStateReviewVerdict(
                project_id="project-1", chapter_number=3, verdict_id="verdict", accepted=True,
                approved_changes=reviewed,
            )

    monkeypatch.setattr(world_review, "ChapterDeltaTranslator", Translator)
    monkeypatch.setattr(world_review, "BookStateReviewGate", Gate)
    policy = RuntimePolicy.for_profile("standard")
    policy = policy.model_copy(update={"review": policy.review.model_copy(update={"travel_tolerance": 0.35})})

    outcome = preparation.BookStateCanonPreparer().prepare(
        context=preparation.CanonPreparationContext(
            policy=policy,
            llm_client=object(),  # type: ignore[arg-type]
            artifact_store=object(),  # type: ignore[arg-type]
            _record_decision_event=lambda **_kwargs: None,  # type: ignore[arg-type]
            _record_rule_decision_event=lambda **_kwargs: None,  # type: ignore[arg-type]
        ),
        session=object(),  # type: ignore[arg-type]
        candidate_id="candidate-1",
        project_id="project-1",
        chapter_number=3,
        writer_output=WriterOutput(
            project_id="project-1", chapter_number=3, title="t", body="b", end_of_chapter_summary="s",
            chapter_delta=chapter_delta("b"),
        ),
        verdict=ReviewVerdict(verdict="pass"),
        entity_admission_plan=EntityAdmissionPlan(
            project_id="project-1", chapter_number=3, candidate_fingerprint="",
        ),
    )

    assert outcome.blocked is False
    assert [(item.id, item.metadata) for item in outcome.approved_changes.graph_deltas] == [
        ("delta-3", {"translation_warnings": []}),
    ]
    assert calls == [{"approved_by": ["chapter_extraction"], "base_chapter": 2, "travel_tolerance": 0.35}]
