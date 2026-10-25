from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from forwin.book_state import BookStateCompiler, BookStateProjection, BookStateRepository
from forwin.book_state.book_frame_repository import StoryCalendarRepository
from forwin.book_state.extraction.chapter_delta_schema import ChapterDeltaExtraction, RelationChange
from forwin.book_state.extraction.translator import ChapterDeltaTranslator, TranslationResult
from forwin.book_state.runtime import resolve_world_node_location_id
from forwin.book_state.story_clock import format_story_time
from forwin.genesis.handoff.world_seed_committer import WorldSeedCommitter, seed_ref_id
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.genesis import BookGenesisRevision
from forwin.naming.types import EntityAdmissionDecision, EntityAdmissionPlan
from forwin.protocol.book_state import (
    ApprovedGraphDeltaSet,
    CognitionPatch,
    EdgePatch,
    GraphDelta,
    MapEdge,
    MapNode,
    NarrativePatch,
    NodePatch,
)
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url


OPENING = 1000
REGION = "region-1"
SUBWORLD = "subworld-1"


def _pack() -> dict[str, Any]:
    story_engine = {
        "core_cast": [{
            "id": "cast-1", "name": "甲", "role": "role-cast", "desire": "desire-cast",
            "fear": "fear-cast", "culture_profile_id": "", "affiliated_faction": "faction-1",
            "current_base": "node-1",
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
            "appellations": [], "generic_character_refs": [],
            "travel_modes": [{"id": "mode-walk", "name": "", "speed_multiplier": 1.0, "allowed_edge_types": ["road"]}],
            "news_speed_multiplier": 1.0,
        },
        "actors": [
            {
                "ref": "cast-1", "actor_tier": "major", "location_ref": "node-1",
                "travel_mode_ids": ["mode-walk"], "resources": [],
                "agendas": [{
                    "id": "agenda-1", "goal": "agenda-goal", "motivation": "agenda-motivation",
                    "priority": 3, "horizon": "arc", "means_refs": [], "constraints": [], "secrecy_refs": [],
                }],
                "intents": [{
                    "id": "intent-1", "agenda_id": "agenda-1", "action_type": "move",
                    "summary": "intent-summary", "target_refs": [], "location_ref": "node-2",
                    "scheduled_in": {"amount": 2, "unit": "日"},
                    "requires_onstage": True, "impact_level": "minor",
                }],
            },
            {"ref": "opp-1", "actor_tier": "minor", "location_ref": "node-2", "travel_mode_ids": [], "resources": [], "agendas": [], "intents": []},
            {"ref": "faction-1", "actor_tier": "minor", "location_ref": "node-1", "travel_mode_ids": [], "resources": [], "agendas": [], "intents": []},
        ],
        "relations": [
            {"source": "cast-1", "target": "faction-1", "edge_type": "member_of", "note": "", "visibility": "public", "appellation": ""},
            {"source": "opp-1", "target": "cast-1", "edge_type": "rival_of", "note": "", "visibility": "public", "appellation": ""},
        ],
        "facts": [
            {"id": "fact-1", "proposition": "proposition-1", "truth_value": True, "sensitivity": "secret", "related_refs": ["cast-1"], "nobody_knows": False},
            {"id": "fact-2", "proposition": "proposition-2", "truth_value": True, "sensitivity": "public", "related_refs": [], "nobody_knows": False},
        ],
        "knowledge": [
            {"observer_ref": "cast-1", "fact_id": "fact-1", "state": "knows", "channel": "seeded"},
        ],
        "rules": [],
        "clocks": [],
        "reader_promises": [],
        "reveal_candidates": [],
    }
    return {
        "world": {
            "story_engine": story_engine,
            "map_atlas": {"nodes": [{"id": f"node-{number}", "name": f"node-{number}"} for number in range(1, 4)]},
        },
        "world_seed": seed,
        "book_arc_blueprint": {"arcs": [{"arc_number": 1}]},
    }


@dataclass(frozen=True)
class _World:
    project_id: str

    def node(self, kind: str, ref: str) -> str:
        return seed_ref_id(self.project_id, kind, ref)

    def stable(self, kind: str, name: str) -> str:
        return seed_ref_id(self.project_id, kind, name)

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
    def road(self) -> str:
        return f"road-{self.project_id}"

    @property
    def hidden(self) -> str:
        return f"hidden-{self.project_id}"


def _session_factory():
    engine = get_engine(postgres_test_url())
    init_db(engine)
    return engine, get_session_factory(engine)


def _seed_world(Session) -> _World:
    """Chapter 0 from the world seed; loc 1 -road- loc 2 -hidden route- loc 3."""
    pack = _pack()
    with Session.begin() as session:
        project = StateUpdater(session).create_project(
            title="chapter delta translator", premise="translator", genre="test",
            runtime_policy=RuntimePolicy.for_profile("standard"),
            creation_status="genesis_ready",
        )
        revision = BookGenesisRevision(project_id=project.id, pack_json=json.dumps(pack, ensure_ascii=False))
        session.add(revision)
        world = _World(project.id)
        repo = BookStateRepository(session)
        for number in (1, 2, 3):
            repo.create_map_node(MapNode(
                id=world.loc(number), project_id=project.id, subworld_id=SUBWORLD, region_id=REGION,
                node_type="site", name=f"site-{number}", metadata={"source_node_id": f"node-{number}"},
            ))
        repo.create_map_edge(MapEdge(
            id=world.road, project_id=project.id, subworld_id=SUBWORLD, from_node_id=world.loc(1),
            to_node_id=world.loc(2), edge_type="road", bidirectional=True, travel_time=2.0,
        ))
        repo.create_map_edge(MapEdge(
            id=world.hidden, project_id=project.id, subworld_id=SUBWORLD, from_node_id=world.loc(2),
            to_node_id=world.loc(3), edge_type="hidden_route", bidirectional=True, travel_time=1.0,
            discovered_by_default=False,
        ))
        session.flush()
        WorldSeedCommitter().commit(session=session, project=project, revision=revision, pack=pack)
    return world


def _extraction(chapter: int, fields: dict[str, Any]) -> ChapterDeltaExtraction:
    return ChapterDeltaExtraction.model_validate({
        "body_sha256": f"{chapter:x}" * 64,
        "time": {"elapsed": None, "evidence": ""},
        **fields,
    })


def _translate(
    Session, world: _World, chapter: int, fields: dict[str, Any],
    *, decisions: list[dict[str, Any]] | None = None,
) -> TranslationResult:
    plan = EntityAdmissionPlan(
        project_id=world.project_id,
        chapter_number=chapter,
        candidate_fingerprint="fingerprint",
        decisions=[EntityAdmissionDecision.model_validate(item) for item in decisions or []],
    )
    with Session() as session:
        return ChapterDeltaTranslator(session).translate(
            project_id=world.project_id,
            chapter_number=chapter,
            extraction=_extraction(chapter, fields),
            admission_plan=plan,
            chapter_summary=f"summary {chapter}",
            review_verdict_id="verdict-1",
        )


def _commit(Session, world: _World, chapter: int, result: TranslationResult) -> GraphDelta:
    assert result.delta is not None, result.issues
    with Session.begin() as session:
        compiled = BookStateCompiler(session).compile(
            ApprovedGraphDeltaSet(project_id=world.project_id, chapter_number=chapter, graph_deltas=[result.delta])
        )
        assert compiled.committed, compiled.blocked_reasons
    return result.delta


def _runtime(Session, world: _World, chapter: int):
    with Session() as session:
        return BookStateProjection(session).load_runtime_as_of(world.project_id, as_of_chapter=chapter)


def _issues(result: TranslationResult) -> list[tuple[str, str, str]]:
    return [(issue.severity, issue.code, issue.path) for issue in result.issues]


def _all_patches(delta: GraphDelta) -> list[Any]:
    return [
        *delta.node_patches, *delta.edge_patches, *delta.fact_patches,
        *delta.map_patches, *delta.cognition_patches, *delta.narrative_patches,
    ]


def test_story_time_continues_from_previous_chapter() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)

    first = _translate(Session, world, 1, {"time": {"elapsed": {"amount": 2, "unit": "日"}, "evidence": "e"}})
    with Session() as session:
        calendar = StoryCalendarRepository(session).get(world.project_id)

    delta = first.delta
    assert delta is not None and first.issues == ()
    assert delta.id == f"chapter-delta:{world.project_id}:1:{'1' * 16}"
    assert (delta.source_type, str(delta.delta_type), delta.operation, delta.target_type) == (
        "chapter_extraction", "world_state", "apply_chapter_extraction", "chapter",
    )
    assert (delta.story_time_start, delta.story_time_end) == (OPENING, OPENING + 2 * 1440)
    assert delta.story_time == format_story_time(calendar, OPENING + 2 * 1440)
    assert (delta.summary, delta.review_verdict_id) == ("summary 1", "verdict-1")
    assert delta.metadata["extraction_path"] == "chapter_extraction"
    assert delta.metadata["time_elapsed_minutes"] == 2 * 1440
    _commit(Session, world, 1, first)

    second = _translate(Session, world, 2, {})
    assert (second.delta.story_time_start, second.delta.story_time_end) == (OPENING + 2880, OPENING + 2880)
    assert second.delta.metadata["time_elapsed_minutes"] is None
    engine.dispose()


def test_movement_sets_location_and_records_movement_metadata() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)

    result = _translate(Session, world, 1, {
        "time": {"elapsed": {"amount": 4, "unit": "时"}, "evidence": "e"},
        "movements": [
            {
                "entity_ref": world.hero, "from_ref": world.loc(1), "to_ref": world.loc(2),
                "depart_offset": {"amount": 1, "unit": "时"}, "arrive_offset": {"amount": 12, "unit": "刻"},
                "travel_mode_id": "mode-walk", "evidence": "e1",
            },
            {
                "entity_ref": world.rival, "from_ref": world.loc(2), "to_ref": world.loc(1),
                "depart_offset": {"amount": 1, "unit": "unknown-unit"}, "evidence": "e2",
            },
        ],
    })

    delta = result.delta
    assert delta is not None
    assert _issues(result) == [("warning", "offset_unknown", "movements[1].depart_offset")]
    assert [
        (patch.node_id, str(patch.op), patch.field_path, patch.old_value, patch.new_value)
        for patch in delta.node_patches
    ] == [
        (world.hero, "set", "state.location_id", world.loc(1), world.loc(2)),
        (world.rival, "set", "state.location_id", world.loc(2), world.loc(1)),
    ]
    assert delta.metadata["movements"] == [
        {
            "entity_id": world.hero, "from_id": world.loc(1), "to_id": world.loc(2),
            "depart_minute": OPENING + 60, "arrive_minute": OPENING + 180,
            "travel_mode_id": "mode-walk", "evidence": "e1",
        },
        {
            "entity_id": world.rival, "from_id": world.loc(2), "to_id": world.loc(1),
            "depart_minute": None, "arrive_minute": None, "travel_mode_id": "", "evidence": "e2",
        },
    ]
    _commit(Session, world, 1, result)
    runtime = _runtime(Session, world, 1)
    assert runtime.world.get_state(world.hero)["location_id"] == world.loc(2)
    assert runtime.world.get_state(world.rival)["location_id"] == world.loc(1)
    engine.dispose()


def test_new_site_creates_map_node_edge_and_site_state() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)

    result = _translate(Session, world, 1, {
        "new_sites": [
            {"name": "watchtower", "near_ref": world.loc(2), "kind": "landmark", "evidence": "e1"},
            {"name": "ferry", "region_ref": REGION, "kind": "dock", "evidence": "e2"},
        ],
        "movements": [{"entity_ref": world.hero, "from_ref": world.loc(1), "to_ref": "new:watchtower", "evidence": "e3"}],
    })

    delta = result.delta
    assert delta is not None and result.issues == ()
    tower = world.stable("map_node", "watchtower")
    ferry = world.stable("map_node", "ferry")
    tower_node, tower_edge, ferry_node, ferry_edge = delta.map_patches
    assert (tower_node.target_type, tower_node.target_id, str(tower_node.op)) == ("map_node", tower, "create")
    assert {key: tower_node.new_value[key] for key in ("id", "subworld_id", "region_id", "node_type", "name")} == {
        "id": tower, "subworld_id": SUBWORLD, "region_id": REGION, "node_type": "landmark", "name": "watchtower",
    }
    assert (tower_edge.target_type, str(tower_edge.op)) == ("map_edge", "create")
    assert {key: tower_edge.new_value[key] for key in ("from_node_id", "to_node_id", "edge_type", "bidirectional", "travel_time")} == {
        "from_node_id": tower, "to_node_id": world.loc(2), "edge_type": "path", "bidirectional": True, "travel_time": 0.25,
    }
    assert (ferry_node.target_id, ferry_node.new_value["region_id"], ferry_node.new_value["subworld_id"]) == (
        ferry, REGION, SUBWORLD,
    )
    assert ferry_node.new_value["node_type"] == "site"
    # A region-only site is joined to the region anchor node (the region's first map node).
    assert (ferry_edge.new_value["from_node_id"], ferry_edge.new_value["to_node_id"]) == (ferry, world.loc(1))
    site_states = [patch for patch in delta.node_patches if str(patch.node_type) == "site_state"]
    assert [(str(patch.op), patch.new_value["profile"]["map_node_id"]) for patch in site_states] == [
        ("create", tower), ("create", ferry),
    ]
    (move,) = [patch for patch in delta.node_patches if patch.field_path == "state.location_id"]
    assert (move.node_id, move.new_value) == (world.hero, tower)
    assert delta.metadata["movements"][0]["to_id"] == tower

    _commit(Session, world, 1, result)
    runtime = _runtime(Session, world, 1)
    assert set(runtime.map.nodes_by_id) >= {tower, ferry}
    assert runtime.map.shortest_path(world.loc(2), tower).reachable
    assert runtime.map.shortest_path(world.loc(1), ferry).reachable
    assert resolve_world_node_location_id(runtime.world, site_states[0].node_id) == tower
    assert runtime.world.get_state(world.hero)["location_id"] == tower

    unanchored = _translate(Session, world, 2, {"new_sites": [{"name": "nowhere", "evidence": "e"}]})
    assert unanchored.delta is None
    assert _issues(unanchored) == [("error", "new_site_unanchored", "new_sites[0]")]
    engine.dispose()


def test_relation_create_end_and_reactivate() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    relation = {"source_ref": world.hero, "target_ref": world.rival, "edge_type": "ally_of", "evidence": "e"}

    created = _commit(Session, world, 1, _translate(Session, world, 1, {
        "relation_changes": [{**relation, "op": "create", "appellation": "brother"}],
    }))
    (create,) = created.edge_patches
    assert (str(create.op), create.edge_family, create.new_value["metadata"]["appellation"]) == (
        "create", "social", "brother",
    )
    ended = _commit(Session, world, 2, _translate(Session, world, 2, {"relation_changes": [{**relation, "op": "end"}]}))
    (end,) = ended.edge_patches
    assert (end.edge_id, str(end.op), end.field_path, end.old_value) == (create.edge_id, "deactivate", "is_active", True)
    again = _commit(Session, world, 3, _translate(Session, world, 3, {"relation_changes": [{**relation, "op": "create"}]}))
    (reactivate,) = again.edge_patches
    assert (reactivate.edge_id, str(reactivate.op)) == (create.edge_id, "create")

    activity = {}
    for chapter in (1, 2, 3):
        edge = _runtime(Session, world, chapter).world.edges_by_id[create.edge_id]
        activity[chapter] = (edge.is_active, edge.ended_at_chapter)
    assert activity == {1: (True, None), 2: (False, 2), 3: (True, None)}
    engine.dispose()


def test_knowledge_events_compile_with_tags_and_reader_observer() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)

    result = _translate(Session, world, 1, {"knowledge_events": [
        {
            "observer_ref": "reader", "fact_ref": world.fact("fact-2"), "state": "knows", "channel": "witnessed",
            "touches_secret_ref": world.fact("fact-1"), "evidence": "e1",
        },
        {
            "observer_ref": world.rival,
            "new_fact": {"proposition": "the hero hides a key", "truth_value": False, "sensitivity": "hidden"},
            "state": "believes", "channel": "told", "source_ref": world.hero,
            "contradicts_ref": world.fact("fact-1"), "evidence": "e2",
        },
    ]})

    delta = result.delta
    assert delta is not None and result.issues == ()
    new_fact = world.stable("fact", "the hero hides a key")
    (fact_patch,) = delta.fact_patches
    assert (fact_patch.fact_id, str(fact_patch.op), fact_patch.truth_value, fact_patch.sensitivity_level) == (
        new_fact, "create", "false", "hidden",
    )
    reader, rival = delta.cognition_patches
    assert (
        reader.observer_type, reader.observer_id, str(reader.op), reader.field_path,
        reader.new_value, reader.old_value, reader.evidence_refs,
    ) == (
        "reader", "reader", "merge", "knowledge_by_ref",
        {world.fact("fact-2"): "knows"}, {world.fact("fact-2"): None}, [f"knowledge:witnessed:-:{OPENING}"],
    )
    assert (rival.observer_type, rival.observer_id, rival.new_value, rival.evidence_refs) == (
        "character", world.rival, {f"fact:{new_fact}": "believes"}, [f"knowledge:told:{world.hero}:{OPENING}"],
    )
    assert delta.metadata["knowledge_tags"] == [
        {
            "observer_type": "reader", "observer_id": "reader", "fact_ref": world.fact("fact-2"),
            "state": "knows", "channel": "witnessed", "source_id": "",
            "touches_secret_ref": world.fact("fact-1"), "contradicts_ref": "", "flashback": False, "evidence": "e1",
        },
        {
            "observer_type": "character", "observer_id": world.rival, "fact_ref": f"fact:{new_fact}",
            "state": "believes", "channel": "told", "source_id": world.hero,
            "touches_secret_ref": "", "contradicts_ref": world.fact("fact-1"), "flashback": False, "evidence": "e2",
        },
    ]

    _commit(Session, world, 1, result)
    runtime = _runtime(Session, world, 1)
    assert runtime.cognition_by_observer[("reader", "reader")].knowledge_by_ref == {world.fact("fact-2"): "knows"}
    assert runtime.cognition_by_observer[("character", world.rival)].knowledge_by_ref == {f"fact:{new_fact}": "believes"}
    assert runtime.world.facts_by_id[new_fact].truth_value == "false"
    engine.dispose()


def test_flashback_entity_change_emits_no_state_patch() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)

    result = _translate(Session, world, 1, {
        "entity_changes": [
            {"ref": world.rival, "field": "alive_status", "new_value": "dead", "evidence": "e1", "flashback": True},
            {"ref": world.hero, "field": "health", "new_value": "wounded", "evidence": "e2"},
        ],
        "knowledge_events": [{
            "observer_ref": world.rival, "fact_ref": world.fact("fact-2"), "state": "knows",
            "channel": "witnessed", "evidence": "e3", "flashback": True,
        }],
        "events": [{
            "summary": "old battle", "participant_refs": [world.rival], "significance": "minor",
            "visibility": "public", "evidence": "e4", "flashback": True,
        }],
    })

    delta = result.delta
    assert delta is not None and result.issues == ()
    assert [(patch.node_id, patch.field_path) for patch in delta.node_patches] == [(world.hero, "state.health")]
    (fact,) = delta.fact_patches
    assert fact.new_value["fact_type"] == "event"
    assert delta.metadata["flashback_refs"] == [
        {"path": "entity_changes[0]", "refs": [world.rival]},
        {"path": "knowledge_events[0]", "refs": [world.rival, world.fact("fact-2")]},
        {"path": "events[0]", "refs": [f"fact:{fact.fact_id}", world.rival]},
    ]
    assert delta.metadata["knowledge_tags"][0]["flashback"] is True
    _commit(Session, world, 1, result)
    runtime = _runtime(Session, world, 1)
    assert runtime.world.get_state(world.rival)["alive_status"] == "alive"
    assert runtime.world.get_state(world.hero)["health"] == "wounded"
    engine.dispose()


def test_item_transfer_updates_holder_with_old_value() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    item = world.stable("item", "bronze key")

    found = _commit(Session, world, 1, _translate(Session, world, 1, {"item_transfers": [{
        "new_item": {"name": "bronze key", "kind": "item"}, "from_ref": world.loc(1), "to_ref": world.hero, "evidence": "e1",
    }]}))
    (create,) = found.node_patches
    assert (create.node_id, str(create.node_type), str(create.op)) == (item, "item", "create")
    assert create.new_value["state"]["holder_id"] == world.hero

    result = _translate(Session, world, 2, {"item_transfers": [{
        "item_ref": item, "from_ref": world.hero, "to_ref": world.rival, "evidence": "e2",
    }]})
    (transfer,) = result.delta.node_patches
    assert (transfer.node_id, str(transfer.op), transfer.field_path, transfer.old_value, transfer.new_value) == (
        item, "set", "state.holder_id", world.hero, world.rival,
    )
    assert result.delta.metadata["item_transfers"] == [
        {"item_id": item, "from_id": world.hero, "to_id": world.rival, "evidence": "e2"},
    ]
    _commit(Session, world, 2, result)
    assert _runtime(Session, world, 2).world.get_state(item)["holder_id"] == world.rival
    engine.dispose()


def test_new_character_ref_resolves_through_admission_plan() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)

    result = _translate(
        Session, world, 1,
        {
            "events": [{
                "summary": "the porter arrives", "participant_refs": ["new:porter", "new:little-hero", "new:passer-by"],
                "location_ref": world.loc(1), "significance": "major", "visibility": "secret", "evidence": "e1",
            }],
            "movements": [{"entity_ref": "new:porter", "from_ref": world.loc(1), "to_ref": world.loc(2), "evidence": "e2"}],
        },
        decisions=[
            {"mention_name": "porter", "action": "register_character", "entity_id": "char-porter",
             "canonical_name": "porter", "role_hint": "carrier"},
            {"mention_name": "little-hero", "action": "register_alias", "entity_id": world.hero,
             "canonical_name": "甲", "aliases": ["little-hero"]},
            {"mention_name": "passer-by", "action": "background_generic"},
        ],
    )

    delta = result.delta
    assert delta is not None and result.issues == ()
    porter, alias, event = delta.node_patches
    assert (porter.node_id, str(porter.node_type), str(porter.op)) == ("char-porter", "character", "create")
    assert porter.new_value["name"] == "porter"
    assert porter.new_value["state"]["location_id"] == world.loc(2)
    assert porter.new_value["state"]["alive_status"] == "alive"
    assert (alias.node_id, str(alias.op), alias.field_path, alias.old_value, alias.new_value) == (
        world.hero, "append", "aliases", [], "little-hero",
    )
    assert str(event.node_type) == "event"
    assert event.new_value["state"]["participant_ids"] == ["char-porter", world.hero]
    assert event.new_value["profile"]["location_id"] == world.loc(1)
    (fact,) = delta.fact_patches
    assert (fact.sensitivity_level, fact.new_value["fact_type"]) == ("secret", "event")
    assert delta.metadata["movements"][0]["entity_id"] == "char-porter"

    _commit(Session, world, 1, result)
    runtime = _runtime(Session, world, 1)
    assert runtime.world.get_node("char-porter").name == "porter"
    assert runtime.world.get_state("char-porter")["location_id"] == world.loc(2)
    assert "little-hero" in runtime.world.get_node(world.hero).aliases
    engine.dispose()


def test_unresolved_new_ref_blocks_translation() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)

    result = _translate(
        Session, world, 1,
        {
            "entity_changes": [{"ref": "char-missing", "field": "health", "new_value": "ok", "evidence": "e1"}],
            "movements": [{"entity_ref": "new:nobody", "from_ref": world.loc(1), "to_ref": world.loc(2), "evidence": "e2"}],
            "events": [{
                "summary": "s", "participant_refs": ["new:rejected"], "significance": "minor",
                "visibility": "public", "evidence": "e3",
            }],
        },
        decisions=[{"mention_name": "rejected", "action": "plan_conflict", "reason": "canon conflict"}],
    )

    assert result.delta is None
    assert _issues(result) == [
        ("error", "unresolved_ref", "entity_changes[0].ref"),
        ("error", "unresolved_ref", "movements[0].entity_ref"),
        ("error", "unresolved_ref", "events[0].participant_refs[0]"),
    ]
    engine.dispose()


def test_unknown_field_and_missing_old_value_block_translation() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)

    result = _translate(Session, world, 1, {
        "entity_changes": [
            {"ref": world.hero, "field": "not_a_field", "new_value": "x", "evidence": "e1"},
            # Only a missing parent dict blocks: a missing leaf merges into its parent.
            {"ref": world.hero, "field": "talent_profile.rank", "new_value": "a", "evidence": "e2"},
        ],
        "relation_changes": [
            {
                "source_ref": world.rival, "target_ref": world.hero, "edge_type": "rival_of",
                "op": "set", "field": "metadata.notes.tone", "new_value": "cold", "evidence": "e3",
            },
            # The schema rejects unknown edge types; an unvalidated item still cannot pass.
            RelationChange.model_construct(
                source_ref=world.rival, target_ref=world.hero, edge_type="not_an_edge_type",
                op="create", field="", new_value=None, appellation="", evidence="e4",
            ),
        ],
    })

    assert result.delta is None
    assert _issues(result) == [
        ("error", "unknown_field", "entity_changes[0].field"),
        ("error", "missing_old_value", "entity_changes[1].field"),
        ("error", "missing_old_value", "relation_changes[0].field"),
        ("error", "edge_type_invalid", "relation_changes[1].edge_type"),
    ]
    engine.dispose()


def test_events_threads_and_intents_translate() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    intent = world.node("intent", "cast-1/intent-1")
    thread = world.stable("plot_thread", "the missing key")

    result = _translate(Session, world, 1, {
        "events": [{
            "summary": "a duel at dawn", "participant_refs": [world.hero, world.rival], "location_ref": world.loc(2),
            "significance": "moderate", "visibility": "public", "evidence": "e1",
        }],
        "thread_beats": [
            {"new_thread_title": "the missing key", "beat_type": "setup", "evidence": "e2"},
            {"new_thread_title": "the missing key", "beat_type": "advance", "evidence": "e3"},
        ],
        "intent_interactions": [
            {"intent_ref": intent, "effect": "advanced", "evidence": "e4"},
            {"intent_ref": intent, "effect": "fulfilled", "evidence": "e5"},
        ],
        "plan_fulfillment": [{"target_id": "goal:1", "status": "missing"}],
    })

    delta = result.delta
    assert delta is not None and result.issues == ()
    event, intent_patch = delta.node_patches
    assert (str(event.node_type), event.new_value["state"]["status"], event.new_value["state"]["result_refs"]) == (
        "event", "occurred", [f"fact:{delta.fact_patches[0].fact_id}"],
    )
    assert (delta.fact_patches[0].sensitivity_level, delta.fact_patches[0].proposition) == ("public", "a duel at dawn")
    assert (intent_patch.node_id, intent_patch.field_path, intent_patch.old_value, intent_patch.new_value) == (
        intent, "state.status", "planned", "resolved",
    )
    (create,) = delta.narrative_patches
    assert (create.target_ref, str(create.op), create.new_value["status"]) == (f"plot_thread:{thread}", "create", "active")
    assert [(beat["beat_type"], beat["description"]) for beat in create.new_value["payload"]["beats"]] == [
        ("setup", "e2"), ("advance", "e3"),
    ]
    assert delta.metadata["intent_interactions"] == [
        {"intent_id": intent, "effect": "advanced", "evidence": "e4"},
        {"intent_id": intent, "effect": "fulfilled", "evidence": "e5"},
    ]
    assert delta.metadata["plan_fulfillment"] == [{"target_id": "goal:1", "status": "missing", "evidence": ""}]
    _commit(Session, world, 1, result)

    resolved = _translate(Session, world, 2, {"thread_beats": [
        {"thread_ref": thread, "beat_type": "resolution", "evidence": "e6"},
    ]})
    append, status = resolved.delta.narrative_patches
    assert (str(append.op), append.field_path, len(append.old_value)) == ("append", "payload.beats", 2)
    assert (str(status.op), status.field_path, status.old_value, status.new_value) == ("set", "status", "active", "resolved")
    _commit(Session, world, 2, resolved)
    runtime = _runtime(Session, world, 2)
    assert runtime.narrative.get_node(thread).status == "resolved"
    assert runtime.world.get_state(intent)["status"] == "resolved"
    engine.dispose()


def test_every_non_create_patch_has_field_path_and_old_value() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    item = world.stable("item", "bronze key")
    thread = world.stable("plot_thread", "the missing key")
    _commit(Session, world, 1, _translate(Session, world, 1, {
        "item_transfers": [{"new_item": {"name": "bronze key", "kind": "item"}, "from_ref": "", "to_ref": world.hero, "evidence": "e"}],
        "relation_changes": [{"source_ref": world.hero, "target_ref": world.rival, "edge_type": "ally_of", "op": "create", "evidence": "e"}],
        "thread_beats": [{"new_thread_title": "the missing key", "beat_type": "setup", "evidence": "e"}],
    }))

    result = _translate(
        Session, world, 2,
        {
            "entity_changes": [
                {"ref": world.hero, "field": "goal", "new_value": "a new goal", "evidence": "e"},
                {"ref": world.hero, "field": "narrative_role", "new_value": "a new role", "evidence": "e"},
            ],
            "relation_changes": [
                {"source_ref": world.hero, "target_ref": world.rival, "edge_type": "ally_of", "op": "end", "evidence": "e"},
                {"source_ref": world.rival, "target_ref": world.hero, "edge_type": "rival_of", "op": "set",
                 "field": "weight", "new_value": 0.5, "evidence": "e"},
            ],
            "knowledge_events": [{"observer_ref": world.hero, "fact_ref": world.fact("fact-2"), "state": "knows",
                                  "channel": "witnessed", "evidence": "e"}],
            "movements": [{"entity_ref": world.hero, "from_ref": world.loc(1), "to_ref": world.loc(3),
                           "discovers_route": True, "evidence": "e"}],
            "item_transfers": [{"item_ref": item, "from_ref": world.hero, "to_ref": world.rival, "evidence": "e"}],
            "thread_beats": [{"thread_ref": thread, "beat_type": "resolution", "evidence": "e"}],
            "intent_interactions": [{"intent_ref": world.node("intent", "cast-1/intent-1"), "effect": "disrupted", "evidence": "e"}],
        },
        decisions=[{"mention_name": "little-hero", "action": "register_alias", "entity_id": world.hero,
                    "canonical_name": "甲", "aliases": ["little-hero"]}],
    )

    delta = result.delta
    assert delta is not None and result.issues == ()
    non_create = [patch for patch in _all_patches(delta) if str(patch.op) != "create"]
    assert {type(patch) for patch in non_create} == {NodePatch, EdgePatch, CognitionPatch, NarrativePatch}
    assert len(non_create) == len(_all_patches(delta))
    for patch in non_create:
        assert patch.field_path, patch
        assert patch.old_value is not None, patch
    _commit(Session, world, 2, result)
    engine.dispose()


def test_route_discovery_appends_visible_refs() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)

    result = _translate(Session, world, 1, {"movements": [
        {"entity_ref": world.hero, "from_ref": world.loc(1), "to_ref": world.loc(3), "discovers_route": True, "evidence": "e1"},
        # The rival has no cognition overlay, so every route is already open to it.
        {"entity_ref": world.rival, "from_ref": world.loc(2), "to_ref": world.loc(3), "discovers_route": True, "evidence": "e2"},
    ]})

    delta = result.delta
    assert delta is not None and result.issues == ()
    assert [
        (patch.observer_type, patch.observer_id, str(patch.op), patch.field_path, patch.new_value, patch.old_value)
        for patch in delta.cognition_patches
    ] == [
        ("character", world.hero, "append", "visible_refs", f"map_edge:{world.hidden}", []),
        ("character", world.hero, "append", "visible_refs", f"map_edge:{world.hidden}__reverse", []),
    ]
    _commit(Session, world, 1, result)
    runtime = _runtime(Session, world, 1)
    assert runtime.map.shortest_path(world.loc(1), world.loc(3), observer=("character", world.hero)).reachable

    known = _translate(Session, world, 2, {"movements": [
        {"entity_ref": world.hero, "from_ref": world.loc(3), "to_ref": world.loc(1), "discovers_route": True, "evidence": "e3"},
    ]})
    assert known.delta.cognition_patches == []
    engine.dispose()


def test_route_unknown_to_mover_is_not_discovered_without_the_flag() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)

    result = _translate(Session, world, 1, {"movements": [
        {"entity_ref": world.hero, "from_ref": world.loc(1), "to_ref": world.loc(3), "evidence": "e1"},
    ]})

    # The review gate, not the translator, decides whether the move was possible.
    assert result.delta is not None and result.issues == ()
    assert result.delta.cognition_patches == []
    assert result.delta.metadata["movements"][0]["to_id"] == world.loc(3)
    engine.dispose()


def test_first_time_field_sets_merge_into_their_parent_and_rewind() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    rival_edge = world.node("edge", "opp-1/rival_of/cast-1")
    profile_before = _runtime(Session, world, 0).world.get_node(world.hero).profile

    result = _translate(Session, world, 1, {
        "entity_changes": [{"ref": world.hero, "field": "public_identity", "new_value": "a clerk", "evidence": "e1"}],
        "relation_changes": [{
            "source_ref": world.rival, "target_ref": world.hero, "edge_type": "rival_of",
            "op": "set", "field": "state.intensity", "new_value": 3, "evidence": "e2",
        }],
    })

    delta = result.delta
    assert delta is not None and result.issues == ()
    (profile,) = delta.node_patches
    assert (str(profile.op), profile.field_path, profile.new_value, profile.old_value) == (
        "merge", "profile", {"public_identity": "a clerk"}, profile_before,
    )
    (edge,) = delta.edge_patches
    assert (edge.edge_id, str(edge.op), edge.field_path, edge.new_value, edge.old_value) == (
        rival_edge, "merge", "state", {"intensity": 3}, {},
    )
    _commit(Session, world, 1, result)
    after = _runtime(Session, world, 1)
    assert after.world.get_node(world.hero).profile == {**profile_before, "public_identity": "a clerk"}
    assert after.world.edges_by_id[rival_edge].state == {"intensity": 3}
    assert "public_identity" not in _runtime(Session, world, 0).world.get_node(world.hero).profile

    with Session.begin() as session:
        repo = BookStateRepository(session)
        repo.invalidate_project_range(world.project_id, from_chapter=1, through_chapter=1)
        assert repo.get_world_node(world.hero).profile == profile_before
        assert repo.get_world_edge(rival_edge).state == {}
    engine.dispose()


def test_symmetric_relations_match_either_direction() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    seeded = world.node("edge", "opp-1/rival_of/cast-1")
    reversed_rival = {"source_ref": world.hero, "target_ref": world.rival, "edge_type": "rival_of", "evidence": "e"}

    first = _translate(Session, world, 1, {"relation_changes": [
        {**reversed_rival, "op": "create"},
        {**reversed_rival, "op": "set", "field": "weight", "new_value": 0.5},
        {"source_ref": world.hero, "target_ref": world.rival, "edge_type": "ally_of", "op": "create", "evidence": "e"},
    ]})

    assert _issues(first) == [("warning", "relation_exists", "relation_changes[0]")]
    weight, ally = first.delta.edge_patches
    assert (weight.edge_id, str(weight.op), weight.field_path, weight.old_value) == (seeded, "set", "weight", 1.0)
    assert (weight.source_id, weight.target_id) == (world.rival, world.hero)
    assert ally.new_value["directionality"] == "undirected"
    _commit(Session, world, 1, first)

    second = _translate(Session, world, 2, {"relation_changes": [
        {**reversed_rival, "op": "end"},
        {"source_ref": world.rival, "target_ref": world.hero, "edge_type": "ally_of", "op": "end", "evidence": "e"},
    ]})
    assert [(patch.edge_id, str(patch.op)) for patch in second.delta.edge_patches] == [
        (seeded, "deactivate"), (ally.edge_id, "deactivate"),
    ]
    _commit(Session, world, 2, second)
    edges = _runtime(Session, world, 2).world.edges_by_id
    assert (edges[seeded].is_active, edges[ally.edge_id].is_active) == (False, False)
    engine.dispose()


def test_directed_relation_with_only_the_reverse_edge_is_a_direction_mismatch() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    created = _commit(Session, world, 1, _translate(Session, world, 1, {"relation_changes": [{
        "source_ref": world.hero, "target_ref": world.rival, "edge_type": "mentor_of", "op": "create", "evidence": "e",
    }]}))
    assert created.edge_patches[0].new_value["directionality"] == "directed"

    reverse = {"source_ref": world.rival, "target_ref": world.hero, "edge_type": "mentor_of", "evidence": "e"}
    result = _translate(Session, world, 2, {"relation_changes": [
        {**reverse, "op": "create"},
        {**reverse, "op": "end"},
        {**reverse, "op": "set", "field": "weight", "new_value": 0.5},
    ]})

    assert result.delta is not None and result.delta.edge_patches == []
    assert _issues(result) == [
        ("warning", "relation_direction_mismatch", f"relation_changes[{index}]") for index in range(3)
    ]
    engine.dispose()


def test_location_and_holder_entity_changes_become_movement_and_transfer() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    item = world.stable("item", "bronze key")
    _commit(Session, world, 1, _translate(Session, world, 1, {"item_transfers": [{
        "new_item": {"name": "bronze key", "kind": "item"}, "from_ref": "", "to_ref": world.hero, "evidence": "e",
    }]}))

    result = _translate(Session, world, 2, {"entity_changes": [
        {"ref": world.hero, "field": "location_id", "new_value": world.loc(2), "evidence": "e1"},
        {"ref": item, "field": "holder_id", "new_value": world.rival, "evidence": "e2"},
    ]})

    delta = result.delta
    assert delta is not None and result.issues == ()
    assert [(patch.node_id, patch.field_path, patch.old_value, patch.new_value) for patch in delta.node_patches] == [
        (world.hero, "state.location_id", world.loc(1), world.loc(2)),
        (item, "state.holder_id", world.hero, world.rival),
    ]
    assert delta.metadata["movements"] == [{
        "entity_id": world.hero, "from_id": world.loc(1), "to_id": world.loc(2),
        "depart_minute": None, "arrive_minute": None, "travel_mode_id": "", "evidence": "e1",
    }]
    assert delta.metadata["item_transfers"] == [
        {"item_id": item, "from_id": world.hero, "to_id": world.rival, "evidence": "e2"},
    ]
    _commit(Session, world, 2, result)

    dropped = _translate(
        Session, world, 3,
        {"item_transfers": [{"item_ref": item, "from_ref": world.rival, "to_ref": "new:passer-by", "evidence": "e3"}]},
        decisions=[{"mention_name": "passer-by", "action": "background_generic"}],
    )
    assert _issues(dropped) == [("warning", "item_transfer_dropped", "item_transfers[0]")]
    assert (dropped.delta.node_patches, dropped.delta.metadata["item_transfers"]) == ([], [])
    engine.dispose()


def test_relation_endpoint_map_node_resolves_to_its_site_state() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    tower = world.stable("map_node", "watchtower")
    faction = world.node("faction", "faction-1")
    _commit(Session, world, 1, _translate(Session, world, 1, {"new_sites": [
        {"name": "watchtower", "near_ref": world.loc(2), "evidence": "e"},
    ]}))
    controls = {"source_ref": faction, "edge_type": "controls", "op": "create", "evidence": "e"}

    result = _translate(Session, world, 2, {"relation_changes": [{**controls, "target_ref": tower}]})

    (edge,) = result.delta.edge_patches
    assert (edge.source_id, edge.target_id, edge.edge_family) == (
        faction, world.stable("site_state", tower), "control_conflict",
    )
    _commit(Session, world, 2, result)
    missing = _translate(Session, world, 3, {"relation_changes": [{**controls, "target_ref": world.loc(3)}]})
    assert missing.delta is None
    assert _issues(missing) == [("error", "unresolved_ref", "relation_changes[0].target_ref")]
    engine.dispose()


def test_new_entity_ref_reuses_existing_node_with_matching_name() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    faction = world.node("faction", "faction-1")
    item = world.stable("item", "Bronze Key")
    _commit(Session, world, 1, _translate(Session, world, 1, {"item_transfers": [{
        "new_item": {"name": "Bronze Key", "kind": "item"}, "from_ref": "", "to_ref": world.hero, "evidence": "e",
    }]}))

    result = _translate(Session, world, 2, {
        "entity_changes": [{
            "new_entity": {"name": " 丙 ", "kind": "faction"}, "field": "goal", "new_value": "a new goal", "evidence": "e1",
        }],
        "item_transfers": [{
            "new_item": {"name": "bronze  key", "kind": "item"}, "from_ref": world.hero, "to_ref": world.rival, "evidence": "e2",
        }],
    })

    delta = result.delta
    assert delta is not None and result.issues == ()
    assert [(str(patch.op), patch.node_id, patch.field_path) for patch in delta.node_patches] == [
        ("set", faction, "state.goal"), ("set", item, "state.holder_id"),
    ]
    engine.dispose()


def test_same_chapter_reversed_symmetric_creates_make_one_edge() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)

    result = _translate(Session, world, 1, {"relation_changes": [
        {"source_ref": world.hero, "target_ref": world.rival, "edge_type": "ally_of", "op": "create", "evidence": "e1"},
        {"source_ref": world.rival, "target_ref": world.hero, "edge_type": "ally_of", "op": "create", "evidence": "e2"},
    ]})

    (create,) = result.delta.edge_patches
    low, high = sorted((world.hero, world.rival))
    assert create.edge_id == world.stable("edge", f"{low}/ally_of/{high}")
    assert _issues(result) == [("warning", "relation_exists", "relation_changes[1]")]
    engine.dispose()


def test_directed_create_after_the_reverse_edge_ended_is_allowed() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    mentor = {"source_ref": world.hero, "target_ref": world.rival, "edge_type": "mentor_of", "evidence": "e"}
    _commit(Session, world, 1, _translate(Session, world, 1, {"relation_changes": [{**mentor, "op": "create"}]}))
    _commit(Session, world, 2, _translate(Session, world, 2, {"relation_changes": [{**mentor, "op": "end"}]}))

    result = _translate(Session, world, 3, {"relation_changes": [{
        "source_ref": world.rival, "target_ref": world.hero, "edge_type": "mentor_of", "op": "create", "evidence": "e",
    }]})

    assert result.issues == ()
    (create,) = result.delta.edge_patches
    assert (str(create.op), create.source_id, create.target_id) == ("create", world.rival, world.hero)
    _commit(Session, world, 3, result)
    engine.dispose()


def test_location_entity_change_is_skipped_when_the_entity_has_a_movement() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)

    result = _translate(Session, world, 1, {
        "entity_changes": [{"ref": world.hero, "field": "location_id", "new_value": world.loc(2), "evidence": "e1"}],
        "movements": [{"entity_ref": world.hero, "from_ref": "", "to_ref": world.loc(2), "evidence": "e2"}],
    })

    delta = result.delta
    assert delta is not None and result.issues == ()
    assert [(patch.field_path, patch.new_value) for patch in delta.node_patches] == [("state.location_id", world.loc(2))]
    assert [(move["from_id"], move["to_id"], move["evidence"]) for move in delta.metadata["movements"]] == [
        (world.loc(1), world.loc(2), "e2"),
    ]
    engine.dispose()


def test_reused_runtime_must_match_project_and_as_of_chapter():
    import pytest
    from forwin.book_state.extraction.chapter_input import build_chapter_extraction_input
    from forwin.book_state.reviewer import BookStateReviewGate
    from forwin.book_state.runtime import RuntimeMismatch
    from forwin.book_state.lexicon import BookLexicon
    from forwin.world_engine.running_state import RunningStateCheck
    from forwin.world_engine.delta_builder import WorldTickDeltaBuilder
    from forwin.world_engine.types import TickContext
    from tests.test_world_actor_turns import _runtime

    runtime = _runtime()
    for project_id, chapter in (("p", 4), ("other", 3)):
        with pytest.raises(RuntimeMismatch):
            WorldTickDeltaBuilder(None).build(
                context=TickContext(project_id=project_id, chapter_number=chapter, now=10, window_start=0, seed=1),
                candidates=[], news_in_transit=[], tick_id="tick", runtime=runtime,
            )
        with pytest.raises(RuntimeMismatch):
            BookStateReviewGate(None).review(
                ApprovedGraphDeltaSet(project_id=project_id, chapter_number=chapter, graph_deltas=[]),
                base_chapter=chapter, runtime=runtime,
            )
        with pytest.raises(RuntimeMismatch):
            ChapterDeltaTranslator(None).translate(
                project_id=project_id, chapter_number=chapter + 1,
                extraction=_extraction(chapter + 1, {}), chapter_summary="summary",
                admission_plan=EntityAdmissionPlan(project_id=project_id, chapter_number=chapter + 1,
                                                   candidate_fingerprint="f"), runtime=runtime,
            )
        with pytest.raises(RuntimeMismatch):
            build_chapter_extraction_input(None, project_id=project_id, chapter_number=chapter + 1, runtime=runtime)

        with pytest.raises(RuntimeMismatch):
            RunningStateCheck(
                None, context=TickContext(project_id=project_id, chapter_number=chapter, now=10, window_start=0, seed=1),
                runtime=runtime, lexicon=BookLexicon(), calendar=None, tick_id="tick", travel_tolerance=0.2,
            )


def test_runtime_mismatch_names_expected_and_actual_snapshot():
    import pytest
    from forwin.book_state.runtime import RuntimeMismatch, require_runtime_as_of
    from tests.test_world_actor_turns import _runtime

    with pytest.raises(RuntimeMismatch) as caught:
        require_runtime_as_of(_runtime(), "expected-project", 7)
    assert str(caught.value) == (
        "Runtime mismatch: expected project 'expected-project', chapter 7; actual project 'p', chapter 3"
    )
