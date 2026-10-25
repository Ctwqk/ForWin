from __future__ import annotations

import json
from copy import deepcopy

import pytest
from sqlalchemy import func, select, text

from forwin.audit.events import DecisionEventInfo, DecisionEventType
from forwin.book_state import BookStateProjection, BookStateRepository
from forwin.book_state.lexicon import lexicon_from_payload
from forwin.book_state.query import BookStateQuery
from forwin.book_state.story_clock import calendar_from_payload
from forwin.canon import CanonAdmissionService, CanonStaleVersion
from forwin.genesis.handoff.commands import HandoffStageError
from forwin.genesis.handoff.world_seed_committer import (
    WorldSeedCommitError,
    WorldSeedCommitter,
    build_world_seed_deltas,
    map_node_ids_by_genesis_ref,
    seed_ref_id,
)
from forwin.map.models import MapNodeRow
from forwin.models import DecisionEvent
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.book_frame import BookLexiconRow, StoryCalendarRow
from forwin.models.book_state import (
    CharacterIdentityMapRow,
    GraphDeltaRow,
    WorldNodeRow,
    WorldNodeStateRow,
)
from forwin.models.genesis import BookGenesisRevision
from forwin.models.outbox import OutboxEvent
from forwin.models.project import Project
from forwin.protocol.book_state import ApprovedGraphDeltaSet, GraphDelta
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url


OPENING = 3 * 1440 + 2 * 60
MAP_NODE_IDS = {"node-1": "loc-db-1", "node-2": "loc-db-2"}


@pytest.fixture
def story_engine():
    return {
        "core_cast": [{
            "id": "cast-1", "name": "甲", "role": "role-cast", "desire": "desire-cast",
            "fear": "fear-cast", "culture_profile_id": "culture-1",
            "affiliated_faction": "faction-1", "current_base": "node-1",
        }],
        "opposition": [{
            "id": "opp-1", "name": "乙", "role": "role-opp", "desire": "desire-opp",
            "culture_profile_id": "", "backing_faction": "", "base_location": "node-2",
        }],
        "factions": [{
            "id": "faction-1", "name": "丙", "role": "role-faction", "goal": "goal-faction",
            "culture_profile_id": "culture-2", "base_location": "node-1",
        }],
    }


@pytest.fixture
def map_atlas():
    return {"nodes": [{"id": "node-1", "name": "node-1"}, {"id": "node-2", "name": "node-2"}]}


@pytest.fixture
def seed():
    return {
        "calendar": {
            "epoch_label": "",
            "opening_minute": OPENING,
            "units": [{"name": "日", "minutes": 1440}, {"name": "时", "minutes": 60}],
        },
        "lexicon": {
            "time_unit_aliases": [], "rank_ladders": [], "currencies": [],
            "appellations": [], "generic_character_refs": [],
            "travel_modes": [{"id": "mode-1", "name": "", "speed_multiplier": 1.0, "allowed_edge_types": ["road"]}],
            "news_speed_multiplier": 1.0,
        },
        "actors": [
            {
                "ref": "cast-1", "actor_tier": "major", "location_ref": "node-1",
                "travel_mode_ids": ["mode-1"], "resources": ["resource-1"],
                "agendas": [{
                    "id": "agenda-1", "goal": "agenda-goal", "motivation": "agenda-motivation",
                    "priority": 3, "horizon": "agenda-horizon", "means_refs": ["faction-1", "fact-1"],
                    "constraints": ["constraint-1"], "secrecy_refs": ["opp-1"],
                }],
                "intents": [{
                    "id": "intent-1", "agenda_id": "agenda-1", "action_type": "move",
                    "summary": "intent-summary", "target_refs": ["opp-1", "fact-1"], "location_ref": "node-2",
                    "scheduled_in": {"amount": 2, "unit": "日"},
                    "requires_onstage": False, "impact_level": "minor",
                }],
            },
            {"ref": "opp-1", "actor_tier": "minor", "location_ref": "node-2", "travel_mode_ids": [], "resources": [], "agendas": [], "intents": []},
            {"ref": "faction-1", "actor_tier": "minor", "location_ref": "node-1", "travel_mode_ids": [], "resources": ["resource-2"], "agendas": [], "intents": []},
        ],
        "relations": [
            {"source": "cast-1", "target": "faction-1", "edge_type": "member_of", "note": "note-1", "visibility": "public", "appellation": "丁"},
            {"source": "opp-1", "target": "cast-1", "edge_type": "rival_of", "note": "", "visibility": "secret", "appellation": ""},
        ],
        "facts": [
            {"id": "fact-1", "proposition": "proposition-1", "truth_value": True, "sensitivity": "secret", "related_refs": ["cast-1"], "nobody_knows": False},
            {"id": "fact-2", "proposition": "proposition-2", "truth_value": False, "sensitivity": "public", "related_refs": ["faction-1", "fact-1"], "nobody_knows": False},
        ],
        "knowledge": [
            {"observer_ref": "cast-1", "fact_id": "fact-1", "state": "knows", "channel": "seeded"},
            {"observer_ref": "faction-1", "fact_id": "fact-2", "state": "believes", "channel": "seeded"},
        ],
        "rules": [{"id": "rule-1", "statement": "rule-statement", "hardness": "hard", "checkable": {"kind": "exclusive_relation", "edge_type": "serves", "max_per_source": 1}}],
        "clocks": [{"id": "clock-1", "label": "clock-label", "due_in": {"amount": 3, "unit": "时"}, "owner_ref": "faction-1", "scope": "public", "on_expire": "clock-expire"}],
        "reader_promises": [{"id": "promise-1", "summary": "promise-summary", "promise_type": "promise-type", "expected_payoff_window": "window-1"}],
        "reveal_candidates": [{"fact_id": "fact-1", "arc_number": 1, "note": ""}],
    }


@pytest.fixture
def pack(story_engine, map_atlas, seed):
    return {
        "world": {"story_engine": story_engine, "map_atlas": map_atlas},
        "world_seed": seed,
        "book_arc_blueprint": {"arcs": [{"arc_number": 1}, {"arc_number": 2}]},
    }


def _build(seed, story_engine, *, project_id="project-1", map_node_ids=MAP_NODE_IDS):
    return build_world_seed_deltas(
        project_id=project_id,
        revision_id="revision-1",
        seed=seed,
        story_engine=story_engine,
        map_node_ids=map_node_ids,
        calendar=calendar_from_payload(seed["calendar"]),
        lexicon=lexicon_from_payload(seed["lexicon"]),
    )


def _patches_by_type(delta):
    patches: dict[str, list] = {}
    for patch in delta.node_patches:
        patches.setdefault(str(patch.node_type), []).append(patch)
    return patches


def test_build_world_seed_deltas_maps_every_section(seed, story_engine) -> None:
    world, cognition, narrative = _build(seed, story_engine)

    def node_id(kind: str, ref: str) -> str:
        return seed_ref_id("project-1", kind, ref)

    assert [delta.id for delta in (world, cognition, narrative)] == [
        "genesis-seed:project-1:world",
        "genesis-seed:project-1:cognition",
        "genesis-seed:project-1:narrative",
    ]
    assert [str(delta.delta_type) for delta in (world, cognition, narrative)] == [
        "world_state", "cognition", "narrative_control",
    ]
    for delta in (world, cognition, narrative):
        assert delta.project_id == "project-1" and delta.chapter_number == 0
        assert (delta.source_type, delta.source_id) == ("genesis_seed", "revision-1")
        assert delta.story_time == "3日2时"
        assert delta.story_time_start == delta.story_time_end == OPENING

    nodes = _patches_by_type(world)
    assert {kind: len(items) for kind, items in nodes.items()} == {
        "character": 2, "faction": 1, "agenda": 1, "intent": 1, "rule": 1, "clock": 1,
    }
    assert (len(world.edge_patches), len(world.fact_patches)) == (2, 2)
    assert len(cognition.cognition_patches) == 2 and len(narrative.narrative_patches) == 1
    assert not world.cognition_patches and not cognition.node_patches and not narrative.node_patches
    every_patch = [*world.node_patches, *world.edge_patches, *world.fact_patches, *narrative.narrative_patches]
    assert {str(patch.op) for patch in every_patch} == {"create"}
    assert seed_ref_id("project-1", "fact", "fact-1").startswith("fact-")

    cast, opp = nodes["character"]
    assert cast.node_id == node_id("character", "cast-1")
    assert cast.new_value["project_id"] == "project-1"
    assert (cast.new_value["name"], cast.new_value["summary"]) == ("甲", "role-cast")
    assert cast.new_value["profile"] == {"narrative_role": "role-cast", "culture_tag": "culture-1", "fear": "fear-cast"}
    assert cast.new_value["state"] == {
        "alive_status": "alive", "location_id": "loc-db-1",
        "restraint": {"state": "free", "by_ref": "", "since": None},
        "actor_tier": "major", "next_turn_at": OPENING, "travel_mode_ids": ["mode-1"],
        "resources": ["resource-1"], "goal": "desire-cast", "faction_id": node_id("faction", "faction-1"),
    }
    assert cast.new_value["metadata"] == {"genesis_ref": "cast-1", "source": "genesis_seed"}
    assert opp.new_value["profile"]["fear"] == ""
    assert (opp.new_value["state"]["location_id"], opp.new_value["state"]["faction_id"]) == ("loc-db-2", "")

    (faction,) = nodes["faction"]
    assert faction.node_id == node_id("faction", "faction-1")
    assert (faction.new_value["name"], faction.new_value["summary"]) == ("丙", "goal-faction")
    assert faction.new_value["profile"] == {"narrative_role": "role-faction", "culture_tag": "culture-2"}
    assert faction.new_value["state"] == {
        "actor_tier": "minor", "next_turn_at": OPENING, "location_id": "loc-db-1",
        "goal": "goal-faction", "resources": ["resource-2"],
    }
    assert faction.new_value["metadata"] == {"genesis_ref": "faction-1", "source": "genesis_seed"}

    fact_1 = f"fact:{node_id('fact', 'fact-1')}"
    (agenda,) = nodes["agenda"]
    assert agenda.node_id == node_id("agenda", "cast-1/agenda-1")
    assert agenda.new_value["name"] == "agenda-goal"
    assert agenda.new_value["profile"] == {
        "owner_ref": cast.node_id, "goal": "agenda-goal",
        "motivation": "agenda-motivation", "horizon": "agenda-horizon",
    }
    assert agenda.new_value["state"] == {
        "status": "active", "priority": 3, "means_refs": [faction.node_id, fact_1],
        "constraints": ["constraint-1"], "secrecy_refs": [opp.node_id],
    }

    (intent,) = nodes["intent"]
    assert intent.node_id == node_id("intent", "cast-1/intent-1")
    assert intent.new_value["name"] == "intent-summary"
    assert intent.new_value["profile"] == {
        "owner_ref": cast.node_id, "agenda_ref": agenda.node_id, "action_type": "move",
        "summary": "intent-summary", "target_refs": [opp.node_id, fact_1], "location_id": "loc-db-2",
        "requires_onstage": False, "impact_level": "minor", "expected_effects": [], "preconditions": [],
    }
    assert intent.new_value["state"] == {"status": "planned", "scheduled_at": OPENING + 2 * 1440, "slip_count": 0}

    (rule,) = nodes["rule"]
    assert rule.node_id == node_id("rule", "rule-1") and rule.new_value["name"] == "rule-statement"
    assert rule.new_value["profile"] == {
        "statement": "rule-statement", "hardness": "hard", "checkable": {"kind": "exclusive_relation", "edge_type": "serves", "max_per_source": 1},
    }
    assert rule.new_value["state"] == {"status": "active"}

    (clock,) = nodes["clock"]
    assert clock.node_id == node_id("clock", "clock-1") and clock.new_value["name"] == "clock-label"
    assert clock.new_value["profile"] == {
        "label": "clock-label", "owner_ref": faction.node_id, "scope": "public", "on_expire": "clock-expire",
    }
    assert clock.new_value["state"] == {"due_at": OPENING + 3 * 60, "status": "running"}

    member, rival = world.edge_patches
    assert (member.source_id, member.target_id, member.edge_type, member.edge_family) == (
        cast.node_id, faction.node_id, "member_of", "organization",
    )
    assert member.edge_id == node_id("edge", "cast-1/member_of/faction-1")
    assert member.new_value["visibility_default"] == "visible"
    assert member.new_value["metadata"] == {
        "note": "note-1", "appellation": "丁", "genesis_ref": "cast-1/member_of/faction-1",
    }
    assert (rival.edge_family, rival.new_value["visibility_default"]) == ("social", "hidden")

    secret, public = world.fact_patches
    assert secret.fact_id == node_id("fact", "fact-1")
    assert (secret.truth_value, secret.sensitivity_level, secret.related_refs) == ("true", "secret", [cast.node_id])
    assert (public.truth_value, public.sensitivity_level, public.related_refs) == (
        "false", "public", [faction.node_id, fact_1],
    )
    assert public.new_value["related_node_refs"] == [faction.node_id, fact_1]
    assert public.new_value["sensitivity_level"] == "public"

    knows, believes = cognition.cognition_patches
    assert (str(knows.observer_type), knows.observer_id, knows.op, knows.field_path) == (
        "character", cast.node_id, "merge", "knowledge_by_ref",
    )
    assert knows.new_value == {fact_1: "knows"}
    assert knows.evidence_refs == [f"knowledge:seeded:-:{OPENING}"]
    assert (str(believes.observer_type), believes.observer_id) == ("faction", faction.node_id)
    assert believes.new_value == {f"fact:{node_id('fact', 'fact-2')}": "believes"}

    (promise,) = narrative.narrative_patches
    assert promise.target_ref == f"promise:{node_id('promise', 'promise-1')}"
    assert promise.new_value["node_type"] == "promise"
    assert (promise.new_value["title"], promise.new_value["status"]) == ("promise-summary", "open")
    assert promise.new_value["payload"]["promise_type"] == "promise-type"
    assert promise.new_value["payload"]["expected_payoff_window"] == "window-1"
    assert promise.new_value["payload"]["created_at_chapter"] == 0


@pytest.mark.parametrize("checkable,expected", [
    (None, None),
    ({"kind": "actor_cannot_enter", "location_ref": "node-2", "include_children": False,
      "except_refs": ["cast-1", "opp-1"], "except_member_of": ["faction-1"]},
     {"kind": "actor_cannot_enter", "location_ref": "loc-db-2", "include_children": False,
      "except_refs": [seed_ref_id("project-1", "character", ref) for ref in ("cast-1", "opp-1")],
      "except_member_of": [seed_ref_id("project-1", "faction", "faction-1")]}),
    ({"kind": "actor_cannot_enter", "location_ref": "node-1"},
     {"kind": "actor_cannot_enter", "location_ref": "loc-db-1", "include_children": True,
      "except_refs": [], "except_member_of": []}),
    ({"kind": "blocked_channel", "fact_ref": "fact-1", "source_refs": ["cast-1", "faction-1"],
      "channels": ["told", "rumor"]},
     {"kind": "blocked_channel", "fact_ref": f"fact:{seed_ref_id('project-1', 'fact', 'fact-1')}",
      "source_refs": [seed_ref_id("project-1", "character", "cast-1"), seed_ref_id("project-1", "faction", "faction-1")],
      "channels": ["told", "rumor"]}),
    ({"kind": "requires_travel_mode", "edge_types": ["road"], "mode_ids": ["mode-1"]},
     {"kind": "requires_travel_mode", "edge_types": ["road"], "mode_ids": ["mode-1"]}),
    ({"kind": "exclusive_relation", "edge_type": "serves", "max_per_source": 2},
     {"kind": "exclusive_relation", "edge_type": "serves", "max_per_source": 2}),
    ({"kind": "irreversible_state", "node_types": ["character"], "field": "alive_status", "value": "dead"},
     {"kind": "irreversible_state", "node_types": ["character"], "field": "alive_status", "value": "dead"}),
])
def test_build_remaps_rule_predicate_refs(seed, story_engine, checkable, expected) -> None:
    seed["rules"][0]["checkable"] = checkable
    before = deepcopy(seed)

    world, _, _ = _build(seed, story_engine)

    rule = _patches_by_type(world)["rule"][0]
    assert rule.new_value["profile"]["checkable"] == expected
    assert seed == before


@pytest.mark.parametrize("checkable,path,ref", [
    ({"kind": "actor_cannot_enter", "location_ref": "missing-site"}, "location_ref", "missing-site"),
    ({"kind": "actor_cannot_enter", "location_ref": "node-1", "except_refs": ["missing-actor"]},
     "except_refs[0]", "missing-actor"),
    ({"kind": "actor_cannot_enter", "location_ref": "node-1", "except_member_of": ["missing-group"]},
     "except_member_of[0]", "missing-group"),
    ({"kind": "blocked_channel", "fact_ref": "missing-fact", "source_refs": ["cast-1"], "channels": ["told"]},
     "fact_ref", "missing-fact"),
    ({"kind": "blocked_channel", "fact_ref": "fact-1", "source_refs": ["missing-source"], "channels": ["told"]},
     "source_refs[0]", "missing-source"),
    ({"kind": "requires_travel_mode", "edge_types": ["road"], "mode_ids": ["missing-mode"]},
     "mode_ids[0]", "missing-mode"),
])
def test_build_rejects_unresolved_rule_predicate_refs(seed, story_engine, checkable, path, ref) -> None:
    seed["rules"][0]["checkable"] = checkable

    with pytest.raises(WorldSeedCommitError) as raised:
        _build(seed, story_engine)

    assert len(raised.value.issues) == 1
    assert f"rules[0].checkable.{path}: {ref}" in raised.value.issues[0]


@pytest.mark.parametrize("checkable", [
    {"kind": "unknown", "location_ref": "node-1"},
    {"kind": "actor_cannot_enter", "location_ref": "node-1", "other_ref": "cast-1"},
    {"kind": "actor_cannot_enter", "location_ref": ""},
    "invalid",
])
def test_build_rejects_invalid_rule_predicate(seed, story_engine, checkable) -> None:
    seed["rules"][0]["checkable"] = checkable
    with pytest.raises(WorldSeedCommitError, match=r"invalid_checkable rules\[0\].checkable"):
        _build(seed, story_engine)


def test_seed_ids_are_scoped_to_project_and_kind() -> None:
    assert seed_ref_id("project-1", "fact", "x") != seed_ref_id("project-2", "fact", "x")
    assert seed_ref_id("project-1", "fact", "x") != seed_ref_id("project-1", "rule", "x")
    assert len(seed_ref_id("project-1", "character", "cast-1")) == len("character-") + 20


def test_commit_rejects_unresolved_location_ref(seed, story_engine) -> None:
    with pytest.raises(WorldSeedCommitError) as raised:
        _build(seed, story_engine, map_node_ids={"node-1": "loc-db-1"})
    assert isinstance(raised.value, HandoffStageError)
    assert {tuple(issue.split(":", 1)[0].split(" ")) for issue in raised.value.issues} == {
        ("unresolved_location_ref", "actors[0].intents[0].location_ref"),
        ("unresolved_location_ref", "actors[1].location_ref"),
    }
    assert "unresolved_location_ref" in str(raised.value)


@pytest.mark.parametrize("collection", ["agendas", "intents", "facts", "rules", "clocks", "reader_promises"])
def test_build_rejects_items_without_id(seed, story_engine, collection) -> None:
    items = seed["actors"][0][collection] if collection in ("agendas", "intents") else seed[collection]
    items[0]["id"] = ""
    with pytest.raises(WorldSeedCommitError) as raised:
        _build(seed, story_engine)
    assert "missing_id" in {issue.split(" ", 1)[0] for issue in raised.value.issues}


def test_build_rejects_duplicate_relations(seed, story_engine) -> None:
    seed["relations"].append(deepcopy(seed["relations"][0]))
    with pytest.raises(WorldSeedCommitError) as raised:
        _build(seed, story_engine)
    assert [issue.split(" ", 1)[0] for issue in raised.value.issues] == ["duplicate_ref"]


# --- PostgreSQL ---------------------------------------------------------------


def _session_factory():
    engine = get_engine(postgres_test_url())
    init_db(engine)
    return get_session_factory(engine)


def _setup_project(session, pack, *, map_node_ids=MAP_NODE_IDS) -> tuple[str, str]:
    project = StateUpdater(session).create_project(
        title="world seed", premise="world seed", genre="test",
        runtime_policy=RuntimePolicy.for_profile("standard"),
        creation_status="genesis_ready",
    )
    revision = BookGenesisRevision(project_id=project.id, pack_json=json.dumps(pack, ensure_ascii=False))
    session.add(revision)
    for genesis_ref, db_id in map_node_ids.items():
        session.add(MapNodeRow(
            id=f"{db_id}-{project.id}", project_id=project.id, node_type="site",
            metadata_json=json.dumps({"source_node_id": genesis_ref}),
        ))
    session.add(MapNodeRow(id=f"loc-unbound-{project.id}", project_id=project.id, node_type="site"))
    session.flush()
    return project.id, revision.id


def _commit(session, project_id: str, revision_id: str, pack):
    return WorldSeedCommitter().commit(
        session=session,
        project=session.get(Project, project_id),
        revision=session.get(BookGenesisRevision, revision_id),
        pack=pack,
    )


def _row_counts(session, project_id: str) -> dict[str, int]:
    return {
        model.__tablename__: session.scalar(
            select(func.count()).select_from(model).where(model.project_id == project_id)
        )
        for model in (
            GraphDeltaRow, WorldNodeRow, WorldNodeStateRow, StoryCalendarRow, BookLexiconRow,
            CharacterIdentityMapRow, DecisionEvent,
        )
    } | {
        "outbox_events": session.scalar(
            select(func.count()).select_from(OutboxEvent).where(OutboxEvent.aggregate_id == project_id)
        ),
    }


def test_commit_world_seed_writes_book_state_calendar_and_lexicon(pack) -> None:
    Session = _session_factory()
    with Session.begin() as session:
        project_id, revision_id = _setup_project(session, pack)
        assert map_node_ids_by_genesis_ref(session, project_id) == {
            "node-1": f"loc-db-1-{project_id}", "node-2": f"loc-db-2-{project_id}",
        }
        summary = _commit(session, project_id, revision_id, pack)
    assert summary.skipped is False
    assert summary.graph_delta_ids == tuple(
        f"genesis-seed:{project_id}:{section}" for section in ("world", "cognition", "narrative")
    )
    assert (
        summary.actor_count, summary.agenda_count, summary.intent_count, summary.clock_count,
        summary.fact_count, summary.knowledge_count, summary.rule_count, summary.promise_count,
    ) == (3, 1, 1, 1, 2, 2, 1, 1)

    def node_id(kind: str, ref: str) -> str:
        return seed_ref_id(project_id, kind, ref)

    with Session.begin() as session:
        runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=0)
        world = runtime.world
        cast = world.get_node(node_id("character", "cast-1"))
        assert (cast.name, cast.summary) == ("甲", "role-cast")
        assert cast.profile["narrative_role"] == "role-cast" and cast.profile["fear"] == "fear-cast"
        assert cast.profile["personality_loadout"]
        assert cast.metadata["genesis_ref"] == "cast-1" and cast.metadata["source"] == "genesis_seed"
        cast_state = world.get_state(cast.id)
        assert cast_state["location_id"] == f"loc-db-1-{project_id}"
        assert cast_state["faction_id"] == node_id("faction", "faction-1")
        assert cast_state["restraint"] == {"state": "free", "by_ref": "", "since": None}
        assert (cast_state["alive_status"], cast_state["next_turn_at"]) == ("alive", OPENING)
        assert world.get_node(node_id("character", "opp-1")).profile["personality_loadout"]

        faction = world.get_node(node_id("faction", "faction-1"))
        assert faction.metadata["genesis_ref"] == "faction-1"
        assert world.get_state(faction.id)["location_id"] == f"loc-db-1-{project_id}"
        agenda = world.get_node(node_id("agenda", "cast-1/agenda-1"))
        assert agenda.profile["owner_ref"] == cast.id and world.get_state(agenda.id)["status"] == "active"
        intent = world.get_node(node_id("intent", "cast-1/intent-1"))
        assert intent.profile["agenda_ref"] == agenda.id
        assert intent.profile["location_id"] == f"loc-db-2-{project_id}"
        assert world.get_state(intent.id)["scheduled_at"] == OPENING + 2 * 1440
        clock = world.get_node(node_id("clock", "clock-1"))
        assert world.get_state(clock.id) == {"due_at": OPENING + 180, "status": "running"}
        rule = world.get_node(node_id("rule", "rule-1"))
        assert rule.profile["hardness"] == "hard" and world.get_state(rule.id) == {"status": "active"}

        member = world.edges_by_id[node_id("edge", "cast-1/member_of/faction-1")]
        assert member.metadata["appellation"] == "丁" and member.visibility_default == "visible"
        assert world.edges_by_id[node_id("edge", "opp-1/rival_of/cast-1")].visibility_default == "hidden"
        secret = world.facts_by_id[node_id("fact", "fact-1")]
        assert (secret.sensitivity_level, secret.truth_value) == ("secret", "true")
        assert world.facts_by_id[node_id("fact", "fact-2")].sensitivity_level == "public"

        fact_1 = f"fact:{node_id('fact', 'fact-1')}"
        assert runtime.cognition_by_observer[("character", cast.id)].knowledge_by_ref == {fact_1: "knows"}
        assert runtime.cognition_by_observer[("faction", faction.id)].knowledge_by_ref == {
            f"fact:{node_id('fact', 'fact-2')}": "believes",
        }
        promise = runtime.narrative.get_node(node_id("promise", "promise-1"))
        assert (promise.node_type, promise.title, promise.status) == ("promise", "promise-summary", "open")

        assert BookStateQuery(session).current_story_minute(project_id, as_of_chapter=0) == OPENING
        counts = _row_counts(session, project_id)
        assert (counts["story_calendars"], counts["book_lexicons"]) == (1, 1)
        identities = session.scalars(
            select(CharacterIdentityMapRow).where(CharacterIdentityMapRow.project_id == project_id)
        ).all()
        assert {(row.book_state_node_id, row.genesis_ref_id) for row in identities} == {
            (cast.id, "cast-1"), (node_id("character", "opp-1"), "opp-1"),
        }
        event = session.scalars(
            select(DecisionEvent).where(
                DecisionEvent.project_id == project_id,
                DecisionEvent.event_type == DecisionEventType.WORLD_SEED_COMMITTED,
            )
        ).one()
        assert json.loads(event.payload_json)["actor_count"] == 3
        outbox = session.scalars(
            select(OutboxEvent).where(OutboxEvent.event_id == f"genesis-seed:{project_id}:projection")
        ).one()
        assert outbox.event_type == "knowledge.projection.refresh_requested"
        assert json.loads(outbox.payload_json)["as_of_chapter"] == 0


def test_commit_world_seed_lock_lets_other_sessions_reference_the_project(pack) -> None:
    # Start-writing enqueues its generation task from another session before it commits.
    Session = _session_factory()
    with Session.begin() as session:
        project_id, revision_id = _setup_project(session, pack)
    with Session() as seeding:
        _commit(seeding, project_id, revision_id, pack)
        with Session.begin() as other:
            other.execute(text("SET LOCAL lock_timeout = '2s'"))
            StateUpdater(other).save_decision_event(DecisionEventInfo(
                project_id=project_id, scope="project",
                event_type=DecisionEventType.CONTINUE_REQUESTED, actor_type="api", summary="enqueue",
            ))
        seeding.rollback()


def test_commit_world_seed_is_idempotent(pack) -> None:
    Session = _session_factory()
    with Session.begin() as session:
        project_id, revision_id = _setup_project(session, pack)
        first = _commit(session, project_id, revision_id, pack)
    with Session.begin() as session:
        before = _row_counts(session, project_id)
        second = _commit(session, project_id, revision_id, pack)
        assert _row_counts(session, project_id) == before
    assert second.skipped is True and first.skipped is False
    assert second.graph_delta_ids == first.graph_delta_ids
    assert second.actor_count == first.actor_count == 3


def test_commit_world_seed_refuses_when_book_already_has_canon(pack) -> None:
    Session = _session_factory()
    with Session.begin() as session:
        project_id, revision_id = _setup_project(session, pack)
        BookStateRepository(session).append_graph_delta(
            GraphDelta(id=f"writer-{project_id}", project_id=project_id, source_type="writer_output")
        )
    with pytest.raises(WorldSeedCommitError) as raised:
        with Session.begin() as session:
            _commit(session, project_id, revision_id, pack)
    assert isinstance(raised.value.__cause__, CanonStaleVersion)
    assert [issue.split(" ", 1)[0] for issue in raised.value.issues] == ["canon_conflict"]
    with Session() as session:
        counts = _row_counts(session, project_id)
        assert (counts["story_calendars"], counts["book_lexicons"]) == (0, 0)
        assert session.scalars(
            select(GraphDeltaRow.id).where(GraphDeltaRow.project_id == project_id)
        ).all() == [f"writer-{project_id}"]


def test_commit_failure_after_calendar_save_leaves_nothing_after_rollback(pack) -> None:
    Session = _session_factory()
    with Session.begin() as session:
        project_id, revision_id = _setup_project(session, pack, map_node_ids={"node-1": "loc-db-1"})
    with Session() as session:
        with pytest.raises(WorldSeedCommitError) as raised:
            _commit(session, project_id, revision_id, pack)
        assert {issue.split(" ", 1)[0] for issue in raised.value.issues} == {"unresolved_location_ref"}
        # The calendar and lexicon were flushed before map resolution failed.
        assert _row_counts(session, project_id)["story_calendars"] == 1
        session.rollback()
    with Session() as session:
        counts = _row_counts(session, project_id)
        assert {name: counts[name] for name in (
            "story_calendars", "book_lexicons", "graph_deltas", "world_nodes", "character_identity_map",
        )} == {"story_calendars": 0, "book_lexicons": 0, "graph_deltas": 0, "world_nodes": 0, "character_identity_map": 0}


def test_commit_world_seed_requires_chapter_zero_seed_deltas(pack, seed, story_engine) -> None:
    Session = _session_factory()
    with Session.begin() as session:
        project_id, revision_id = _setup_project(session, pack)
        deltas = _build(seed, story_engine, project_id=project_id)
        admission = CanonAdmissionService()
        for changes in (
            ApprovedGraphDeltaSet(project_id=project_id, chapter_number=1, graph_deltas=deltas),
            ApprovedGraphDeltaSet(
                project_id=project_id, chapter_number=0,
                graph_deltas=[deltas[0].model_copy(update={"source_type": "writer_output"})],
            ),
        ):
            with pytest.raises(ValueError):
                admission.commit_world_seed(
                    session=session, project_id=project_id, revision_id=revision_id,
                    approved_changes=changes,
                )
        assert _row_counts(session, project_id)["graph_deltas"] == 0


def test_commit_world_seed_rejects_invalid_seed_without_writes(pack) -> None:
    pack["world_seed"]["actors"] = [actor for actor in pack["world_seed"]["actors"] if actor["ref"] != "opp-1"]
    Session = _session_factory()
    with Session.begin() as session:
        project_id, revision_id = _setup_project(session, pack)
        with pytest.raises(WorldSeedCommitError) as raised:
            _commit(session, project_id, revision_id, pack)
        assert [issue.split(" ", 1)[0] for issue in raised.value.issues] == ["missing_actor"]
        counts = _row_counts(session, project_id)
        assert (counts["story_calendars"], counts["book_lexicons"], counts["graph_deltas"]) == (0, 0, 0)
