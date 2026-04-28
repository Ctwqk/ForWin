from __future__ import annotations

import json
from types import SimpleNamespace

from sqlalchemy import select

from forwin.book_state.runtime import ObjectiveWorldGraph, distance_between_world_nodes
from forwin.context.assembler import _build_map_context, assemble_context
from forwin.map.generator import generate_subworld_map
from forwin.map.genesis_adapter import build_subworld_map_specs_from_genesis
from forwin.map.pathfinding import MapGraph
from forwin.map.protocol import MapAnchorNodeSpec, MapGenerationResult, MapValidationReport, SubWorldMapSpec
from forwin.map.service import (
    create_or_update_book_map,
    create_or_update_subworld_map,
    ensure_book_map_from_genesis_atlas,
    get_book_map_runtime,
    resolve_world_node_location_id,
)
from forwin.models import (
    ArcPlanVersion,
    BookGenesisRevision,
    ChapterPlan,
    MapEdgeRow,
    MapGenerationRunRow,
    MapNodeRow,
    MapRegionRow,
    Project,
)
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.protocol.context import ReviewContextPack
from forwin.protocol.experience import ChapterExperiencePlan
from forwin.protocol.scene import SceneOutput
from forwin.protocol.state_change import TimeAdvance
from forwin.protocol.book_state import MapEdge, MapNode, WorldNode
from forwin.protocol.writer import WriterOutput
from forwin.reviewer.webnovel import WebNovelExperienceReviewer
from forwin.state.repo import StateRepository
from forwin.writer.prompts import build_single_chapter_draft_prompt


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
    engine = get_engine(postgres_test_url())
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
    engine = get_engine(postgres_test_url())
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


def test_book_map_generation_rolls_back_partial_rows_when_later_subworld_fails(monkeypatch) -> None:
    from forwin.map import service as map_service

    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)
    original_create_or_update_subworld_map = map_service.create_or_update_subworld_map

    def fake_create_or_update_subworld_map(session, spec, *, commit=False):
        if spec.subworld_id == "sw_realm":
            return MapGenerationResult(
                project_id=spec.project_id,
                subworld_id=spec.subworld_id,
                generation_seed=spec.generation_seed,
                validation_report=MapValidationReport(valid=False, errors=["bad realm map"]),
            )
        return original_create_or_update_subworld_map(session, spec, commit=commit)

    monkeypatch.setattr(map_service, "create_or_update_subworld_map", fake_create_or_update_subworld_map)

    with Session() as session:
        session.add(Project(id="p1", title="书", premise="premise"))
        session.commit()

        result = create_or_update_book_map(session, [_spec(), _realm_spec()])
        map_runs = session.execute(select(MapGenerationRunRow).where(MapGenerationRunRow.project_id == "p1")).scalars().all()
        region_count = session.execute(select(MapRegionRow).where(MapRegionRow.project_id == "p1")).scalars().all()
        node_count = session.execute(select(MapNodeRow).where(MapNodeRow.project_id == "p1")).scalars().all()
        edge_count = session.execute(select(MapEdgeRow).where(MapEdgeRow.project_id == "p1")).scalars().all()

    assert result.validation_report.valid is False
    assert result.validation_report.errors == ["bad realm map"]
    assert map_runs == []
    assert region_count == []
    assert node_count == []
    assert edge_count == []


def test_regenerating_subworld_preserves_valid_inter_subworld_edges() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)
    with Session() as session:
        session.add(Project(id="p1", title="书", premise="premise"))
        session.commit()

        create_or_update_book_map(session, [_spec(), _realm_spec()])
        before = get_book_map_runtime(session, "p1")
        inter_edge_ids = set(before.inter_subworld_edges_by_id)

        create_or_update_subworld_map(session, _spec())
        after = get_book_map_runtime(session, "p1")

        assert inter_edge_ids
    assert inter_edge_ids.issubset(after.inter_subworld_edges_by_id)


def test_arc_map_expansion_adds_missing_subworld_and_world_gate() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)
    atlas = {
        "overview": "主舞台与秘境。",
        "submaps": [
            {"id": "sw-main", "name": "主舞台", "scope": "macro_region", "key_locations": ["黑石城"]},
            {"id": "sw-realm", "name": "上古秘境界", "scope": "realm", "key_locations": ["秘境祭坛"]},
        ],
        "regions": [
            {"id": "region-main", "name": "旧城", "subworld_name": "主舞台"},
            {"id": "region-realm", "name": "秘境核心", "subworld_name": "上古秘境界"},
        ],
        "nodes": [
            {"id": "node-city", "name": "黑石城", "parent_subworld": "sw-main", "parent_region_id": "region-main"},
            {"id": "node-altar", "name": "秘境祭坛", "parent_subworld": "sw-realm", "parent_region_id": "region-realm"},
        ],
    }
    with Session() as session:
        session.add(Project(id="p1", title="书", premise="premise"))
        session.commit()
        first_spec = build_subworld_map_specs_from_genesis(
            project_id="p1",
            genesis_revision_id="rev1",
            map_atlas={**atlas, "submaps": atlas["submaps"][:1], "regions": atlas["regions"][:1], "nodes": atlas["nodes"][:1]},
        )[0]
        create_or_update_book_map(session, [first_spec])

        result = ensure_book_map_from_genesis_atlas(
            session,
            project_id="p1",
            genesis_revision_id="rev1",
            map_atlas=atlas,
        )
        runtime = get_book_map_runtime(session, "p1")

    assert result.validation_report.valid is True
    assert result.summary["created_subworld_ids"] == ["sw-realm"]
    assert result.summary["interconnection_source"] == "default_chain"
    assert "sw-realm" in runtime.regions_by_subworld
    assert runtime.inter_subworld_edges_by_id


def test_arc_map_expansion_uses_explicit_atlas_cross_subworld_edges() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)
    atlas = {
        "overview": "主舞台与秘境通过显式界门相连。",
        "submaps": [
            {"id": "sw-main", "name": "主舞台", "scope": "macro_region", "key_locations": ["黑石城"]},
            {"id": "sw-realm", "name": "上古秘境界", "scope": "realm", "key_locations": ["秘境祭坛"]},
        ],
        "regions": [
            {"id": "region-main", "name": "旧城", "subworld_name": "主舞台"},
            {"id": "region-realm", "name": "秘境核心", "subworld_name": "上古秘境界"},
        ],
        "nodes": [
            {"id": "node-city", "name": "黑石城", "parent_subworld": "sw-main", "parent_region_id": "region-main"},
            {"id": "node-altar", "name": "秘境祭坛", "parent_subworld": "sw-realm", "parent_region_id": "region-realm"},
        ],
        "edges": [
            {
                "id": "atlas-edge-city-altar",
                "from_node_id": "node-city",
                "to_node_id": "node-altar",
                "kind": "portal",
            }
        ],
    }
    with Session() as session:
        session.add(Project(id="p1", title="书", premise="premise"))
        session.commit()
        first_spec = build_subworld_map_specs_from_genesis(
            project_id="p1",
            genesis_revision_id="rev1",
            map_atlas={**atlas, "submaps": atlas["submaps"][:1], "regions": atlas["regions"][:1], "nodes": atlas["nodes"][:1]},
        )[0]
        create_or_update_book_map(session, [first_spec])

        result = ensure_book_map_from_genesis_atlas(
            session,
            project_id="p1",
            genesis_revision_id="rev1",
            map_atlas=atlas,
        )
        runtime = get_book_map_runtime(session, "p1")

    assert result.validation_report.valid is True
    assert result.summary["interconnection_source"] == "atlas_edges"
    assert result.summary["created_subworld_ids"] == ["sw-realm"]
    inter_edges = list(runtime.inter_subworld_edges_by_id.values())
    assert inter_edges
    assert any(edge.metadata.get("source_edge_id") == "atlas-edge-city-altar" for edge in inter_edges)


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
    assert path.path_edge_ids
    assert path.total_travel_time == sum(
        graph.edges_by_id[edge_id].travel_time for edge_id in path.path_edge_ids if edge_id in graph.edges_by_id
    )
    assert graph.shortest_path("missing", ruin.id).reachable is False
    assert resolve_world_node_location_id(world, "site_ruin_state") == ruin.id


def test_reviewer_flags_scene_movement_that_exceeds_map_travel_time_budget() -> None:
    result = generate_subworld_map(_spec())
    city = next(node for node in result.map_nodes if node.name == "帝都")
    ruin = next(node for node in result.map_nodes if node.name == "上古遗迹入口")
    map_context = {
        "chapter_travel_time_budget": 0.1,
        "review_graph": {
            "available": True,
            "map_nodes": [node.model_dump(mode="json") for node in result.map_nodes],
            "map_edges": [edge.model_dump(mode="json") for edge in result.map_edges],
        },
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


def test_reviewer_uses_capped_review_graph_payload_when_cap_exceeded() -> None:
    result = generate_subworld_map(_spec())
    city = next(node for node in result.map_nodes if node.name == "帝都")
    ruin = next(node for node in result.map_nodes if node.name == "上古遗迹入口")
    map_context = {
        "chapter_travel_time_budget": 0.1,
        "review_graph": {
            "available": False,
            "reason": "map_context_graph_cap_exceeded",
            "map_nodes": [node.model_dump(mode="json") for node in result.map_nodes],
            "map_edges": [edge.model_dump(mode="json") for edge in result.map_edges],
        },
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


def test_reviewer_flags_observer_known_hidden_route_detour() -> None:
    nodes = [
        MapNode(id="city", project_id="p1", node_type="settlement", name="城"),
        MapNode(id="gate", project_id="p1", node_type="waypoint", name="关口"),
        MapNode(id="inner", project_id="p1", node_type="site", name="内殿"),
    ]
    objective_edges = [
        MapEdge(id="secret", project_id="p1", from_node_id="city", to_node_id="inner", edge_type="hidden_route", status="hidden", discovered_by_default=False, travel_time=0.2),
        MapEdge(id="city_gate", project_id="p1", from_node_id="city", to_node_id="gate", edge_type="road", travel_time=5),
        MapEdge(id="gate_inner", project_id="p1", from_node_id="gate", to_node_id="inner", edge_type="road", travel_time=5),
    ]
    visible_edges = [edge for edge in objective_edges if edge.id != "secret"]
    map_context = {
        "chapter_travel_time_budget": 1,
        "active_locations": [{"entity_id": "char_mc", "entity_name": "陆沉", "location_id": "city"}],
        "review_graph": {
            "available": True,
            "map_nodes": [node.model_dump(mode="json") for node in nodes],
            "map_edges": [edge.model_dump(mode="json") for edge in visible_edges],
        },
        "objective_review_graph": {
            "available": True,
            "map_nodes": [node.model_dump(mode="json") for node in nodes],
            "map_edges": [edge.model_dump(mode="json") for edge in objective_edges],
        },
        "observer_cognition": {
            "character:char_mc": {
                "id": "cog",
                "project_id": "p1",
                "observer_type": "character",
                "observer_id": "char_mc",
                "hidden_refs": ["map_edge:secret"],
            }
        },
    }
    context = ReviewContextPack(
        project_id="p1",
        project_title="书",
        chapter_number=1,
        chapter_plan_title="潜入",
        chapter_plan_one_line="主角抵达内殿。",
        map_context=map_context,
    )
    output = WriterOutput(
        project_id="p1",
        chapter_number=1,
        title="潜入",
        body="陆沉从城中抵达内殿。",
        end_of_chapter_summary="陆沉抵达内殿。",
        scene_outputs=[
            SceneOutput(scene_no=1, scene_objective="出发", scene_location_id="city", involved_entities=["char_mc"], text="出发。"),
            SceneOutput(scene_no=2, scene_objective="抵达", scene_location_id="inner", involved_entities=["char_mc"], text="抵达。"),
        ],
        time_advance=TimeAdvance(new_time_label="片刻后", duration_description="片刻后"),
    )

    verdict = WebNovelExperienceReviewer(llm_enabled=False).review(context, output)

    issue_names = {issue.rule_name for issue in verdict.issues}
    assert "map_known_travel_time_exceeds_chapter_time" in issue_names


def test_reviewer_flags_unmet_movement_access_rule() -> None:
    nodes = [
        MapNode(id="city", project_id="p1", node_type="settlement", name="城"),
        MapNode(id="inner", project_id="p1", node_type="site", name="内殿"),
    ]
    edges = [
        MapEdge(
            id="restricted_gate",
            project_id="p1",
            from_node_id="city",
            to_node_id="inner",
            edge_type="world_gate",
            bidirectional=True,
            travel_time=0.5,
            access_rule_id="inner_token",
        )
    ]
    map_context = {
        "chapter_travel_time_budget": 1,
        "movement_policy": {"allowed_access_rule_ids": []},
        "review_graph": {
            "available": True,
            "map_nodes": [node.model_dump(mode="json") for node in nodes],
            "map_edges": [edge.model_dump(mode="json") for edge in edges],
        },
    }
    context = ReviewContextPack(
        project_id="p1",
        project_title="书",
        chapter_number=1,
        chapter_plan_title="入殿",
        chapter_plan_one_line="主角进入内殿。",
        map_context=map_context,
    )
    output = WriterOutput(
        project_id="p1",
        chapter_number=1,
        title="入殿",
        body="主角从城中进入内殿。",
        end_of_chapter_summary="主角抵达内殿。",
        scene_outputs=[
            SceneOutput(scene_no=1, scene_objective="出发", scene_location_id="city", text="出发。"),
            SceneOutput(scene_no=2, scene_objective="抵达", scene_location_id="inner", text="抵达。"),
        ],
        time_advance=TimeAdvance(new_time_label="片刻后", duration_description="片刻后"),
    )

    verdict = WebNovelExperienceReviewer(llm_enabled=False).review(context, output)

    assert any(issue.rule_name == "map_access_rule_unmet" for issue in verdict.issues)


def test_reviewer_applies_movement_policy_speed_multiplier() -> None:
    nodes = [
        MapNode(id="city", project_id="p1", node_type="settlement", name="城"),
        MapNode(id="inner", project_id="p1", node_type="site", name="内殿"),
    ]
    edge = MapEdge(
        id="long_road",
        project_id="p1",
        from_node_id="city",
        to_node_id="inner",
        edge_type="road",
        bidirectional=True,
        travel_time=2.0,
    )
    base_map_context = {
        "chapter_travel_time_budget": 1,
        "review_graph": {
            "available": True,
            "map_nodes": [node.model_dump(mode="json") for node in nodes],
            "map_edges": [edge.model_dump(mode="json")],
        },
    }
    output = WriterOutput(
        project_id="p1",
        chapter_number=1,
        title="赶路",
        body="主角从城中抵达内殿。",
        end_of_chapter_summary="主角抵达内殿。",
        scene_outputs=[
            SceneOutput(scene_no=1, scene_objective="出发", scene_location_id="city", text="出发。"),
            SceneOutput(scene_no=2, scene_objective="抵达", scene_location_id="inner", text="抵达。"),
        ],
        time_advance=TimeAdvance(new_time_label="片刻后", duration_description="片刻后"),
    )
    blocked_context = ReviewContextPack(
        project_id="p1",
        project_title="书",
        chapter_number=1,
        chapter_plan_title="赶路",
        chapter_plan_one_line="主角抵达内殿。",
        map_context=base_map_context,
    )
    fast_context = ReviewContextPack(
        project_id="p1",
        project_title="书",
        chapter_number=1,
        chapter_plan_title="赶路",
        chapter_plan_one_line="主角抵达内殿。",
        map_context={**base_map_context, "movement_policy": {"team_speed_multiplier": 3}},
    )

    blocked_verdict = WebNovelExperienceReviewer(llm_enabled=False).review(blocked_context, output)
    fast_verdict = WebNovelExperienceReviewer(llm_enabled=False).review(fast_context, output)

    assert any(issue.rule_name == "map_travel_time_exceeds_chapter_time" for issue in blocked_verdict.issues)
    assert not any(issue.rule_name == "map_travel_time_exceeds_chapter_time" for issue in fast_verdict.issues)


def test_map_context_is_compact_and_filters_hidden_edges() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)
    with Session() as session:
        session.add(Project(id="p1", title="书", premise="premise"))
        session.commit()
        result = create_or_update_subworld_map(session, _spec())
        city = next(node for node in result.map_nodes if node.name == "帝都")
        hidden = result.map_edges[0].model_copy(
            update={
                "id": "hidden_context_edge",
                "from_node_id": city.id,
                "to_node_id": result.map_nodes[-1].id,
                "edge_type": "hidden_route",
                "status": "hidden",
                "discovered_by_default": False,
                "visibility_default": "hidden",
            }
        )
        from forwin.map.repository import MapRepository

        MapRepository(session).upsert_map_edge(hidden)

        context = _build_map_context(
            session,
            "p1",
            [SimpleNamespace(entity_id="char_mc", name="主角", current_state={"location_id": city.id})],
        )

    assert "map_nodes" not in context
    assert "map_edges" not in context
    assert context["review_graph"]["available"] is True
    review_edge_ids = {edge["id"] for edge in context["review_graph"]["map_edges"]}
    assert "hidden_context_edge" not in review_edge_ids
    assert context["active_locations"][0]["nearby_nodes"]


def test_genesis_book_map_feeds_context_and_writer_prompt() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)
    atlas = {
        "overview": "旧城、荒原与地下遗迹构成主舞台。",
        "topology_rules": ["移动必须有路程与风险成本"],
        "submaps": [{"id": "sw-main", "name": "主舞台", "scope": "macro_region", "key_locations": ["焚化塔"]}],
        "regions": [{"id": "region-city", "name": "旧城", "subworld_name": "主舞台", "level": 1}],
        "nodes": [
            {
                "id": "node-city",
                "name": "雨夜站台",
                "kind": "city",
                "parent_subworld": "sw-main",
                "parent_region_id": "region-city",
                "description": "主角开局地点",
            }
        ],
        "edges": [],
    }
    with Session() as session:
        project = Project(id="p1", title="书", premise="premise", genre="玄幻")
        revision = BookGenesisRevision(
            id="rev1",
            project_id="p1",
            revision=1,
            status="locked",
            pack_json=json.dumps(
                {
                    "world": {
                        "world_bible": {"overview": "旧城与禁术并存。"},
                        "map_atlas": atlas,
                        "story_engine": {
                            "core_cast": [
                                {"name": "林夜", "current_base": "node-city", "home_location": "node-city"}
                            ],
                            "long_arcs": ["旧术复苏"],
                        },
                    }
                },
                ensure_ascii=False,
            ),
        )
        project.active_genesis_revision_id = revision.id
        session.add(project)
        session.flush()
        arc = ArcPlanVersion(
            id="arc1",
            project_id="p1",
            arc_number=1,
            chapter_start=1,
            chapter_end=1,
            arc_synopsis="旧城开局",
            status="active",
        )
        chapter = ChapterPlan(
            id="chapter1",
            project_id="p1",
            arc_plan_id=arc.id,
            chapter_number=1,
            title="雨夜",
            one_line="林夜从雨夜站台出发。",
            goals_json=json.dumps(["进入旧城冲突"], ensure_ascii=False),
        )
        session.add_all([revision, arc])
        session.flush()
        session.add(chapter)
        session.flush()
        specs = build_subworld_map_specs_from_genesis(
            project_id="p1",
            genesis_revision_id=revision.id,
            map_atlas=atlas,
        )
        map_result = create_or_update_book_map(session, specs)
        city_anchor = next(
            node
            for subworld_result in map_result.subworld_results
            for node in subworld_result.map_nodes
            if node.metadata.get("source_node_id") == "node-city"
        )
        session.commit()

        context = assemble_context(StateRepository(session), "p1", chapter)
        prompt = build_single_chapter_draft_prompt(context)

    assert context.map_context["map_node_count"] > 0
    active_location = next(item for item in context.map_context["active_locations"] if item["entity_name"] == "林夜")
    assert active_location["location_id"] == city_anchor.id
    assert active_location["location_name"] == "雨夜站台"
    assert active_location["source"] == "genesis_story_engine"
    assert "【地图运行时】" in prompt[1]["content"]
    assert "雨夜站台" in prompt[1]["content"]


def create_or_update_subworld_map_for_test():
    return generate_subworld_map(_spec())
