from __future__ import annotations

from sqlalchemy import func, select

from forwin.book_state import (
    BookStateCompiler,
    BookStateDeltaAdapter,
    BookStateProjection,
    BookStateRepository,
    BookStateReviewGate,
    CognitionView,
    LegacyBookStateImporter,
    MapGraph,
    NarrativeControlGraph,
    distance_between_world_nodes,
)
from forwin.book_state.schema import validate_world_node
from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.book_state import GraphDeltaRow, NarrativeNodeRow, WorldNodeRow
from forwin.models.entity import Entity, EntityState
from forwin.protocol.book_state import (
    ApprovedGraphDeltaSet,
    CognitionOverlay,
    GraphDelta,
    MapEdge,
    MapNode,
    NodePatch,
    WorldNode,
)
from forwin.protocol.world_v4 import DeltaKind, DeltaSource, DeltaSourceType, ExtractedWorldChangeSet, WorldDelta


def _session():
    engine = get_engine(":memory:")
    init_db(engine)
    return get_session_factory(engine)


def _project(session) -> str:
    project = Project(title="BookState", premise="p", genre="g", setting_summary="s")
    session.add(project)
    session.flush()
    return project.id


def test_schema_runtime_map_and_cognition_overlay() -> None:
    node = WorldNode(id="char_mc", project_id="p1", node_type="character", state={"location_id": "loc_city"})
    assert validate_world_node(node).ok

    graph = MapGraph(
        nodes=[
            MapNode(id="loc_city", project_id="p1", node_type="settlement", coordinates={"x": 0, "y": 0}),
            MapNode(id="loc_gate", project_id="p1", node_type="waypoint", coordinates={"x": 1, "y": 0}),
            MapNode(id="loc_inner", project_id="p1", node_type="room", coordinates={"x": 2, "y": 0}),
        ],
        edges=[
            MapEdge(id="road", project_id="p1", from_node_id="loc_city", to_node_id="loc_gate", edge_type="road", travel_time=5, distance=1),
            MapEdge(id="path", project_id="p1", from_node_id="loc_gate", to_node_id="loc_inner", edge_type="path", travel_time=5, distance=1),
            MapEdge(id="secret", project_id="p1", from_node_id="loc_city", to_node_id="loc_inner", edge_type="hidden_route", travel_time=1, distance=1, status="hidden", discovered_by_default=False),
        ],
        cognition_by_observer={
            ("character", "char_mc"): CognitionView(CognitionOverlay(id="cog", project_id="p1", observer_type="character", observer_id="char_mc", hidden_refs=["map_edge:secret"]))
        },
    )

    objective = graph.shortest_path("loc_city", "loc_inner", metric="travel_time", algorithm="astar")
    known = graph.shortest_path("loc_city", "loc_inner", metric="travel_time", observer=("character", "char_mc"))

    assert objective.path_edge_ids == ["secret"]
    assert known.path_edge_ids == ["road", "path"]
    assert len(graph.all_pairs_shortest_paths(node_ids=["loc_city", "loc_gate"])) == 2


def test_compiler_persists_delta_snapshots_and_review_gate() -> None:
    Session = _session()
    with Session.begin() as session:
        project_id = _project(session)
        repo = BookStateRepository(session)
        repo.create_world_node(WorldNode(id="char_mc", project_id=project_id, node_type="character", state={"location_id": "loc_a"}))
        repo.append_world_node_state(project_id=project_id, node_id="char_mc", node_type="character", as_of_chapter=0, state={"location_id": "loc_a"})
        repo.create_world_node(WorldNode(id="char_origin", project_id=project_id, node_type="character", state={"location_id": "loc_a"}))
        repo.append_world_node_state(project_id=project_id, node_id="char_origin", node_type="character", as_of_chapter=0, state={"location_id": "loc_a"})
        repo.create_map_node(MapNode(id="loc_a", project_id=project_id, node_type="settlement"))
        repo.create_map_node(MapNode(id="loc_b", project_id=project_id, node_type="settlement"))
        repo.create_map_edge(MapEdge(id="edge_ab", project_id=project_id, from_node_id="loc_a", to_node_id="loc_b", edge_type="road", travel_time=1))
        changes = ApprovedGraphDeltaSet(
            project_id=project_id,
            chapter_number=1,
            graph_deltas=[
                GraphDelta(
                    id="delta_move",
                    project_id=project_id,
                    node_patches=[NodePatch(node_id="char_mc", node_type="character", op="set", field_path="state.location_id", old_value="loc_a", new_value="loc_b")],
                )
            ],
        )

        verdict = BookStateReviewGate(session).review(changes)
        result = BookStateCompiler(session).compile(changes)
        runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=1)
        delta_count = session.scalar(select(func.count()).select_from(GraphDeltaRow))

    assert verdict.accepted is True
    assert result.committed is True
    assert delta_count == 1
    assert runtime.world.get_state("char_mc")["location_id"] == "loc_b"
    movement_path = distance_between_world_nodes(runtime.world, runtime.map, "char_origin", "char_mc")
    assert movement_path.reachable is True
    assert movement_path.path_edge_ids == ["edge_ab"]
    assert movement_path.total_travel_time == 1


def test_review_gate_blocks_unknown_location_id_but_warns_for_legacy_location() -> None:
    Session = _session()
    with Session.begin() as session:
        project_id = _project(session)
        repo = BookStateRepository(session)
        repo.create_map_node(MapNode(id="loc_a", project_id=project_id, node_type="settlement"))
        changes = ApprovedGraphDeltaSet(
            project_id=project_id,
            chapter_number=1,
            graph_deltas=[
                GraphDelta(
                    id="delta_bad_location",
                    project_id=project_id,
                    node_patches=[
                        NodePatch(node_id="char_mc", node_type="character", op="set", field_path="state.location_id", old_value="loc_a", new_value="missing"),
                        NodePatch(node_id="char_old", node_type="character", op="set", field_path="state.location", old_value="loc_a", new_value="missing"),
                    ],
                )
            ],
        )

        verdict = BookStateReviewGate(session).review(changes)

    severities = {issue.target_ref: issue.severity for issue in verdict.issues if issue.code == "movement_unknown_map_node"}
    assert verdict.accepted is False
    assert severities["node:char_mc"] == "error"
    assert severities["node:char_old"] == "warning"


def test_v4_adapter_and_legacy_import_create_book_state_rows() -> None:
    Session = _session()
    with Session.begin() as session:
        project_id = _project(session)
        entity = Entity(project_id=project_id, kind="character", name="陆沉", created_at_chapter=1)
        session.add(entity)
        session.flush()
        session.add(EntityState(entity_id=entity.id, as_of_chapter=1, state_json='{"location_id": "loc_city"}'))
        counts = LegacyBookStateImporter(session).import_project(project_id)

        source = DeltaSource(source_type=DeltaSourceType.CHARACTER_ACTION, actor_id="char_mc")
        changes = ExtractedWorldChangeSet(
            project_id=project_id,
            chapter_number=2,
            world_deltas=[
                WorldDelta(
                    delta_id="wd1",
                    project_id=project_id,
                    world_line_id="line_main",
                    delta_kind=DeltaKind.VISIBLE,
                    summary="主角进入黑石城",
                    narrative_chapter=2,
                    source=source,
                    affected_entities=[entity.id],
                )
            ],
        )
        approved = BookStateDeltaAdapter().from_world_change_set(changes)
        result = BookStateCompiler(session).compile(approved)
        node_count = session.scalar(select(func.count()).select_from(WorldNodeRow))
        narrative_count = session.scalar(select(func.count()).select_from(NarrativeNodeRow))

    assert counts["world_nodes"] == 1
    assert result.committed is True
    assert node_count == 2
    assert narrative_count >= 1
    assert NarrativeControlGraph(nodes=[]).open_gap_ids() == []
