from __future__ import annotations

import json
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
from forwin.models.genesis import BookGenesisRevision
from forwin.models.project import ArcPlanVersion
from forwin.naming.types import EntityAdmissionPlan
from forwin.protocol.book_state import ApprovedGraphDeltaSet, GraphDelta, MapEdge, MapNode, NodePatch
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.runtime.policy import RuntimePolicy
from tests.chapter_delta_helpers import chapter_delta


from tests.world_gate_helpers import (
    OPENING, SUBWORLD, REGION, WALK, TWO_ARCS, _World, _pack, _session_factory, _seed_world, _knowledge,
)


def _time(amount: float, unit: str) -> dict[str, Any]:
    return {"time": {"elapsed": {"amount": amount, "unit": unit}, "evidence": "e"}}


def _move(entity: str, from_ref: str, to_ref: str, **extra: Any) -> dict[str, Any]:
    return {"entity_ref": entity, "from_ref": from_ref, "to_ref": to_ref, "evidence": "e", **extra}


def _day_of_moves(*movements: dict[str, Any]) -> dict[str, Any]:
    """A one-day chapter: ample time for any route on the test map."""
    return {**_time(1, "日"), "movements": list(movements)}


def _offset(amount: float, unit: str = "时") -> dict[str, Any]:
    return {"amount": amount, "unit": unit}



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


@pytest.mark.parametrize("depart, expected", [(899, ["movement_overlap"]), (900, [])])
@pytest.mark.parametrize("source", ["chapter_extraction", "world_engine"])
def test_gate_rejects_overlapping_moves_of_one_mover(monkeypatch, depart, expected, source):
    from tests.test_world_constraints import LEXICON, _runtime

    monkeypatch.setattr("forwin.book_state.reviewer.BookLexiconRepository.get", lambda self, project: LEXICON)
    delta = GraphDelta(
        id="overlap", project_id="p", chapter_number=4, source_type=source,
        story_time_start=600, story_time_end=1200, metadata={"time_elapsed_minutes": 600, "movements": [
            {"entity_id": "a", "from_id": "l1", "to_id": "l2", "depart_minute": 600, "arrive_minute": 900},
            {"entity_id": "b", "from_id": "l1", "to_id": "l2", "depart_minute": 600, "arrive_minute": 900},
            {"entity_id": "a", "from_id": "l2", "to_id": "l1", "depart_minute": depart, "arrive_minute": 1200},
        ]},
    )
    from forwin.book_state.map_graph import MapGraph
    runtime = _runtime()
    runtime.map = MapGraph(nodes=list(runtime.map.nodes_by_id.values()), edges=[
        edge.model_copy(update={"bidirectional": True}) for edge in runtime.map.edges_by_id.values()
    ])
    gate = BookStateReviewGate(None)
    monkeypatch.setattr(gate.projection, "load_runtime_as_of", lambda *args, **kwargs: runtime)
    verdict = gate.review(ApprovedGraphDeltaSet(project_id="p", chapter_number=4, graph_deltas=[delta]), base_chapter=3)
    assert [issue.code for issue in verdict.issues if issue.severity == "error"] == expected
    assert verdict.accepted is (not expected)


def test_gate_rejects_overlapping_extracted_moves_postgres():
    engine, Session = _session_factory()
    world = _seed_world(Session)
    fields = {**_time(6, "时"), "movements": [
        _move(world.hero, world.loc(1), world.loc(2), depart_offset=_offset(0), arrive_offset=_offset(2)),
        _move(world.hero, world.loc(2), world.loc(1), depart_offset=_offset(1), arrive_offset=_offset(3)),
    ]}
    verdict = _review(Session, world, _delta(Session, world, 1, fields))
    assert verdict.accepted is False
    assert ("error", "movement_overlap") in _codes(verdict)
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

    # 240 min of travel, 20% tolerance: a span under 192 min is an error, under 240 a warning.
    # Each move alone fits every span here.
    assert check(_time(3, "时"), hero_there_and_back) == [("error", "movement_total_too_fast")]
    assert check(_time(3.5, "时"), hero_there_and_back) == [("warning", "movement_total_fast_within_tolerance")]
    # A span exactly at the inclusive bound, 240 * (1 - 0.2), is a warning.
    assert check(_time(3.2, "时"), hero_there_and_back) == [("warning", "movement_total_fast_within_tolerance")]
    assert check(_time(4, "时"), hero_there_and_back) == []
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
    from types import SimpleNamespace

    from forwin.planning.beat_sheet.service import ChapterMilestoneRefs

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

    shared_runtime = object()
    milestone_calls: list[tuple[Any, ...]] = []

    def milestone_refs(*args: Any) -> ChapterMilestoneRefs:
        milestone_calls.append(args[1:])
        return ChapterMilestoneRefs(frozenset({"milestone-ref"}), frozenset({("exit-ref", "state.status", "destroyed")}))

    monkeypatch.setattr("forwin.planning.beat_sheet.service.chapter_milestone_refs", milestone_refs)
    monkeypatch.setattr(world_review.BookStateProjection, "load_runtime_as_of", lambda *args, **kwargs: shared_runtime)
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
        session=SimpleNamespace(scalar=lambda query: None),
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
    assert calls == [{"approved_by": ["chapter_extraction"], "base_chapter": 2,
                      "travel_tolerance": 0.35, "runtime": shared_runtime,
                      "milestone_refs": frozenset({"milestone-ref"}),
                      "planned_endings": frozenset({("exit-ref", "state.status", "destroyed")})}]
    assert milestone_calls == [("project-1", 3)]


@pytest.mark.parametrize("payload", [
    {"ref": "a", "until_chapter": 1},
    {"ref": "a", "kind": "must_not_know", "until_chapter": 1},
    {"ref": "a", "kind": "must_hold_state", "until_chapter": 1},
    {"ref": "a", "kind": "must_exist"},
    {"ref": "a", "kind": "invalid", "until_chapter": 1},
    {"ref": "a", "kind": "must_exist", "until_chapter": True},
    {"ref": "a", "kind": "must_exist", "until_chapter": -1},
])
def test_gate_rejects_malformed_protection(monkeypatch, payload):
    from forwin.protocol.book_state import NarrativePatch
    from tests.test_world_constraints import _runtime

    gate = BookStateReviewGate(None)
    monkeypatch.setattr(gate.projection, "load_runtime_as_of", lambda *args, **kwargs: _runtime())
    delta = GraphDelta(id="plan", project_id="p", source_type="plan_constraint", narrative_patches=[
        NarrativePatch(target_ref="review_constraint:new", op="create", new_value={
            "id": "new", "project_id": "p", "node_type": "review_constraint", "payload": payload,
        }),
    ])
    verdict = gate.review(ApprovedGraphDeltaSet(project_id="p", chapter_number=0, graph_deltas=[delta]), base_chapter=0)
    assert not verdict.accepted
    assert ("error", "invalid_protection") in _codes(verdict)


def test_gate_rejects_non_narrative_plan_constraint(monkeypatch):
    from tests.test_world_constraints import _runtime

    gate = BookStateReviewGate(None)
    monkeypatch.setattr(gate.projection, "load_runtime_as_of", lambda *args, **kwargs: _runtime())
    delta = GraphDelta(id="plan", project_id="p", source_type="plan_constraint", node_patches=[
        NodePatch(node_id="a", node_type="character", op="set", field_path="state.alive_status",
                  old_value="alive", new_value="dead"),
    ])
    verdict = gate.review(ApprovedGraphDeltaSet(project_id="p", chapter_number=0, graph_deltas=[delta]), base_chapter=0)
    assert not verdict.accepted
    assert ("error", "invalid_protection") in _codes(verdict)


@pytest.mark.parametrize("source", ["chapter_extraction", "world_engine"])
def test_gate_reports_protection_violation_on_chapter_delta_only(monkeypatch, source):
    from dataclasses import asdict

    from forwin.book_state.protections import delta_boundary_violations
    from tests.test_world_constraints import _protection, _runtime

    runtime = _runtime(protections=[_protection("must_be_alive")])
    delta = GraphDelta(id="kill", project_id="p", chapter_number=4, source_type=source, node_patches=[
        NodePatch(node_id=ref, node_type="character", op="set", field_path="state.alive_status",
                  old_value="alive", new_value="dead", reason=f"{ref} dies")
        for ref in ("a", "b")
    ])
    gate = BookStateReviewGate(None)
    monkeypatch.setattr(gate.projection, "load_runtime_as_of", lambda *args, **kwargs: runtime)
    verdict = gate.review(ApprovedGraphDeltaSet(project_id="p", chapter_number=4, graph_deltas=[delta]),
                          base_chapter=3, milestone_refs=frozenset({"b"}))
    boundary = [issue for issue in verdict.issues if issue.code in {"protection_violated", "milestone_ref_ended"}]

    if source == "world_engine":
        # A tick's delta is boundary-checked before it is built.
        assert boundary == []
        return
    protection, milestone = delta_boundary_violations(runtime, delta, chapter=4, milestone_refs=frozenset({"b"}))
    assert [(issue.severity, issue.code, issue.target_ref, issue.detail) for issue in boundary] == [
        ("error", "protection_violated", "review_constraint:protection", asdict(protection)),
        ("error", "milestone_ref_ended", "node:b", asdict(milestone)),
    ]
    assert (protection.kind, protection.ref, milestone.kind, milestone.milestone_ref) == (
        "must_be_alive", "a", "milestone", "b",
    )
    assert verdict.accepted is False
    # Without milestone refs only the protection is broken.
    plain = gate.review(ApprovedGraphDeltaSet(project_id="p", chapter_number=4, graph_deltas=[delta]), base_chapter=3)
    assert [issue.code for issue in plain.issues if issue.severity == "error"] == ["protection_violated"]


def test_chapter_review_checks_committed_protections_and_given_milestone_refs():
    from forwin.book_state.projection import BookStateProjection
    from forwin.canon.admission import CanonAdmissionService
    from forwin.planning.beat_sheet.types import ProtectionSpec

    engine, Session = _session_factory()
    try:
        world = _seed_world(Session)
        kill_rival = {"entity_changes": [
            {"ref": world.rival, "field": "alive_status", "new_value": "dead", "evidence": "the rival falls"},
        ]}

        def review(milestone_refs: frozenset[str]) -> BookStateReviewVerdict:
            with Session() as session:
                result = world_review.review_chapter_extraction(
                    session, project_id=world.project_id, chapter_number=1,
                    extraction=ChapterDeltaExtraction.model_validate({
                        "body_sha256": "1" * 64, "time": {"elapsed": None, "evidence": ""}, **kill_rival,
                    }),
                    admission_plan=EntityAdmissionPlan(project_id=world.project_id, chapter_number=1,
                                                       candidate_fingerprint="fingerprint"),
                    chapter_summary="summary", travel_tolerance=0.2, milestone_refs=milestone_refs,
                )
            assert result.gate is not None
            return result.gate

        assert review(frozenset()).issues == []
        with Session.begin() as session:
            CanonAdmissionService().commit_plan_constraints(
                session=session, project_id=world.project_id, beat_sheet_id="sheet-1",
                creates=[ProtectionSpec(ref=world.rival, kind="must_be_alive", until_chapter=0)], releases=[],
            )
            runtime = BookStateProjection(session).load_runtime_as_of(world.project_id, as_of_chapter=0)
            protection_id = next(node.id for node in runtime.narrative.nodes_by_id.values()
                                 if node.node_type == "review_constraint")

        protected = review(frozenset())
        assert [(issue.severity, issue.code, issue.target_ref) for issue in protected.issues] == [
            ("error", "protection_violated", f"review_constraint:{protection_id}"),
        ]
        detail = protected.issues[0].detail
        assert {key: detail[key] for key in ("kind", "ref", "source", "impact_level", "evidence")} == {
            "kind": "must_be_alive", "ref": world.rival, "source": "beat_sheet", "impact_level": "major",
            "evidence": "the rival falls",
        }
        needed = review(frozenset({world.rival}))
        assert [(issue.code, issue.target_ref, issue.detail["impact_level"]) for issue in needed.issues] == [
            ("protection_violated", f"review_constraint:{protection_id}", "critical"),
            ("milestone_ref_ended", f"node:{world.rival}", "critical"),
        ]
    finally:
        engine.dispose()


def test_beat_sheet_reader_target_moves_premature_reveal_boundary():
    from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
    from forwin.planning.beat_sheet.types import ArcBeatSheetData

    engine, Session = _session_factory()
    try:
        world = _seed_world(Session, reveal_candidates=[{"fact_id": "fact-secret", "arc_number": 2}])
        reader_learns = {"knowledge_events": [_knowledge("reader", world.fact("fact-secret"), channel="inferred")]}
        assert _check(Session, world, 6, reader_learns) == []
        with Session.begin() as session:
            arc = session.scalars(select(ArcPlanVersion).where(
                ArcPlanVersion.project_id == world.project_id, ArcPlanVersion.arc_number == 2)).one()
            repo = ArcBeatSheetRepository(session)
            data = ArcBeatSheetData(protagonist_ref=world.hero, phases=[], band_breakdown=[], reveal_targets=[{
                "fact_ref": world.fact("fact-secret"), "observer": "reader", "target_state": "knows", "range": [8, 10],
            }])
            repo.create(world.project_id, arc.id, data, source="llm", reason="delayed reveal")
        assert _check(Session, world, 6, reader_learns) == [("error", "premature_reveal")]
        assert _check(Session, world, 7, reader_learns) == [("error", "premature_reveal")]
        assert _check(Session, world, 8, reader_learns) == []
        with Session.begin() as session:
            repo = ArcBeatSheetRepository(session)
            current = repo.active_for_project(world.project_id)[0]
            data = repo.load(current).model_copy(update={"reveal_targets": [
                repo.load(current).reveal_targets[0].model_copy(update={"range": (7, 10)}),
            ]})
            repo.create(world.project_id, current.arc_plan_id, data, source="llm", reason="earlier reveal")
        assert _check(Session, world, 7, reader_learns) == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("observer,directive_fact,target_state,states,exempt", [
    ("reader", "fact:secret", "knows", ["knows"], True),
    ("actor", "fact:secret", "knows", ["knows"], False),
    ("reader", "fact:other", "knows", ["knows"], False),
    ("reader", "fact:secret", "heard", ["knows"], False),
    ("reader", "fact:secret", "suspects", ["knows"], False),
    ("reader", "fact:secret", "suspects", ["heard"], True),
    ("reader", "fact:secret", "suspects", ["suspects"], True),
    ("reader", "fact:secret", "believes", ["suspects"], True),
    ("reader", "fact:secret", "suspects", ["heard", "knows"], False),
    ("reader", "fact:secret", "invalid", ["heard"], False),
])
def test_reveal_directive_exempts_premature_reveal(monkeypatch, observer, directive_fact, target_state, states, exempt):
    from types import SimpleNamespace
    from forwin.book_state import reviewer
    from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
    from forwin.models.project import ChapterPlan
    from forwin.protocol.book_state import FactNode

    from forwin.book_state.map_graph import MapGraph

    runtime = BookStateRuntime(project_id="p", as_of_chapter=0, map_graph=MapGraph(), world=ObjectiveWorldGraph(facts=[
        FactNode(id="secret", project_id="p", proposition="Secret", sensitivity_level="secret", truth_value="true"),
        FactNode(id="public", project_id="p", proposition="Public", sensitivity_level="public", truth_value="true"),
    ]))
    plan = ChapterPlan(reveal_directives_json=json.dumps([
        {"id": "r", "fact_ref": directive_fact, "observer": observer, "target_state": target_state},
    ]))
    session = SimpleNamespace(scalar=lambda query: plan)
    gate = BookStateReviewGate(session)
    monkeypatch.setattr(gate.projection, "load_runtime_as_of", lambda *args, **kwargs: runtime)
    monkeypatch.setattr(reviewer, "earliest_reveal_chapters", lambda *args: {"secret": 6})
    delta = GraphDelta(id="delta", project_id="p", chapter_number=1, source_type="chapter_extraction", metadata={
        "knowledge_tags": [{"observer_type": "reader", "observer_id": "reader", "fact_ref": "fact:public",
                            "touches_secret_ref": "fact:secret", "contradicts_ref": "fact:secret",
                            "state": state, "channel": "inferred"} for state in states],
    })
    verdict = gate.review(ApprovedGraphDeltaSet(project_id="p", chapter_number=1, graph_deltas=[delta]), base_chapter=0)
    codes = [issue.code for issue in verdict.issues]
    assert ("premature_reveal" not in codes) is exempt
    assert "contradicts_hidden_fact" in codes


def test_reveal_directive_exemption_uses_current_chapter_plan_postgres():
    from forwin.models.project import ChapterPlan
    from forwin.planning.beat_sheet.types import READER_OBSERVER

    engine, Session = _session_factory()
    try:
        world = _seed_world(Session, reveal_candidates=[{"fact_id": "fact-secret", "arc_number": 2}])
        secret = world.fact("fact-secret")
        fields = {"knowledge_events": [_knowledge(READER_OBSERVER, secret, channel="inferred")]}
        assert _check(Session, world, 1, fields) == [("error", "premature_reveal")]
        with Session.begin() as session:
            arc = session.scalars(select(ArcPlanVersion).where(ArcPlanVersion.project_id == world.project_id,
                                                              ArcPlanVersion.arc_number == 1)).one()
            session.add(ChapterPlan(project_id=world.project_id, arc_plan_id=arc.id, chapter_number=1,
                reveal_directives_json=json.dumps([{"id": "r", "fact_ref": secret,
                                                   "observer": READER_OBSERVER, "target_state": "knows"}])))
        assert _check(Session, world, 1, fields) == []
        assert _check(Session, world, 2, fields) == [("error", "premature_reveal")]
    finally:
        engine.dispose()


def test_malformed_reveal_directives_log_chapter(caplog):
    from types import SimpleNamespace
    from forwin.book_state.reviewer import chapter_reveal_directives
    from forwin.models.project import ChapterPlan

    session = SimpleNamespace(scalar=lambda query: ChapterPlan(reveal_directives_json="{invalid"))
    assert chapter_reveal_directives(session, "project-corrupt", 7) == []
    assert "project-corrupt" in caplog.text and "chapter=7" in caplog.text


def test_review_pass_loads_one_runtime(monkeypatch):
    from forwin.book_state.projection import BookStateProjection

    engine, Session = _session_factory()
    world = _seed_world(Session)
    original = BookStateProjection.load_runtime_as_of
    loads = []
    def load(self, *args, **kwargs):
        loads.append(kwargs["as_of_chapter"])
        return original(self, *args, **kwargs)
    monkeypatch.setattr(BookStateProjection, "load_runtime_as_of", load)
    with Session() as session:
        result = world_review.review_chapter_extraction(
            session, project_id=world.project_id, chapter_number=1,
            extraction=ChapterDeltaExtraction.model_validate({"body_sha256": "a" * 64, **_time(1, "日")}),
            admission_plan=EntityAdmissionPlan(project_id=world.project_id, chapter_number=1,
                                               candidate_fingerprint="fingerprint"),
            chapter_summary="summary", travel_tolerance=0.2,
        )
    assert result.gate is not None and result.gate.accepted
    assert loads == [0]
    engine.dispose()


def test_review_pass_reuses_real_loader_with_memory_repository(monkeypatch):
    from types import SimpleNamespace
    from forwin.book_state import projection
    from forwin.book_state.book_frame_repository import BookLexiconRepository, StoryCalendarRepository
    from forwin.book_state.lexicon import BookLexicon
    from forwin.book_state.map_graph import MapGraph
    from forwin.book_state.narrative import NarrativeControlGraph
    from forwin.book_state.query import BookStateQuery
    from forwin.book_state.runtime import ObjectiveWorldGraph

    repo = SimpleNamespace(
        latest_world_snapshot=lambda *args: None, latest_map_snapshot=lambda *args: None,
        load_cognition_views=lambda *args, **kwargs: {},
        load_base_world_graph=lambda *args, **kwargs: ObjectiveWorldGraph(),
        load_base_map_graph=lambda *args, **kwargs: MapGraph(),
        load_narrative_graph=lambda *args, **kwargs: NarrativeControlGraph(),
        list_graph_deltas=lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(projection, "BookStateRepository", lambda session: repo)
    monkeypatch.setattr(StoryCalendarRepository, "get", lambda *args: None)
    monkeypatch.setattr(BookLexiconRepository, "get", lambda *args: BookLexicon())
    monkeypatch.setattr(BookStateQuery, "current_story_minute", lambda *args, **kwargs: 0)
    original = projection.BookStateProjection.load_runtime_as_of
    loads = []
    def load(self, *args, **kwargs):
        loads.append(kwargs["as_of_chapter"])
        return original(self, *args, **kwargs)
    monkeypatch.setattr(projection.BookStateProjection, "load_runtime_as_of", load)
    def run():
        return world_review.review_chapter_extraction(
            None, project_id="p", chapter_number=3,
            extraction=ChapterDeltaExtraction.model_validate({"body_sha256": "a" * 64,
                "time": {"elapsed": None, "evidence": ""}}),
            admission_plan=EntityAdmissionPlan(project_id="p", chapter_number=3, candidate_fingerprint="f"),
            chapter_summary="summary", travel_tolerance=0.2,
        )
    reused = run()
    assert reused.gate is not None and reused.gate.accepted
    assert loads == [2]
    translate, review = ChapterDeltaTranslator.translate, BookStateReviewGate.review
    def self_translate(self, **kwargs):
        return translate(self, **{**kwargs, "runtime": None})
    def self_review(self, changes, **kwargs):
        return review(self, changes, **{**kwargs, "runtime": None})
    monkeypatch.setattr(ChapterDeltaTranslator, "translate", self_translate)
    monkeypatch.setattr(BookStateReviewGate, "review", self_review)
    loads.clear()
    independent = run()
    assert independent == reused
    assert loads == [2, 2, 2]


@pytest.mark.parametrize("hard,severity", [(True, "error"), (False, "warning")])
def test_gate_flags_rule_violation_hard_and_soft(monkeypatch, hard, severity):
    from tests.test_rule_predicates import rule
    from tests.test_world_constraints import _runtime
    runtime = _runtime(rule("actor_cannot_enter", hard=hard, location_ref="l2"))
    gate = BookStateReviewGate(None)
    monkeypatch.setattr(gate.projection, "load_runtime_as_of", lambda *a, **kw: runtime)
    delta = GraphDelta(id="d", project_id="p", chapter_number=4, source_type="chapter_extraction",
        node_patches=[NodePatch(node_id="a", node_type="character", op="set", field_path="state.location_id", old_value="l1", new_value="l2")])
    verdict = gate.review(ApprovedGraphDeltaSet(project_id="p", chapter_number=4, graph_deltas=[delta]), base_chapter=3)
    assert _codes(verdict) == [(severity, "rule_violated")]
    assert verdict.issues[0].target_ref == "rule:r"
    assert verdict.accepted is (not hard)


def test_unknown_rule_kind_warns(monkeypatch):
    from tests.test_rule_predicates import rule
    from tests.test_world_constraints import _runtime
    gate = BookStateReviewGate(None)
    monkeypatch.setattr(gate.projection, "load_runtime_as_of", lambda *a, **kw: _runtime(rule("future_kind")))
    delta = GraphDelta(id="d", project_id="p", chapter_number=4, source_type="world_engine")
    verdict = gate.review(ApprovedGraphDeltaSet(project_id="p", chapter_number=4, graph_deltas=[delta]), base_chapter=3)
    assert _codes(verdict) == [("warning", "unknown_rule_predicate")]
    assert verdict.accepted


@pytest.mark.parametrize("recorded", [True, False])
def test_gate_reports_movement_rule_blocked(monkeypatch, recorded):
    from tests.test_rule_predicates import rule
    from tests.test_world_constraints import _runtime, LEXICON
    gate = BookStateReviewGate(None)
    runtime = _runtime(rule("requires_travel_mode", edge_types=["road"], mode_ids=["m"]))
    monkeypatch.setattr(gate.projection, "load_runtime_as_of", lambda *a, **kw: runtime)
    monkeypatch.setattr("forwin.book_state.reviewer.BookLexiconRepository.get", lambda *a: LEXICON)
    delta = GraphDelta(id="d", project_id="p", chapter_number=4, source_type="world_engine",
        story_time_start=600, story_time_end=1200,
        node_patches=[NodePatch(node_id="a", node_type="character", op="set", field_path="state.location_id",
                               old_value="l1", new_value="l2")],
        metadata={"movements": [{"entity_id": "a", "from_id": "l1", "to_id": "l2"}]} if recorded else {})
    verdict = gate.review(ApprovedGraphDeltaSet(project_id="p", chapter_number=4, graph_deltas=[delta]), base_chapter=3)
    assert ("error", "movement_rule_blocked") in _codes(verdict)
    assert ("error", "movement_unreachable") not in _codes(verdict)


@pytest.mark.parametrize("hard", [True, False])
def test_gate_rule_predicate_postgres(hard):
    from forwin.protocol.book_state import WorldNode
    engine, Session = _session_factory()
    world = _seed_world(Session)
    with Session.begin() as session:
        BookStateRepository(session).create_world_node(WorldNode(
            id=f"entry-rule-{world.project_id}", project_id=world.project_id, node_type="rule", name="r",
            profile={"hardness": "hard" if hard else "soft", "checkable": {
                "kind": "actor_cannot_enter", "location_ref": world.loc(2),
            }}, state={"status": "active"}), version_chapter=0, version_state={"status": "active"})
    verdict = _review(Session, world, _delta(Session, world, 1, _day_of_moves(_move(world.hero, world.loc(1), world.loc(2)))))
    assert ("error" if hard else "warning", "rule_violated") in _codes(verdict)
    assert verdict.accepted is (not hard)
    engine.dispose()


def _travel_rule_fixture(monkeypatch, *, modes=(), hard=True):
    from forwin.book_state.lexicon import BookLexicon, TravelMode
    from tests.test_rule_predicates import rule
    from tests.test_world_constraints import _runtime

    runtime = _runtime(rule("requires_travel_mode", hard=hard, edge_types=["road"], mode_ids=["m"]))
    runtime.world.states_by_node_id["a"].update(
        travel_mode_ids=list(modes), restraint={"state": "free", "by_ref": "", "since": None},
    )
    lexicon = BookLexicon(travel_modes=(TravelMode("m", "", 2, ("road",)),))
    monkeypatch.setattr("forwin.book_state.reviewer.BookLexiconRepository.get", lambda *a: lexicon)
    return runtime, lexicon


def _review_travel_delta(runtime, patches, movements=()):
    delta = GraphDelta(id="travel", project_id="p", chapter_number=4, source_type="world_engine",
        story_time_start=600, story_time_end=1200, node_patches=patches,
        metadata={"movements": list(movements)})
    return BookStateReviewGate(None).review(
        ApprovedGraphDeltaSet(project_id="p", chapter_number=4, graph_deltas=[delta]),
        runtime=runtime, base_chapter=3,
    )


@pytest.mark.parametrize("op", ["merge", "set"])
@pytest.mark.parametrize("rule_count", [1, 2])
def test_whole_state_movement_enforces_combined_travel_rules(monkeypatch, op, rule_count):
    from copy import deepcopy
    from forwin.book_state.map_graph import MapGraph
    from forwin.world_engine.travel import estimate_travel, estimate_travel_from
    from tests.test_rule_predicates import rule

    runtime, lexicon = _travel_rule_fixture(monkeypatch)
    if rule_count == 2:
        runtime.world.add_node(rule("requires_travel_mode", edge_types=["path"], mode_ids=["m"])
                               .model_copy(update={"id": "r2"}))
        runtime.map = MapGraph(nodes=list(runtime.map.nodes_by_id.values()), edges=[
            *runtime.map.edges_by_id.values(),
            MapEdge(id="alt", project_id="p", from_node_id="l1", to_node_id="l2",
                    edge_type="path", travel_time=4),
        ])
    before = deepcopy(runtime.world.states_by_node_id)
    state = runtime.world.get_state("a")
    value = {"location_id": "l2"} if op == "merge" else {**state, "location_id": "l2"}
    verdict = _review_travel_delta(runtime, [NodePatch(node_id="a", node_type="character", op=op,
        field_path="state", old_value=state, new_value=value)])
    estimate = estimate_travel(runtime, lexicon, mover_id="a", from_location_id="l1", to_location_id="l2")
    assert estimate_travel_from(runtime, lexicon, mover_id="a", from_location_id="l1")["l2"] == estimate
    assert not estimate.reachable and estimate.minutes is None
    assert not verdict.accepted
    assert ("error", "movement_rule_blocked") in _codes(verdict)
    assert set(_codes(verdict)) <= {("error", "movement_rule_blocked"), ("error", "rule_violated")}
    assert runtime.world.states_by_node_id == before


@pytest.mark.parametrize("recorded", [True, False])
def test_soft_travel_rule_warns_on_return_leg(monkeypatch, recorded):
    from forwin.book_state.map_graph import MapGraph
    from forwin.world_engine.travel import estimate_travel, estimate_travel_from

    runtime, lexicon = _travel_rule_fixture(monkeypatch, hard=False)
    runtime.map = MapGraph(nodes=list(runtime.map.nodes_by_id.values()), edges=[
        MapEdge(id="out", project_id="p", from_node_id="l1", to_node_id="l2",
                edge_type="path", travel_time=2),
        MapEdge(id="back", project_id="p", from_node_id="l2", to_node_id="l1",
                edge_type="road", travel_time=2),
    ])
    patches = [NodePatch(node_id="a", node_type="character", op="set", field_path="state.location_id",
                         old_value="l1", new_value=target) for target in ("l2", "l1")]
    movements = [
        {"entity_id": "a", "from_id": "l1", "to_id": "l2", "depart_minute": 600, "arrive_minute": 720},
        {"entity_id": "a", "from_id": "l2", "to_id": "l1", "depart_minute": 720, "arrive_minute": 840},
    ]
    verdict = _review_travel_delta(runtime, patches, movements if recorded else ())
    assert verdict.accepted
    assert _codes(verdict) == [("warning", "rule_violated")]
    for start, target in [("l1", "l2"), ("l2", "l1")]:
        travel = estimate_travel(runtime, lexicon, mover_id="a", from_location_id=start, to_location_id=target)
        assert travel.reachable and travel.minutes == 120
        assert estimate_travel_from(runtime, lexicon, mover_id="a", from_location_id=start)[target] == travel


@pytest.mark.parametrize("recorded", [True, False])
@pytest.mark.parametrize("gain,before_move", [(True, True), (False, True), (True, False), (False, False)])
def test_travel_rules_use_modes_at_movement(monkeypatch, recorded, gain, before_move):
    from copy import deepcopy

    initial, updated = ([], ["m"]) if gain else (["m"], [])
    runtime, _ = _travel_rule_fixture(monkeypatch, modes=initial)
    before = deepcopy(runtime.world.states_by_node_id)
    mode = NodePatch(node_id="a", node_type="character", op="set", field_path="state.travel_mode_ids",
                     old_value=initial, new_value=updated)
    move = NodePatch(node_id="a", node_type="character", op="set", field_path="state.location_id",
                     old_value="l1", new_value="l2")
    movements = [{"entity_id": "a", "from_id": "l1", "to_id": "l2",
                  "depart_minute": 600, "arrive_minute": 660}]
    verdict = _review_travel_delta(runtime, [mode, move] if before_move else [move, mode],
                                  movements if recorded else ())
    allowed = gain if before_move else not gain
    assert verdict.accepted is allowed, _codes(verdict)
    assert _codes(verdict) == ([] if allowed else [
        ("error", "movement_rule_blocked"), ("error", "rule_violated"),
    ])
    assert runtime.world.states_by_node_id == before


@pytest.mark.parametrize("gain", [True, False])
def test_cumulative_tick_travel_uses_prior_candidates_modes(monkeypatch, gain):
    from forwin.world_engine.running_state import RunningStateCheck
    from forwin.world_engine.types import TickContext
    from tests.test_world_constraints import CALENDAR, _candidate, _change, _movement

    initial, updated = ([], ["m"]) if gain else (["m"], [])
    runtime, lexicon = _travel_rule_fixture(monkeypatch, modes=initial)
    monkeypatch.setattr("forwin.world_engine.delta_builder.StoryCalendarRepository.get", lambda *a: CALENDAR)
    check = RunningStateCheck(None,
        context=TickContext(project_id="p", chapter_number=3, now=1000, window_start=600, seed=1),
        runtime=runtime, lexicon=lexicon, calendar=CALENDAR, tick_id="tick", travel_tolerance=0.2)
    assert check.admit(_candidate(_change("a", "travel_mode_ids", updated), candidate_id="equip")) == []
    move = _candidate({"movements": [_movement(depart_at=800, arrive_at=920)]}, candidate_id="move")
    issues = check.admit(move)
    if gain:
        assert issues == []
        assert check.runtime.world.get_state("a")["location_id"] == "l2"
    else:
        assert "movement_rule_blocked" in [issue.code for issue in issues]
        assert "rule_violated" in [issue.code for issue in issues]
        assert check.runtime.world.get_state("a")["location_id"] == "l1"
    assert runtime.world.get_state("a")["travel_mode_ids"] == initial


@pytest.mark.parametrize("node_type", ["faction", "organization", "group", "site_state"])
@pytest.mark.parametrize("operation", ["create", "set"])
def test_group_status_enum_rejects_invented_status(monkeypatch, node_type, operation):
    from forwin.book_state.map_graph import MapGraph
    from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
    from forwin.protocol.book_state import WorldNode
    from forwin.book_state.protections import ENDED_STATUSES

    node = WorldNode(id="subject", project_id="p", node_type=node_type, name="Subject", state={"status": "active"})
    runtime = BookStateRuntime(project_id="p", as_of_chapter=0, world=ObjectiveWorldGraph(
        nodes=[node] if operation == "set" else [],
    ), map_graph=MapGraph())
    gate = BookStateReviewGate(None)
    monkeypatch.setattr(gate.projection, "load_runtime_as_of", lambda *args, **kwargs: runtime)
    for status in ["invented-status", "active", *sorted(ENDED_STATUSES)]:
        patch = NodePatch(node_id=node.id, node_type=node_type, op=operation, **(
            {"new_value": {**node.model_dump(mode="json"), "state": {"status": status}}}
            if operation == "create" else {"field_path": "state.status", "old_value": "active", "new_value": status}
        ))
        verdict = gate.review(ApprovedGraphDeltaSet(project_id="p", chapter_number=0, graph_deltas=[
            GraphDelta(id="delta", project_id="p", source_type="world_edit", node_patches=[patch]),
        ]), base_chapter=0)
        assert verdict.accepted is (status != "invented-status"), verdict.issues
        if status == "invented-status":
            assert ("error", "invalid_field_value") in _codes(verdict)


@pytest.mark.parametrize("patches,accepted", [
    ([("state.level", 2)], True),
    ([("state.level", 3)], False),
    ([("profile.scale", ["low", "high"])], False),
    ([("state.level", 1), ("profile.scale", ["low", "high"])], True),
    ([("profile.scale", ["low", "high"]), ("state.level", 1)], True),
    ([("profile", {"label": "Supply", "scale": ["low", "high"]})], False),
    ([("state", {"level": 3})], False),
    ([("profile.scale", ["low", "low", "high"])], False),
])
def test_world_variable_gate_uses_stored_and_patched_scale(patches, accepted):
    from copy import deepcopy
    from tests.test_world_constraints import _runtime
    from forwin.protocol.book_state import WorldNode

    runtime = _runtime()
    variable = WorldNode(id="variable", node_type="world_variable", project_id="p",
                         profile={"label": "Supply", "scale": ["low", "normal", "high"]}, state={"level": 2})
    runtime.world.nodes_by_id[variable.id] = variable
    before = deepcopy(variable.model_dump())
    delta = GraphDelta(id="change", project_id="p", chapter_number=4, source_type="manual", node_patches=[
        NodePatch(node_id="variable", node_type="world_variable", op="set", field_path=field,
                  old_value=2 if field == "state.level" else None, new_value=value)
        for field, value in patches
    ])
    verdict = BookStateReviewGate(None).review(
        ApprovedGraphDeltaSet(project_id="p", chapter_number=4, graph_deltas=[delta]), base_chapter=3, runtime=runtime)
    assert verdict.accepted is accepted, verdict.issues
    assert variable.model_dump() == before


@pytest.mark.parametrize("scale,level,accepted", [
    (["low", "high"], 1, True), (["low"], 0, False),
    (list("abcdefgh"), 0, False), (["low", "low"], 0, False),
    (["low", "high"], 2, False), (["low", "high"], -1, False),
])
def test_world_variable_gate_create(scale, level, accepted):
    from tests.test_world_constraints import _runtime

    delta = GraphDelta(id="create", project_id="p", chapter_number=4, source_type="manual", node_patches=[
        NodePatch(node_id="variable", node_type="world_variable", op="create",
                  new_value={"project_id": "p", "profile": {"label": "Supply", "scale": scale}, "state": {"level": level}})])
    verdict = BookStateReviewGate(None).review(
        ApprovedGraphDeltaSet(project_id="p", chapter_number=4, graph_deltas=[delta]), base_chapter=3, runtime=_runtime())
    assert verdict.accepted is accepted, verdict.issues


@pytest.mark.parametrize("op,path,value,accepted", [
    ("merge", "state", {"level": 3}, False),
    ("merge", "profile", {"scale": ["low", "high"]}, False),
    ("append", "profile.scale", "high", False),
    ("append", "profile.scale", "peak", True),
    ("remove", "profile.scale", "normal", False),
    ("remove", "state.level", None, False),
])
def test_world_variable_gate_checks_result_of_collection_and_section_patches(op, path, value, accepted):
    from tests.test_world_constraints import _runtime
    from forwin.protocol.book_state import WorldNode

    runtime = _runtime()
    runtime.world.nodes_by_id["variable"] = WorldNode(
        id="variable", project_id="p", node_type="world_variable",
        profile={"label": "Supply", "scale": ["low", "normal", "high"]}, state={"level": 0})
    runtime.world.states_by_node_id["variable"] = {"level": 2}
    delta = GraphDelta(id="patch", project_id="p", chapter_number=4, source_type="manual", node_patches=[
        NodePatch(node_id="variable", node_type="world_variable", op=op, field_path=path,
                  old_value=2 if path == "state.level" else None, new_value=value)])
    verdict = BookStateReviewGate(None).review(
        ApprovedGraphDeltaSet(project_id="p", chapter_number=4, graph_deltas=[delta]), base_chapter=3, runtime=runtime)
    assert verdict.accepted is accepted, verdict.issues
    assert runtime.world.states_by_node_id["variable"] == {"level": 2}


@pytest.mark.parametrize("section,field,value", [
    ("profile", "source_kind", "unknown"), ("profile", "visibility", "hidden"),
    ("profile", "drift_tendency", "unknown"), ("state", "trend", "volatile"),
    ("state", "last_drift_at", -1), ("state", "last_drift_at", 1.5),
    ("state", "level", True), ("state", "level", 1.5),
])
def test_world_variable_schema_rejects_invalid_fields(section, field, value):
    from forwin.book_state.schema import validate_world_node
    from forwin.protocol.book_state import WorldNode

    payload = {"profile": {"label": "Supply", "scale": ["low", "high"]}, "state": {"level": 0}}
    payload[section][field] = value
    report = validate_world_node(WorldNode(id="v", project_id="p", node_type="world_variable", **payload))
    assert any(issue.code == "invalid_field_value" and issue.target.endswith(f":{section}.{field}")
               for issue in report.errors)


@pytest.mark.parametrize("op", ["set", "merge"])
@pytest.mark.parametrize("scale_first", [False, True])
def test_world_variable_whole_state_projection_preserves_range(op, scale_first):
    from copy import deepcopy
    from forwin.book_state.compiler import _node_current
    from forwin.book_state.projection import BookStateProjection
    from forwin.protocol.book_state import WorldNode
    from tests.test_world_constraints import _runtime

    runtime = _runtime()
    runtime.world.add_node(WorldNode(
        id="variable", project_id="p", node_type="world_variable",
        profile={"label": "Supply", "scale": ["low", "normal", "high"]}, state={"level": 0}))
    runtime.world.states_by_node_id["variable"] = {"level": 2, "trend": "stable"}
    before = deepcopy(runtime.world.snapshot())
    patches = [
        NodePatch(node_id="variable", node_type="world_variable", op=op, field_path="state",
                  old_value={"level": 2, "trend": "stable"}, new_value={"level": 1}),
        NodePatch(node_id="variable", node_type="world_variable", op="set", field_path="profile.scale",
                  old_value=["low", "normal", "high"], new_value=["low", "high"]),
    ]
    if scale_first:
        patches.reverse()
    for patch in patches:
        assert _node_current(runtime, patch) == patch.old_value
    delta = GraphDelta(id="change", project_id="p", chapter_number=4, source_type="manual", node_patches=patches)
    changes = ApprovedGraphDeltaSet(project_id="p", chapter_number=4, graph_deltas=[delta])
    changes_before = changes.model_dump()
    verdict = BookStateReviewGate(None).review(changes, base_chapter=3, runtime=runtime)
    assert verdict.accepted, verdict.issues
    assert runtime.world.snapshot() == before
    assert changes.model_dump() == changes_before

    projected = deepcopy(runtime)
    BookStateProjection(None).apply_delta_to_runtime(projected, delta)
    state = projected.world.get_state("variable")
    scale = projected.world.get_node("variable").profile["scale"]
    assert 0 <= state["level"] < len(scale)
    assert state == ({"level": 1, "trend": "stable"} if op == "merge" else {"level": 1})
    assert runtime.world.snapshot() == before


@pytest.mark.parametrize("level", [2, 3])
def test_world_variable_complete_node_rejects_out_of_range_level(level):
    from forwin.book_state.schema import validate_world_node
    from forwin.protocol.book_state import WorldNode

    report = validate_world_node(WorldNode(
        id="variable", project_id="p", node_type="world_variable",
        profile={"label": "Supply", "scale": ["low", "high"]}, state={"level": level}))
    assert any(issue.code == "world_variable_level_out_of_range" for issue in report.errors)


@pytest.mark.parametrize("override,level", [({"node_type": "world_variable"}, 3),
                                           ({"node_type": "world_variable"}, 1),
                                           ({"id": "other-variable"}, 1)])
def test_world_variable_create_rejects_conflicting_identity(override, level):
    from forwin.book_state.schema import validate_graph_delta
    from tests.test_world_constraints import _runtime

    delta = GraphDelta(id="create", project_id="p", chapter_number=4, source_type="manual", node_patches=[
        NodePatch(node_id="variable", node_type="item" if "node_type" in override else "world_variable", op="create",
                  new_value={"project_id": "p", "profile": {"label": "Supply", "scale": ["low", "high"]},
                             "state": {"level": level}, **override})])
    runtime = _runtime()
    before = runtime.world.snapshot()
    verdict = BookStateReviewGate(None).review(
        ApprovedGraphDeltaSet(project_id="p", chapter_number=4, graph_deltas=[delta]), base_chapter=3, runtime=runtime)
    assert not verdict.accepted
    assert ("error", "invalid_node_create_patch") in _codes(verdict)
    assert runtime.world.snapshot() == before
    assert not validate_graph_delta(delta).ok
