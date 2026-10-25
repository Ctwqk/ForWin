from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select

from forwin.api_schema import PersonalityLoadoutUpdateRequest
from forwin.book_state import BookStateCompiler, BookStateProjection, BookStateRepository
from forwin.book_state.query import BookStateQuery
from forwin.canon import CanonAdmissionService, CanonStaleVersion
from forwin.characters.creation import CharacterCreationHelper
from forwin.characters.models import CharacterCreationRequest
from forwin.http.adapters.api_book_state_routes import build_handlers
from forwin.models.base import Base, get_engine, get_session_factory, init_db
from forwin.models.book_state import GraphDeltaRow, WorldNodeStateRow
from forwin.models.knowledge import KnowledgeEditProposalRow
from forwin.proposals import proposal_review
from forwin.proposals.proposal_review import approve_world_edit_proposal
from forwin.protocol.book_state import (
    ApprovedGraphDeltaSet,
    CognitionPatch,
    EdgePatch,
    FactPatch,
    GraphDelta,
    NarrativePatch,
    NodePatch,
    WorldNode,
)
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url


_LOADOUT_A = {"dominant": {"skill": "trait-loyal-protector", "weight": 0.72}}
_LOADOUT_B = {"dominant": {"skill": "trait-cold-strategist", "weight": 0.64}}


def _session_factory(backend="postgresql"):
    if backend == "sqlite":
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
    else:
        engine = get_engine(postgres_test_url())
        init_db(engine)
    return engine, get_session_factory(engine)


def _create_project(session) -> str:
    return StateUpdater(session).create_project(
        title="BookState versioning",
        premise="versioning",
        genre="test",
        setting_summary="test",
        runtime_policy=RuntimePolicy.for_profile("standard"),
    ).id


def _compile(session, project_id: str, chapter: int, *, delta_id: str = "", **patches) -> None:
    result = BookStateCompiler(session).compile(
        ApprovedGraphDeltaSet(
            project_id=project_id,
            chapter_number=chapter,
            graph_deltas=[
                GraphDelta(
                    id=delta_id or f"delta_{project_id}_{chapter}",
                    project_id=project_id,
                    chapter_number=chapter,
                    **patches,
                )
            ],
        )
    )
    assert result.committed, result.blocked_reasons


def _compile_without_deltas(session, project_id: str, chapter: int) -> None:
    """Accept a chapter whose writer output produced no BookState deltas."""
    result = BookStateCompiler(session).compile(
        ApprovedGraphDeltaSet(project_id=project_id, chapter_number=chapter, graph_deltas=[])
    )
    assert result.committed, result.blocked_reasons


def _load(session, project_id: str, chapter: int):
    return BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=chapter)


def _create_character(project_id: str, node_id: str, *, name: str = "", **payload) -> NodePatch:
    profile = {"personality_loadout": _LOADOUT_A, **payload.pop("profile", {})}
    return NodePatch(
        node_id=node_id,
        node_type="character",
        op="create",
        new_value={"project_id": project_id, "name": name or node_id, "profile": profile, **payload},
    )


def test_non_state_node_patch_keeps_runtime_state() -> None:
    engine, Session = _session_factory()
    state = {"status": "idle", "health": "healthy", "location_id": "loc_gate"}
    with Session.begin() as session:
        project_id = _create_project(session)
        _compile(session, project_id, 0, node_patches=[_create_character(project_id, "char_a", state=state)])
        _compile(
            session,
            project_id,
            1,
            node_patches=[
                NodePatch(
                    node_id="char_a",
                    node_type="character",
                    op="set",
                    field_path="metadata.writer_location",
                    new_value={"location_id": "loc_gate"},
                )
            ],
        )
        snapshot = BookStateRepository(session).latest_world_snapshot(project_id, 1)
        runtime = _load(session, project_id, 1)

    assert snapshot.as_of_chapter == 1
    assert snapshot.world_node_state_index["char_a"] == state
    assert runtime.world.get_state("char_a") == state
    assert runtime.world.get_node("char_a").metadata["writer_location"] == {"location_id": "loc_gate"}
    engine.dispose()


def test_fact_patch_field_path_survives_reload() -> None:
    engine, Session = _session_factory()
    with Session.begin() as session:
        project_id = _create_project(session)
        _compile(
            session,
            project_id,
            0,
            fact_patches=[
                FactPatch(
                    fact_id="fact_gate",
                    op="create",
                    proposition="gate is sealed",
                    new_value={"project_id": project_id, "state": {"status": "sealed"}},
                )
            ],
        )
        _compile(
            session,
            project_id,
            1,
            fact_patches=[
                FactPatch(
                    fact_id="fact_gate",
                    op="set",
                    field_path="state.status",
                    old_value="sealed",
                    new_value="open",
                )
            ],
        )

    with Session() as session:
        deltas = BookStateRepository(session).list_graph_deltas(project_id, through_chapter=1)

    assert deltas[-1].fact_patches[0].field_path == "state.status"
    engine.dispose()


def test_profile_name_alias_changes_are_versioned_by_chapter() -> None:
    engine, Session = _session_factory()
    with Session.begin() as session:
        project_id = _create_project(session)
        _compile(
            session,
            project_id,
            0,
            node_patches=[_create_character(project_id, "char_a", name="甲", profile={"narrative_role": "a"})],
        )
    with Session.begin() as session:
        _compile(
            session,
            project_id,
            2,
            node_patches=[
                NodePatch(node_id="char_a", node_type="character", op="set", field_path="name", old_value="甲", new_value="乙"),
                NodePatch(
                    node_id="char_a",
                    node_type="character",
                    op="set",
                    field_path="profile.narrative_role",
                    old_value="a",
                    new_value="b",
                ),
                NodePatch(node_id="char_a", node_type="character", op="append", field_path="aliases", new_value="乙二"),
            ],
        )

    with Session() as session:
        before = _load(session, project_id, 1).world.get_node("char_a")
        after = _load(session, project_id, 2).world.get_node("char_a")
        latest = BookStateRepository(session).get_world_node("char_a")

    assert (before.name, before.profile["narrative_role"], before.aliases) == ("甲", "a", [])
    assert (after.name, after.profile["narrative_role"], after.aliases) == ("乙", "b", ["乙二"])
    assert (latest.name, latest.profile["narrative_role"], latest.aliases) == ("乙", "b", ["乙二"])
    engine.dispose()


def test_direct_writer_update_is_visible_as_of_latest_chapter() -> None:
    engine, Session = _session_factory()
    with Session.begin() as session:
        project_id = _create_project(session)
        _compile(session, project_id, 0, node_patches=[_create_character(project_id, "char_a", state={"status": "idle"})])
    with Session.begin() as session:
        _compile(
            session,
            project_id,
            1,
            node_patches=[
                NodePatch(
                    node_id="char_a",
                    node_type="character",
                    op="set",
                    field_path="state.status",
                    old_value="idle",
                    new_value="resting",
                )
            ],
        )
    with Session.begin() as session:
        repo = BookStateRepository(session)
        node = repo.get_world_node("char_a")
        updated = node.model_copy(update={"profile": {**node.profile, "personality_loadout": _LOADOUT_B}})
        repo.create_world_node(updated, version_chapter=repo.latest_available_chapter(project_id))

    with Session() as session:
        chapter_zero = _load(session, project_id, 0)
        chapter_one = _load(session, project_id, 1)

    assert chapter_zero.world.get_node("char_a").profile["personality_loadout"] == _LOADOUT_A
    assert chapter_one.world.get_node("char_a").profile["personality_loadout"] == _LOADOUT_B
    assert chapter_one.world.get_state("char_a") == {"status": "resting"}
    engine.dispose()


def test_same_chapter_versions_resolve_in_write_order() -> None:
    engine, Session = _session_factory()
    with Session.begin() as session:
        project_id = _create_project(session)
        repo = BookStateRepository(session)
        node = WorldNode(id="char_a", project_id=project_id, node_type="character")
        repo.create_world_node(node.model_copy(update={"name": "v0"}), version_chapter=0, version_state={"status": "idle"})
        for index in range(1, 6):
            repo.create_world_node(node.model_copy(update={"name": f"v{index}"}), version_chapter=0)
        world = repo.load_base_world_graph(project_id, as_of_chapter=0)

    assert world.get_node("char_a").name == "v5"
    assert world.get_state("char_a") == {"status": "idle"}
    engine.dispose()


def test_deactivated_edge_is_active_for_earlier_chapters() -> None:
    engine, Session = _session_factory()
    with Session.begin() as session:
        project_id = _create_project(session)
        _compile(
            session,
            project_id,
            0,
            node_patches=[_create_character(project_id, "char_a"), _create_character(project_id, "char_b")],
            edge_patches=[
                EdgePatch(
                    edge_id="edge_ab",
                    op="create",
                    source_id="char_a",
                    target_id="char_b",
                    edge_type="ally_of",
                    edge_family="social",
                    new_value={"project_id": project_id},
                )
            ],
        )
    with Session.begin() as session:
        _compile(
            session,
            project_id,
            2,
            edge_patches=[
                EdgePatch(
                    edge_id="edge_ab",
                    op="deactivate",
                    source_id="char_a",
                    target_id="char_b",
                    edge_type="ally_of",
                    edge_family="social",
                    field_path="is_active",
                    old_value=True,
                )
            ],
        )

    with Session() as session:
        repo = BookStateRepository(session)
        chapter_one = _load(session, project_id, 1)
        chapter_two = _load(session, project_id, 2)
        row_edge = next(edge for edge in repo.list_world_edges(project_id) if edge.id == "edge_ab")
        snapshot = repo.latest_world_snapshot(project_id, 2)

    assert chapter_one.world.edges_by_id["edge_ab"].is_active is True
    assert chapter_two.world.edges_by_id["edge_ab"].is_active is False
    assert row_edge.ended_at_chapter == 2
    assert "edge_ab" not in snapshot.active_edge_ids
    engine.dispose()


_EDGE_AB = {
    "edge_id": "edge_ab",
    "source_id": "char_a",
    "target_id": "char_b",
    "edge_type": "ally_of",
    "edge_family": "social",
}


def _compile_reactivated_edge(Session) -> str:
    """edge_ab is created in chapter 0, ended in chapter 2 and created again in chapter 4."""
    with Session.begin() as session:
        project_id = _create_project(session)
        _compile(
            session,
            project_id,
            0,
            node_patches=[_create_character(project_id, "char_a"), _create_character(project_id, "char_b")],
            edge_patches=[EdgePatch(op="create", new_value={"project_id": project_id}, **_EDGE_AB)],
        )
    with Session.begin() as session:
        _compile(
            session,
            project_id,
            2,
            edge_patches=[EdgePatch(op="deactivate", field_path="is_active", old_value=True, **_EDGE_AB)],
        )
    with Session.begin() as session:
        _compile(
            session,
            project_id,
            4,
            edge_patches=[
                EdgePatch(
                    op="create",
                    new_value={"project_id": project_id, "established_at_chapter": 4},
                    **_EDGE_AB,
                )
            ],
        )
    return project_id


def _edge_activity(session, project_id: str, chapter: int) -> tuple[bool, int | None]:
    edge = _load(session, project_id, chapter).world.edges_by_id["edge_ab"]
    return edge.is_active, edge.ended_at_chapter


def test_create_on_an_ended_edge_reactivates_it_and_keeps_the_ended_span() -> None:
    engine, Session = _session_factory()
    project_id = _compile_reactivated_edge(Session)

    with Session() as session:
        activity = {chapter: _edge_activity(session, project_id, chapter) for chapter in range(5)}
        row = BookStateRepository(session).get_world_edge("edge_ab")
        snapshot_three = BookStateRepository(session).latest_world_snapshot(project_id, 3)
        snapshot_four = BookStateRepository(session).latest_world_snapshot(project_id, 4)

    assert activity == {0: (True, None), 1: (True, None), 2: (False, 2), 3: (False, 2), 4: (True, None)}
    assert (row.is_active, row.ended_at_chapter, row.established_at_chapter) == (True, None, 0)
    assert row.metadata["inactive_spans"] == [[2, 4]]
    assert "edge_ab" not in snapshot_three.active_edge_ids
    assert "edge_ab" in snapshot_four.active_edge_ids
    engine.dispose()


@pytest.mark.parametrize(("from_chapter", "expected"), [(4, (False, 2)), (2, (True, None))])
def test_historical_invalidation_restores_a_reactivated_edge(from_chapter, expected) -> None:
    engine, Session = _session_factory()
    project_id = _compile_reactivated_edge(Session)

    with Session.begin() as session:
        repo = BookStateRepository(session)
        repo.invalidate_project_range(project_id, from_chapter=from_chapter, through_chapter=4)
        row = repo.get_world_edge("edge_ab")
        rewound = _edge_activity(session, project_id, from_chapter - 1)

    assert (row.is_active, row.ended_at_chapter) == expected
    assert "inactive_spans" not in row.metadata
    assert rewound == expected
    engine.dispose()


def test_past_chapter_compile_keeps_a_later_reactivation() -> None:
    engine, Session = _session_factory()
    project_id = _compile_reactivated_edge(Session)
    with Session.begin() as session:
        _compile(
            session,
            project_id,
            2,
            delta_id="delta_past_edit",
            edge_patches=[EdgePatch(op="set", field_path="weight", old_value=1.0, new_value=0.5, **_EDGE_AB)],
        )

    with Session() as session:
        row = BookStateRepository(session).get_world_edge("edge_ab")
        activity = {chapter: _edge_activity(session, project_id, chapter) for chapter in (2, 3, 4)}

    assert (row.is_active, row.ended_at_chapter, row.weight) == (True, None, 0.5)
    assert row.metadata["inactive_spans"] == [[2, 4]]
    assert activity == {2: (False, 2), 3: (False, 2), 4: (True, None)}
    engine.dispose()


def test_narrative_loads_from_snapshot_without_full_replay(monkeypatch) -> None:
    engine, Session = _session_factory()
    with Session.begin() as session:
        project_id = _create_project(session)
        _compile(
            session,
            project_id,
            0,
            narrative_patches=[
                NarrativePatch(
                    target_ref="plot_thread:thread_a",
                    op="create",
                    new_value={"title": "thread a", "status": "active"},
                )
            ],
            # A reader overlay gives the cognition layer a checkpoint too, so the
            # replay start is decided by the world/narrative checkpoint alone.
            cognition_patches=[
                CognitionPatch(
                    observer_type="reader",
                    observer_id="reader",
                    op="append",
                    field_path="visible_refs",
                    new_value="narrative_node:thread_a",
                )
            ],
        )
    with Session.begin() as session:
        _compile(
            session,
            project_id,
            1,
            narrative_patches=[
                NarrativePatch(
                    target_ref="plot_thread:thread_a",
                    op="set",
                    field_path="status",
                    old_value="active",
                    new_value="resolved",
                )
            ],
        )

    calls: list[int] = []
    original = BookStateRepository.list_graph_deltas

    def spy(self, project_id, *, after_chapter=-1, through_chapter):
        calls.append(after_chapter)
        return original(self, project_id, after_chapter=after_chapter, through_chapter=through_chapter)

    monkeypatch.setattr(BookStateRepository, "list_graph_deltas", spy)
    with Session() as session:
        runtime = _load(session, project_id, 2)

    assert runtime.narrative.get_node("thread_a").status == "resolved"
    assert calls == [1]
    engine.dispose()


def test_active_promise_ids_count_narrative_promise_nodes() -> None:
    engine, Session = _session_factory()
    with Session.begin() as session:
        project_id = _create_project(session)
        repo = BookStateRepository(session)
        _compile(
            session,
            project_id,
            0,
            narrative_patches=[
                NarrativePatch(target_ref="promise:promise_open", op="create", new_value={"status": "open"}),
                NarrativePatch(target_ref="promise:promise_paid", op="create", new_value={"status": "resolved"}),
            ],
        )
        snapshot = repo.latest_world_snapshot(project_id, 0)

    assert snapshot.active_promise_ids == ["promise_open"]
    assert set(snapshot.narrative_node_index) == {"promise_open", "promise_paid"}
    assert snapshot.metadata["narrative_indexed"] is True
    engine.dispose()


def test_second_compile_at_same_chapter_keeps_first_compile_effects() -> None:
    engine, Session = _session_factory()
    with Session.begin() as session:
        project_id = _create_project(session)
        _compile(
            session,
            project_id,
            0,
            node_patches=[_create_character(project_id, "char_a", name="A0")],
            narrative_patches=[
                NarrativePatch(
                    target_ref="plot_thread:thread_a",
                    op="create",
                    new_value={"title": "thread a", "status": "active"},
                )
            ],
        )
    with Session.begin() as session:
        _compile(
            session,
            project_id,
            1,
            delta_id="delta_canon_1",
            node_patches=[
                NodePatch(node_id="char_a", node_type="character", op="set", field_path="name", old_value="A0", new_value="A1")
            ],
            narrative_patches=[
                NarrativePatch(
                    target_ref="plot_thread:thread_a",
                    op="set",
                    field_path="status",
                    old_value="active",
                    new_value="resolved",
                )
            ],
        )
    with Session.begin() as session:
        _compile(
            session,
            project_id,
            1,
            delta_id="delta_edit_1",
            node_patches=[
                NodePatch(
                    node_id="char_a",
                    node_type="character",
                    op="set",
                    field_path="metadata.note",
                    new_value="edited",
                )
            ],
        )

    with Session() as session:
        runtime = _load(session, project_id, 1)
        repo = BookStateRepository(session)
        row = repo.get_world_node("char_a")
        snapshot = repo.latest_world_snapshot(project_id, 1)

    node = runtime.world.get_node("char_a")
    assert runtime.narrative.get_node("thread_a").status == "resolved"
    assert (node.name, node.metadata["note"]) == ("A1", "edited")
    assert (row.name, row.metadata["note"]) == ("A1", "edited")
    assert snapshot.source_delta_ids == ["delta_canon_1", "delta_edit_1"]
    engine.dispose()


def test_past_chapter_compile_does_not_clobber_newer_rows() -> None:
    engine, Session = _session_factory()
    edge_patch = {
        "edge_id": "edge_ab",
        "source_id": "char_a",
        "target_id": "char_b",
        "edge_type": "ally_of",
        "edge_family": "social",
    }
    with Session.begin() as session:
        project_id = _create_project(session)
        _compile(
            session,
            project_id,
            0,
            node_patches=[_create_character(project_id, "char_a", name="A0"), _create_character(project_id, "char_b")],
            edge_patches=[EdgePatch(op="create", new_value={"project_id": project_id}, **edge_patch)],
        )
    with Session.begin() as session:
        _compile(
            session,
            project_id,
            1,
            node_patches=[
                NodePatch(node_id="char_b", node_type="character", op="set", field_path="metadata.seen", new_value=True)
            ],
        )
    with Session.begin() as session:
        _compile(
            session,
            project_id,
            2,
            node_patches=[
                NodePatch(node_id="char_a", node_type="character", op="set", field_path="name", old_value="A0", new_value="A2")
            ],
            edge_patches=[EdgePatch(op="deactivate", field_path="is_active", old_value=True, **edge_patch)],
        )
    with Session.begin() as session:
        _compile(
            session,
            project_id,
            1,
            delta_id="delta_past_edit",
            node_patches=[
                NodePatch(node_id="char_a", node_type="character", op="set", field_path="metadata.note", new_value="past")
            ],
            edge_patches=[EdgePatch(op="set", field_path="weight", old_value=1.0, new_value=0.5, **edge_patch)],
        )

    with Session() as session:
        repo = BookStateRepository(session)
        node_row = repo.get_world_node("char_a")
        edge_row = next(edge for edge in repo.list_world_edges(project_id) if edge.id == "edge_ab")
        chapter_one = _load(session, project_id, 1)
        chapter_two = _load(session, project_id, 2)

    assert (node_row.name, node_row.metadata["note"]) == ("A2", "past")
    assert chapter_one.world.get_node("char_a").name == "A0"
    assert chapter_two.world.get_node("char_a").name == "A2"
    assert (edge_row.is_active, edge_row.ended_at_chapter, edge_row.weight) == (False, 2, 0.5)
    assert chapter_one.world.edges_by_id["edge_ab"].is_active is True
    assert chapter_two.world.edges_by_id["edge_ab"].is_active is False
    engine.dispose()


def _propose(Session, project_id: str, operation: dict, *, chapter: int | None = None) -> str:
    patch = {"forwin_patch": [operation]}
    if chapter is not None:
        patch["frontmatter"] = {"as_of_chapter": chapter}
    with Session.begin() as session:
        row = KnowledgeEditProposalRow(
            project_id=project_id,
            source="world_studio",
            target_node_id=str(operation.get("node_id", "")),
            proposed_patch_json=json.dumps(patch),
            status="pending",
        )
        session.add(row)
        session.flush()
        return row.id


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
def test_world_edit_proposal_compiles_at_latest_canon_chapter(backend) -> None:
    engine, Session = _session_factory(backend)
    with Session.begin() as session:
        project_id = _create_project(session)
        _compile(session, project_id, 0, node_patches=[_create_character(project_id, "char_a", name="A0")])
    with Session.begin() as session:
        _compile(
            session,
            project_id,
            2,
            node_patches=[
                NodePatch(node_id="char_a", node_type="character", op="set", field_path="state.status", new_value="awake")
            ],
        )

    rename = _propose(Session, project_id, {"op": "rename_node", "node_id": "char_a", "name": "B"})
    with Session.begin() as session:
        result = approve_world_edit_proposal(session, project_id=project_id, proposal_id=rename)
        delta_chapter = session.get(GraphDeltaRow, result.row.graph_delta_id).chapter_number

    assert result.projection_refresh["as_of_chapter"] == 2
    assert delta_chapter == 2
    with Session() as session:
        assert BookStateRepository(session).get_world_node("char_a").name == "B"
        assert _load(session, project_id, 1).world.get_node("char_a").name == "A0"
        chapter_two = _load(session, project_id, 2)
    assert chapter_two.world.get_node("char_a").name == "B"
    assert chapter_two.world.get_state("char_a") == {"status": "awake"}

    stale = _propose(Session, project_id, {"op": "rename_node", "node_id": "char_a", "name": "C", "old_value": "A0"})
    with pytest.raises(HTTPException) as rejected:
        with Session.begin() as session:
            approve_world_edit_proposal(session, project_id=project_id, proposal_id=stale)
    assert rejected.value.status_code == 409
    engine.dispose()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
def test_world_edit_after_zero_delta_chapter_lands_in_that_chapter(backend) -> None:
    engine, Session = _session_factory(backend)
    with Session.begin() as session:
        project_id = _create_project(session)
        _compile(session, project_id, 0, node_patches=[_create_character(project_id, "char_a", state={"status": "idle"})])
    with Session.begin() as session:
        _compile_without_deltas(session, project_id, 1)
    proposal_id = _propose(
        Session,
        project_id,
        {"op": "set_node_field", "node_id": "char_a", "field_path": "state.status", "new_value": "awake"},
    )
    with Session.begin() as session:
        approved = approve_world_edit_proposal(session, project_id=project_id, proposal_id=proposal_id)
        delta_chapter = session.get(GraphDeltaRow, approved.row.graph_delta_id).chapter_number
    with Session.begin() as session:
        _compile(session, project_id, 2, node_patches=[_create_character(project_id, "char_b")])

    with Session() as session:
        states = [_load(session, project_id, chapter).world.get_state("char_a") for chapter in (0, 1, 2)]

    assert delta_chapter == 1
    assert states == [{"status": "idle"}, {"status": "awake"}, {"status": "awake"}]
    engine.dispose()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
def test_future_chapter_world_edit_clamps_down(backend) -> None:
    engine, Session = _session_factory(backend)
    with Session.begin() as session:
        project_id = _create_project(session)
        _compile(session, project_id, 0, node_patches=[_create_character(project_id, "char_a", state={"status": "idle"})])
    with Session.begin() as session:
        _compile_without_deltas(session, project_id, 1)
    proposal_id = _propose(
        Session,
        project_id,
        {"op": "set_node_field", "node_id": "char_a", "field_path": "state.status", "new_value": "awake"},
        chapter=5,
    )
    with Session.begin() as session:
        approved = approve_world_edit_proposal(session, project_id=project_id, proposal_id=proposal_id)
        delta_chapter = session.get(GraphDeltaRow, approved.row.graph_delta_id).chapter_number
    # The next accepted chapter still lands after the edit.
    with Session.begin() as session:
        _compile(session, project_id, 2, node_patches=[_create_character(project_id, "char_b")])

    with Session() as session:
        states = [_load(session, project_id, chapter).world.get_state("char_a") for chapter in (0, 1, 2)]

    assert (delta_chapter, approved.projection_refresh["as_of_chapter"]) == (1, 1)
    assert states == [{"status": "idle"}, {"status": "awake"}, {"status": "awake"}]
    engine.dispose()


def test_clamped_world_edit_creates_exist_from_the_compile_chapter() -> None:
    engine, Session = _session_factory()
    with Session.begin() as session:
        project_id = _create_project(session)
        _compile(
            session,
            project_id,
            0,
            node_patches=[_create_character(project_id, "char_a"), _create_character(project_id, "char_b")],
        )
    with Session.begin() as session:
        _compile_without_deltas(session, project_id, 1)
    edge_and_fact = {
        "forwin_patch": [
            {"op": "create_edge", "edge_id": "edge_ab", "edge_type": "ally_of", "source_id": "char_a", "target_id": "char_b"},
            {"op": "create_fact", "fact_id": "fact_pact", "proposition": "a and b swore a pact"},
        ],
        "frontmatter": {"as_of_chapter": 5},
    }
    promise = {
        "forwin_patch": [{"op": "create_reader_promise", "promise_id": "promise_debt", "new_value": {"summary": "a debt"}}],
        "frontmatter": {"as_of_chapter": 5},
    }
    # No structured patch: approval records an audit fact instead.
    note = {"new_value": "b owes a a debt", "frontmatter": {"as_of_chapter": 5}}
    proposal_ids = []
    with Session.begin() as session:
        for patch in (edge_and_fact, promise, note):
            row = KnowledgeEditProposalRow(
                project_id=project_id, source="world_studio", proposed_patch_json=json.dumps(patch), status="pending"
            )
            session.add(row)
            session.flush()
            proposal_ids.append(row.id)
    for proposal_id in proposal_ids:
        with Session.begin() as session:
            approve_world_edit_proposal(session, project_id=project_id, proposal_id=proposal_id)

    with Session() as session:
        world = _load(session, project_id, 1).world
        promises = BookStateQuery(session).reader_promises(project_id, as_of_chapter=1)
    assert "edge_ab" in world.edges_by_id
    assert {"fact_pact", f"proposal_fact_{proposal_ids[2]}"} <= set(world.facts_by_id)
    assert [(item.promise_id, item.created_at_chapter) for item in promises] == [("promise_debt", 1)]

    # What the edit created can be edited once canon moves on.
    with Session.begin() as session:
        _compile_without_deltas(session, project_id, 2)
    reweigh = _propose(
        Session,
        project_id,
        {"op": "set_edge_field", "edge_id": "edge_ab", "field_path": "weight", "new_value": 0.5},
        chapter=2,
    )
    with Session.begin() as session:
        approve_world_edit_proposal(session, project_id=project_id, proposal_id=reweigh)
    with Session() as session:
        assert _load(session, project_id, 2).world.edges_by_id["edge_ab"].weight == 0.5
    engine.dispose()


def _node_versions(session, node_id: str) -> list[tuple[int, str]]:
    rows = session.execute(
        select(WorldNodeStateRow)
        .where(WorldNodeStateRow.node_id == node_id)
        .order_by(WorldNodeStateRow.as_of_chapter, WorldNodeStateRow.seq)
    ).scalars()
    return [(row.as_of_chapter, row.source_delta_id) for row in rows]


def test_compiler_created_character_has_one_version_row() -> None:
    engine, Session = _session_factory()
    with Session.begin() as session:
        project_id = _create_project(session)
        # Without a personality_loadout the compiler hands the create to the character helper.
        create = NodePatch(
            node_id="char_new",
            node_type="character",
            op="create",
            new_value={"project_id": project_id, "name": "char_new", "state": {"status": "idle"}},
        )
        _compile(session, project_id, 0, delta_id="delta_char_new", node_patches=[create])

    with Session() as session:
        versions = _node_versions(session, "char_new")
        node = BookStateRepository(session).get_world_node("char_new")

    assert versions == [(0, "delta_char_new")]
    assert node.profile["personality_loadout"]
    engine.dispose()


def test_direct_writer_never_versions_a_node_before_it_exists() -> None:
    engine, Session = _session_factory()
    with Session.begin() as session:
        project_id = _create_project(session)
        _compile(session, project_id, 0, node_patches=[_create_character(project_id, "char_a")])
        CharacterCreationHelper(session).create_character(
            CharacterCreationRequest(
                project_id=project_id,
                source="api_manual",
                character_id="char_later",
                name="Later",
                created_at_chapter=3,
                personality_loadout=_LOADOUT_A,
            )
        )

    build_handlers(get_session=Session)["set_character_personality_loadout"](
        project_id,
        "char_later",
        PersonalityLoadoutUpdateRequest(personality_loadout=_LOADOUT_A, reason="manual edit"),
    )

    with Session() as session:
        assert BookStateRepository(session).latest_available_chapter(project_id) == 0
        assert [chapter for chapter, _ in _node_versions(session, "char_later")] == [3, 3]
    engine.dispose()


def test_world_edit_behind_latest_canon_chapter_is_stale(monkeypatch) -> None:
    engine, Session = _session_factory()
    with Session.begin() as session:
        project_id = _create_project(session)
        _compile(session, project_id, 0, node_patches=[_create_character(project_id, "char_a")])
    with Session.begin() as session:
        _compile_without_deltas(session, project_id, 1)

    def past_edit(chapter: int) -> ApprovedGraphDeltaSet:
        return ApprovedGraphDeltaSet(
            project_id=project_id,
            chapter_number=chapter,
            graph_deltas=[
                GraphDelta(
                    id=f"delta_past_edit_{chapter}",
                    project_id=project_id,
                    chapter_number=chapter,
                    node_patches=[
                        NodePatch(node_id="char_a", node_type="character", op="set", field_path="metadata.note", new_value="x")
                    ],
                )
            ],
        )

    proposal_id = _propose(Session, project_id, {"op": "append_manual_note", "node_id": "char_a", "text": "x"})
    with pytest.raises(CanonStaleVersion, match="chapter 1"):
        with Session.begin() as session:
            CanonAdmissionService().commit_world_edit(
                session=session,
                project_id=project_id,
                proposal_id=proposal_id,
                approved_changes=past_edit(0),
                reason="",
                trigger="test",
            )

    # The approval path computes its chapter before taking the project lock; a
    # chapter accepted in between surfaces as a conflict, not a server error.
    monkeypatch.setattr(proposal_review, "_world_edit_chapter", lambda *_args: 0)
    with pytest.raises(HTTPException) as conflict:
        with Session.begin() as session:
            approve_world_edit_proposal(session, project_id=project_id, proposal_id=proposal_id)
    assert conflict.value.status_code == 409

    with Session() as session:
        assert session.get(KnowledgeEditProposalRow, proposal_id).status == "pending"
        assert session.query(GraphDeltaRow).filter_by(project_id=project_id).count() == 1
    engine.dispose()


def test_world_edit_http_fills_current_state_and_keeps_supplied_cas() -> None:
    from forwin.http.adapters.api_proposal_routes import build_handlers as proposal_handlers

    engine, Session = _session_factory("sqlite")
    with Session.begin() as session:
        project_id = _create_project(session)
        _compile(session, project_id, 0, node_patches=[_create_character(
            project_id, "char_a", state={"status": "idle"})])
        _compile(session, project_id, 1, node_patches=[NodePatch(node_id="char_a", node_type="character",
            op="set", field_path="state.status", old_value="idle", new_value="awake")])
    operation = {"op": "set_node_field", "node_id": "char_a", "field_path": "state.status", "new_value": "busy"}
    proposal_id = _propose(Session, project_id, operation)
    handler = proposal_handlers(get_session=Session)["approve_project_proposal"]
    approved = handler(project_id, proposal_id)
    assert approved.status == "accepted"
    with Session() as session:
        delta = BookStateRepository(session).list_graph_deltas(project_id, through_chapter=1)[-1]
        assert delta.node_patches[0].old_value == "awake"
        assert _load(session, project_id, 1).world.get_state("char_a")["status"] == "busy"
    stale = _propose(Session, project_id, {**operation, "old_value": "awake", "new_value": "idle"})
    with pytest.raises(HTTPException) as error:
        handler(project_id, stale)
    assert error.value.status_code == 409
    with Session() as session:
        assert session.get(KnowledgeEditProposalRow, stale).status == "pending"
        assert _load(session, project_id, 1).world.get_state("char_a")["status"] == "busy"
    engine.dispose()
