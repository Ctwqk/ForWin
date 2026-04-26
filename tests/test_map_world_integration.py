from __future__ import annotations

from sqlalchemy import select

from forwin.book_state.runtime import ObjectiveWorldGraph, distance_between_world_nodes
from forwin.map.generator import generate_subworld_map
from forwin.map.pathfinding import MapGraph
from forwin.map.protocol import MapAnchorNodeSpec, SubWorldMapSpec
from forwin.map.service import (
    create_or_update_book_map,
    create_or_update_subworld_map,
    get_book_map_runtime,
    resolve_world_node_location_id,
)
from forwin.models import MapGenerationRunRow, Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.protocol.context import ReviewContextPack
from forwin.protocol.experience import ChapterExperiencePlan
from forwin.protocol.scene import SceneOutput
from forwin.protocol.state_change import TimeAdvance
from forwin.protocol.book_state import WorldNode
from forwin.protocol.writer import WriterOutput
from forwin.reviewer.webnovel import WebNovelExperienceReviewer


def _spec() -> SubWorldMapSpec:
    return SubWorldMapSpec(
        project_id="p1",
        subworld_id="sw1",
        name="苍澜大陆",
        subworld_type="continent",
        target_region_count=3,
        target_node_count=12,
        target_edge_density=1.5,
        required_region_roles=["帝国核心区", "遗迹带"],
        required_anchor_nodes=[
            MapAnchorNodeSpec(name="帝都", node_type="settlement", region_role="帝国核心区", narrative_function="权力中心"),
            MapAnchorNodeSpec(name="上古遗迹入口", node_type="site", region_role="遗迹带", narrative_function="探索与伏笔"),
        ],
        generation_seed=42,
    )


def _realm_spec() -> SubWorldMapSpec:
    return SubWorldMapSpec(
        project_id="p1",
        subworld_id="sw_realm",
        name="上古秘境界",
        subworld_type="realm",
        target_region_count=3,
        target_node_count=12,
        target_edge_density=1.5,
        required_region_roles=["秘境核心区", "遗迹带"],
        required_anchor_nodes=[
            MapAnchorNodeSpec(name="秘境祭坛", node_type="site", region_role="秘境核心区", narrative_function="跨界目标"),
        ],
        generation_seed=43,
    )


def test_service_persists_generation_run_and_builds_runtime() -> None:
    engine = get_engine(":memory:")
    init_db(engine)
    Session = get_session_factory(engine)
    with Session() as session:
        session.add(Project(id="p1", title="书", premise="premise"))
        session.commit()

        result = create_or_update_subworld_map(session, _spec())
        runtime = get_book_map_runtime(session, "p1")
        run_count = session.execute(select(MapGenerationRunRow)).scalars().all()

        assert result.validation_report.valid is True
        assert len(run_count) == 1
        assert "sw1" in runtime.regions_by_subworld
        assert result.map_nodes[0].id in runtime.map_nodes_by_id


def test_book_map_generation_links_subworlds_with_world_gate_path() -> None:
    engine = get_engine(":memory:")
    init_db(engine)
    Session = get_session_factory(engine)
    with Session() as session:
        session.add(Project(id="p1", title="书", premise="premise"))
        session.commit()

        result = create_or_update_book_map(session, [_spec(), _realm_spec()])
        runtime = get_book_map_runtime(session, "p1")
        graph = MapGraph(nodes=list(runtime.map_nodes_by_id.values()), edges=list(runtime.map_edges_by_id.values()))
        start = next(node.id for node in runtime.map_nodes_by_id.values() if node.name == "帝都")
        target = next(node.id for node in runtime.map_nodes_by_id.values() if node.name == "秘境祭坛")
        path = graph.shortest_path(start, target, metric="travel_time")

        assert result.validation_report.valid is True
        assert result.inter_subworld_edges
        assert runtime.inter_subworld_edges_by_id
        assert path.reachable is True
        assert any(
            runtime.map_edges_by_id[edge_id.removesuffix("__reverse")].edge_type == "world_gate"
            for edge_id in path.path_edge_ids
        )


def test_world_node_distance_and_site_state_resolve_map_node_ids() -> None:
    result = create_or_update_subworld_map_for_test()
    city = next(node for node in result.map_nodes if node.name == "帝都")
    ruin = next(node for node in result.map_nodes if node.name == "上古遗迹入口")
    world = ObjectiveWorldGraph(
        nodes=[
            WorldNode(id="char_mc", project_id="p1", node_type="character", state={"location_id": city.id}),
            WorldNode(id="char_enemy", project_id="p1", node_type="character", state={"location_id": ruin.id}),
            WorldNode(id="site_ruin_state", project_id="p1", node_type="site_state", profile={"map_node_id": ruin.id}),
        ]
    )
    graph = MapGraph(nodes=result.map_nodes, edges=result.map_edges)

    path = distance_between_world_nodes(world, graph, "char_mc", "char_enemy")

    assert path.reachable is True
    assert path.total_travel_time >= 0
    assert resolve_world_node_location_id(world, "site_ruin_state") == ruin.id


def test_reviewer_flags_scene_movement_that_exceeds_map_travel_time_budget() -> None:
    result = generate_subworld_map(_spec())
    city = next(node for node in result.map_nodes if node.name == "帝都")
    ruin = next(node for node in result.map_nodes if node.name == "上古遗迹入口")
    map_context = {
        "chapter_travel_time_budget": 0.1,
        "map_nodes": [node.model_dump(mode="json") for node in result.map_nodes],
        "map_edges": [edge.model_dump(mode="json") for edge in result.map_edges],
    }
    context = ReviewContextPack(
        project_id="p1",
        project_title="书",
        chapter_number=1,
        chapter_plan_title="赶路",
        chapter_plan_one_line="主角赶往遗迹",
        chapter_experience_plan=ChapterExperiencePlan(progress_markers=["抵达遗迹"]),
        map_context=map_context,
    )
    output = WriterOutput(
        project_id="p1",
        chapter_number=1,
        title="赶路",
        body="主角出发。下一刻他已经抵达遗迹？",
        end_of_chapter_summary="主角抵达遗迹。",
        scene_outputs=[
            SceneOutput(scene_no=1, scene_objective="出发", scene_location_id=city.id, text="出发。", immersion_anchor="城门"),
            SceneOutput(scene_no=2, scene_objective="抵达", scene_location_id=ruin.id, text="抵达。", immersion_anchor="遗迹"),
        ],
        time_advance=TimeAdvance(new_time_label="片刻后", duration_description="片刻后"),
    )

    verdict = WebNovelExperienceReviewer(llm_enabled=False).review(context, output)

    assert any(issue.rule_name == "map_travel_time_exceeds_chapter_time" for issue in verdict.issues)


def create_or_update_subworld_map_for_test():
    return generate_subworld_map(_spec())
