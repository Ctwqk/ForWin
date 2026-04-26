from __future__ import annotations

from pathlib import Path

from forwin.map.generator import generate_subworld_map
from forwin.map.pathfinding import MapGraph
from forwin.map.protocol import SCHEME_C_NAME, MapAnchorNodeSpec, SubWorldMapSpec


def _spec(seed: int = 123456) -> SubWorldMapSpec:
    return SubWorldMapSpec(
        project_id="p1",
        subworld_id="sw_canglan",
        name="苍澜大陆",
        subworld_type="continent",
        culture_tags=["中华玄幻"],
        terrain_tags=["mountain", "river", "borderland"],
        target_region_count=5,
        target_node_count=20,
        target_edge_density=1.6,
        required_region_roles=["帝国核心区", "宗门山脉", "边境战线"],
        required_anchor_nodes=[
            MapAnchorNodeSpec(
                name="帝都",
                node_type="settlement",
                region_role="帝国核心区",
                narrative_function="权力中心",
            ),
            MapAnchorNodeSpec(
                name="青云山门",
                node_type="settlement",
                region_role="宗门山脉",
                narrative_function="主角成长地",
            ),
        ],
        required_connection_roles=["官道", "山路", "传送阵"],
        danger_profile={"base": 2},
        generation_seed=seed,
    )


def test_scheme_c_name_is_documented_in_code_and_tests() -> None:
    generator_source = Path("forwin/map/generator.py").read_text(encoding="utf-8")

    assert SCHEME_C_NAME == "方案 C：Graph-based Weighted Map Generation"
    assert SCHEME_C_NAME in generator_source


def test_scheme_c_seed_stability_and_seed_variance() -> None:
    first = generate_subworld_map(_spec(7))
    second = generate_subworld_map(_spec(7))
    third = generate_subworld_map(_spec(8))

    assert [node.id for node in first.map_nodes] == [node.id for node in second.map_nodes]
    assert [edge.id for edge in first.map_edges] == [edge.id for edge in second.map_edges]
    assert [node.id for node in first.map_nodes] != [node.id for node in third.map_nodes]


def test_scheme_c_mst_reachability_anchors_and_extra_edges() -> None:
    result = generate_subworld_map(_spec(9))

    assert result.validation_report.valid is True
    assert len(result.regions) == 5
    assert len(result.map_nodes) == 20
    assert len(result.map_edges) >= round(20 * 1.6)
    assert len(result.map_edges) > len(result.map_nodes) - 1
    anchor_names = {node.name for node in result.map_nodes if node.metadata.get("node_role") == "anchor"}
    assert {"帝都", "青云山门"}.issubset(anchor_names)

    graph = MapGraph(nodes=result.map_nodes, edges=result.map_edges)
    entry_id = result.regions[0].entry_node_ids[0]
    for anchor_name in ("帝都", "青云山门"):
        anchor_id = next(node.id for node in result.map_nodes if node.name == anchor_name)
        assert graph.shortest_path(entry_id, anchor_id).reachable is True
    assert all(edge.distance >= 0 and edge.travel_time >= 0 for edge in result.map_edges)
