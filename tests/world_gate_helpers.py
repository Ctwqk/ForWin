"""Shared world fixtures for chapter extraction and review tests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from forwin.book_state import BookStateRepository
from forwin.genesis.handoff.world_seed_committer import WorldSeedCommitter, seed_ref_id
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.genesis import BookGenesisRevision
from forwin.models.project import ArcPlanVersion
from forwin.protocol.book_state import MapEdge, MapNode
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
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
            "map_atlas": {"nodes": [{"id": f"node-{number}", "name": f"node-{number}"} for number in range(1, 6)]},
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
        session.add_all([ArcPlanVersion(project_id=project.id, arc_synopsis="arc", **arc) for arc in TWO_ARCS])
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


def _knowledge(observer: str, fact_ref: str = "", **extra: Any) -> dict[str, Any]:
    return {
        "observer_ref": observer, "fact_ref": fact_ref, "state": "knows", "channel": "witnessed",
        "evidence": "e", **extra,
    }

