from __future__ import annotations

from sqlalchemy import func, select

from forwin.book_state import BookStateCompiler, BookStateProjection, BookStateRepository
from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.book_state import GraphDeltaPatchRow, GraphDeltaRow, WorldNodeStateRow
from forwin.protocol.book_state import (
    ApprovedGraphDeltaSet,
    CognitionOverlay,
    CognitionPatch,
    GraphDelta,
    MapEdge,
    MapNode,
    MapPatch,
    NodePatch,
    WorldNode,
)


def _create_project(session, title: str = "BookState 测试") -> str:
    project = Project(
        title=title,
        premise="BookState persistence",
        genre="玄幻",
        setting_summary="黑石城与上古遗迹",
    )
    session.add(project)
    session.flush()
    return project.id


def test_repository_roundtrip_loads_runtime_with_map_and_cognition() -> None:
    engine = get_engine(":memory:")
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project_id = _create_project(session)
        repo = BookStateRepository(session)
        repo.create_world_node(
            WorldNode(
                id="char_mc",
                project_id=project_id,
                node_type="character",
                name="陆沉",
                state={"location_id": "loc_city"},
            )
        )
        repo.append_world_node_state(
            project_id=project_id,
            node_id="char_mc",
            node_type="character",
            as_of_chapter=0,
            state={"location_id": "loc_city", "status": "idle"},
        )
        repo.create_map_node(MapNode(id="loc_city", project_id=project_id, node_type="settlement", name="黑石城"))
        repo.create_map_node(MapNode(id="loc_inner", project_id=project_id, node_type="room", name="遗迹内殿"))
        repo.create_map_edge(
            MapEdge(
                id="edge_secret",
                project_id=project_id,
                from_node_id="loc_city",
                to_node_id="loc_inner",
                edge_type="hidden_route",
                travel_time=0.5,
                status="hidden",
                discovered_by_default=False,
            )
        )
        repo.upsert_cognition_overlay(
            CognitionOverlay(
                id="cog_mc_0",
                project_id=project_id,
                observer_type="character",
                observer_id="char_mc",
                as_of_chapter=0,
                hidden_refs=["map_edge:edge_secret"],
            )
        )

        runtime = BookStateProjection(session).load_runtime_as_of(
            project_id,
            as_of_chapter=0,
            observer_keys=[("character", "char_mc")],
        )

    assert runtime.world.get_state("char_mc")["status"] == "idle"
    assert runtime.map.shortest_path("loc_city", "loc_inner").reachable is True
    known = runtime.map.shortest_path(
        "loc_city",
        "loc_inner",
        observer=("character", "char_mc"),
    )
    assert known.reachable is False
    assert runtime.cognition_by_observer[("character", "char_mc")].get_belief("map_edge:edge_secret") == "hidden"


def test_compiler_commits_delta_patches_and_rebuilds_snapshots() -> None:
    engine = get_engine(":memory:")
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project_id = _create_project(session)
        repo = BookStateRepository(session)
        repo.create_world_node(
            WorldNode(
                id="char_mc",
                project_id=project_id,
                node_type="character",
                name="陆沉",
                state={"location_id": "loc_village"},
            )
        )
        repo.append_world_node_state(
            project_id=project_id,
            node_id="char_mc",
            node_type="character",
            as_of_chapter=0,
            state={"location_id": "loc_village"},
        )
        repo.create_map_node(MapNode(id="loc_village", project_id=project_id, node_type="settlement"))
        repo.create_map_node(MapNode(id="loc_city", project_id=project_id, node_type="settlement"))
        repo.create_map_edge(
            MapEdge(
                id="edge_village_city",
                project_id=project_id,
                from_node_id="loc_village",
                to_node_id="loc_city",
                edge_type="road",
                travel_time=2,
                status="open",
            )
        )

        result = BookStateCompiler(session).compile(
            ApprovedGraphDeltaSet(
                project_id=project_id,
                chapter_number=1,
                graph_deltas=[
                    GraphDelta(
                        id="delta_ch1",
                        project_id=project_id,
                        chapter_number=1,
                        story_time="第一日",
                        summary="主角进入黑石城，道路随后封锁。",
                        node_patches=[
                            NodePatch(
                                node_id="char_mc",
                                node_type="character",
                                op="set",
                                field_path="state.location_id",
                                old_value="loc_village",
                                new_value="loc_city",
                            )
                        ],
                        map_patches=[
                            MapPatch(
                                target_type="map_edge",
                                target_id="edge_village_city",
                                op="set",
                                field_path="status",
                                old_value="open",
                                new_value="blocked",
                            )
                        ],
                        cognition_patches=[
                            CognitionPatch(
                                observer_type="character",
                                observer_id="char_mc",
                                op="append",
                                field_path="suspected_refs",
                                new_value="fact:road_blockade",
                            )
                        ],
                    )
                ],
            )
        )

        runtime = BookStateProjection(session).load_runtime_as_of(
            project_id,
            as_of_chapter=1,
            observer_keys=[("character", "char_mc")],
        )
        delta_count = session.scalar(select(func.count()).select_from(GraphDeltaRow))
        patch_count = session.scalar(select(func.count()).select_from(GraphDeltaPatchRow))
        state_count = session.scalar(select(func.count()).select_from(WorldNodeStateRow))

    assert result.committed is True
    assert result.graph_delta_ids == ["delta_ch1"]
    assert result.world_snapshot_id
    assert result.map_snapshot_id
    assert runtime.world.get_state("char_mc")["location_id"] == "loc_city"
    assert runtime.map.edges_by_id["edge_village_city"].status == "blocked"
    assert "fact:road_blockade" in runtime.cognition_by_observer[("character", "char_mc")].suspected_refs
    assert delta_count == 1
    assert patch_count == 3
    assert state_count == 2


def test_compiler_blocks_stale_old_value_without_writing_delta() -> None:
    engine = get_engine(":memory:")
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project_id = _create_project(session)
        repo = BookStateRepository(session)
        repo.create_world_node(WorldNode(id="char_mc", project_id=project_id, node_type="character"))
        repo.append_world_node_state(
            project_id=project_id,
            node_id="char_mc",
            node_type="character",
            as_of_chapter=0,
            state={"status": "idle"},
        )

        result = BookStateCompiler(session).compile(
            ApprovedGraphDeltaSet(
                project_id=project_id,
                chapter_number=1,
                graph_deltas=[
                    GraphDelta(
                        id="delta_bad",
                        project_id=project_id,
                        chapter_number=1,
                        node_patches=[
                            NodePatch(
                                node_id="char_mc",
                                node_type="character",
                                op="set",
                                field_path="state.status",
                                old_value="busy",
                                new_value="injured",
                            )
                        ],
                    )
                ],
            )
        )
        delta_count = session.scalar(select(func.count()).select_from(GraphDeltaRow))

    assert result.committed is False
    assert result.blocked_reasons
    assert delta_count == 0
