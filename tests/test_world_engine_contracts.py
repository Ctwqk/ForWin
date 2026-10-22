from __future__ import annotations

import subprocess
import sys

import pytest
from pydantic import ValidationError

from forwin.book_state.book_frame_repository import BookLexiconRepository
from forwin.book_state.projection import BookStateProjection
from forwin.world_engine.travel import TravelEstimate, estimate_travel
from forwin.world_engine.types import (
    ActorTurnResult,
    AgendaChange,
    EffectKnowledge,
    ProposedIntent,
    TickCandidate,
    WorldEffects,
    tick_seed,
)
from tests.test_world_gate_v6 import _seed_world, _session_factory, _World


def _intent(**overrides) -> dict:
    return {
        "agenda_ref": "agenda-1",
        "action_type": "negotiate",
        "summary": "Seek terms.",
        "target_refs": ["actor-2"],
        "location_id": "place-1",
        "scheduled_in": {"amount": 2, "unit": "day"},
        "requires_onstage": False,
        "impact_level": "moderate",
        "expected_effects": {},
        **overrides,
    }


def _knowledge(**overrides) -> dict:
    return {"observer_ref": "actor-1", "fact_ref": "fact:1", "state": "knows", "channel": "told", **overrides}


def test_world_effects_reject_unknown_action_type_and_knowledge_state() -> None:
    assert ProposedIntent.model_validate(_intent()).action_type == "negotiate"
    with pytest.raises(ValidationError):
        ProposedIntent.model_validate(_intent(action_type="teleport"))
    with pytest.raises(ValidationError):
        ProposedIntent.model_validate(_intent(impact_level="cosmic"))
    with pytest.raises(ValidationError):
        ProposedIntent.model_validate(_intent(unexpected="x"))

    assert EffectKnowledge.model_validate(_knowledge()).source_ref == ""
    for bad in ({"state": "omniscient"}, {"channel": "telepathy"}, {"fact_ref": "1"}):
        with pytest.raises(ValidationError):
            EffectKnowledge.model_validate(_knowledge(**bad))
    with pytest.raises(ValidationError):
        WorldEffects.model_validate({"knowledge": [_knowledge(state="omniscient")]})
    with pytest.raises(ValidationError):
        WorldEffects.model_validate({"relation_changes": [
            {"source_ref": "actor-1", "target_ref": "actor-2", "edge_type": "no_such_edge", "change": "create"},
        ]})
    with pytest.raises(ValidationError):
        WorldEffects.model_validate({"movements": [
            {"entity_ref": "actor-1", "to_location_id": "place-2", "depart_at": 10, "arrive_at": 5},
        ]})
    with pytest.raises(ValidationError):
        TickCandidate(candidate_id="c-1", origin="clock", summary="s", effects=WorldEffects(), impact_level="cosmic")
    with pytest.raises(ValidationError):
        TickCandidate(
            candidate_id="c-1", origin="clock", summary="s", effects=WorldEffects(),
            intent_status_changes={"intent-1": "forgotten"},
        )


def test_world_effects_new_fact_refs_resolve_within_the_effects() -> None:
    fact = {"key": "k1", "proposition": "p", "truth_value": True, "sensitivity": "secret", "known_by": ["actor-1"]}

    effects = WorldEffects.model_validate({"new_facts": [fact], "knowledge": [_knowledge(fact_ref="fact:new:k1")]})

    assert effects.knowledge[0].fact_ref == "fact:new:k1"
    with pytest.raises(ValidationError):
        WorldEffects.model_validate({"new_facts": [fact], "knowledge": [_knowledge(fact_ref="fact:new:k2")]})
    with pytest.raises(ValidationError):
        WorldEffects.model_validate({"new_facts": [fact, fact]})


def test_agenda_change_new_requires_goal() -> None:
    with pytest.raises(ValidationError):
        AgendaChange(agenda_ref="new")
    with pytest.raises(ValidationError):
        AgendaChange(agenda_ref="new", goal="  ")
    with pytest.raises(ValidationError):
        AgendaChange(agenda_ref="agenda-1", status="forgotten")

    assert AgendaChange(agenda_ref="new", goal="Hold the pass.").priority is None
    assert AgendaChange(agenda_ref="agenda-1", status="abandoned").goal == ""
    result = ActorTurnResult.model_validate({
        "actor_id": "actor-1",
        "new_intents": [_intent()],
        "agenda_changes": [{"agenda_ref": "new", "goal": "Hold the pass."}],
        "next_turn_in": {"amount": 1, "unit": "day"},
    })
    assert result.new_intents[0].scheduled_in.amount == 2


def test_tick_seed_is_stable_and_project_scoped() -> None:
    assert tick_seed("project-1", 3) == 336080573
    assert tick_seed("project-1", 3) == tick_seed("project-1", 3)
    assert tick_seed("project-1", 4) != tick_seed("project-1", 3)
    assert tick_seed("project-2", 3) != tick_seed("project-1", 3)
    assert 0 <= tick_seed("project-2", 3) < 2_000_000_000


def _estimate(Session, world: _World, mover: str, from_number: int, to_number: int) -> TravelEstimate:
    with Session() as session:
        runtime = BookStateProjection(session).load_runtime_as_of(world.project_id, as_of_chapter=0)
        lexicon = BookLexiconRepository(session).get(world.project_id)
        return estimate_travel(
            runtime, lexicon, mover_id=mover,
            from_location_id=world.loc(from_number), to_location_id=world.loc(to_number),
        )


def test_estimate_travel_matches_gate_cases() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    horse = {"id": "mode-horse", "name": "", "speed_multiplier": 4.0, "allowed_edge_types": ["road"]}
    road_only = _seed_world(Session, travel_modes=[horse], hero_modes=["mode-horse"])
    with_pass = _seed_world(
        Session, travel_modes=[{**horse, "allowed_edge_types": ["road", "mountain_pass"]}], hero_modes=["mode-horse"],
    )
    long_road = _seed_world(
        Session, travel_modes=[horse], hero_modes=["mode-horse"],
        extra_edges=(("long-road", 2, 4, "road", 10.0, True),),
    )

    # Known route: the 2 h road walked at 1.0.
    assert _estimate(Session, world, world.hero, 1, 2) == TravelEstimate(
        reachable=True, minutes=120.0, route_known=True, blocked_reason="",
    )
    # The hero's overlay does not know the hidden route; the rival has no overlay, so the objective map applies.
    unknown = _estimate(Session, world, world.hero, 1, 3)
    assert (unknown.reachable, unknown.route_known, unknown.minutes) == (True, False, None)
    assert unknown.blocked_reason
    assert _estimate(Session, world, world.rival, 2, 3).minutes == 60.0
    # Unreachable: loc 5 is isolated.
    unreachable = _estimate(Session, world, world.hero, 1, 5)
    assert (unreachable.reachable, unreachable.route_known, unreachable.minutes) == (False, False, None)
    assert unreachable.blocked_reason == "no accessible path"
    # By travel mode: road 2 h + pass 4 h is 360 min on foot; the horse may not take the pass,
    # may with it allowed (90 min), and may take a longer all-road route (12 h / 4 = 180 min).
    assert _estimate(Session, road_only, road_only.hero, 1, 4).minutes == 360.0
    assert _estimate(Session, with_pass, with_pass.hero, 1, 4).minutes == 90.0
    assert _estimate(Session, long_road, long_road.hero, 1, 4).minutes == 180.0
    engine.dispose()


@pytest.mark.parametrize("module", ["forwin.world_engine.travel", "forwin.world_engine.types"])
def test_world_engine_modules_import_first(module: str) -> None:
    # The world gate calls into world_engine.travel, so a fresh import must not cycle through it.
    result = subprocess.run([sys.executable, "-c", f"import {module}"], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
