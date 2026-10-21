from __future__ import annotations

import pytest

from forwin.map.generator import generate_subworld_map
from forwin.map.genesis_adapter import build_subworld_map_specs_from_genesis
from forwin.models.subworld import project_scoped_subworld_id


def test_overview_only_map_atlas_builds_valid_default_spec() -> None:
    specs = build_subworld_map_specs_from_genesis(
        project_id="p1",
        genesis_revision_id="rev1",
        map_atlas={"overview": "旧城、城外荒原、地下遗迹。"},
    )

    assert len(specs) == 1
    assert specs[0].subworld_id == project_scoped_subworld_id(
        "p1",
        "subworld-main-stage",
    )
    assert specs[0].logical_subworld_id == "subworld-main-stage"
    assert specs[0].target_region_count >= 3
    assert specs[0].target_node_count >= specs[0].target_region_count * 3

    result = generate_subworld_map(specs[0])
    assert result.validation_report.valid is True
    assert result.map_nodes


def test_default_submap_for_a_pack_without_submaps_carries_no_story_name() -> None:
    for atlas in ({}, {"overview": "只有概述。"}, {"submaps": []}):
        specs = build_subworld_map_specs_from_genesis(
            project_id="p1",
            genesis_revision_id="rev1",
            map_atlas=atlas,
        )

        assert len(specs) == 1
        assert specs[0].name == "未命名小世界"
        assert "主舞台" not in specs[0].name


def test_genesis_adapter_preserves_source_ids_and_seed_stability() -> None:
    atlas = {
        "overview": "旧城与遗迹组成主舞台。",
        "topology_rules": ["移动必须有路程代价"],
        "submaps": [
            {
                "id": "sw-main",
                "name": "主舞台",
                "scope": "macro_region",
                "terrain": ["plain", "ruin_zone"],
                "key_locations": ["焚化塔"],
            }
        ],
        "regions": [
            {"id": "region-city", "name": "旧城", "subworld_name": "主舞台", "level": 1},
            {"id": "region-ruin", "name": "地下遗迹", "subworld_name": "主舞台", "level": 1},
        ],
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

    first = build_subworld_map_specs_from_genesis(
        project_id="p1",
        genesis_revision_id="rev1",
        map_atlas=atlas,
    )[0]
    second = build_subworld_map_specs_from_genesis(
        project_id="p1",
        genesis_revision_id="rev1",
        map_atlas=atlas,
    )[0]

    assert first.generation_seed == second.generation_seed
    assert first.required_anchor_nodes[0].source_node_id == "node-city"
    assert first.required_anchor_nodes[0].source_region_id == "region-city"
    assert first.required_anchor_nodes[0].source_subworld_id == "sw-main"

    result = generate_subworld_map(first)
    city_anchor = next(node for node in result.map_nodes if node.name == "雨夜站台")
    assert city_anchor.metadata["source_node_id"] == "node-city"
    assert city_anchor.metadata["source_region_id"] == "region-city"


@pytest.mark.parametrize("edge_type", ["portal", "mountain_pass", "rail", None])
def test_connection_roles_come_from_atlas_edge_types(edge_type: str | None) -> None:
    atlas = {
        "topology_rules": ["传送门、山路与水路"],
        "submaps": [{"id": "main", "name": "World", "travel_rules": ["传送门"]}],
        "nodes": [
            {"id": "a", "name": "A", "parent_subworld": "main"},
            {"id": "b", "name": "B", "parent_subworld": "main"},
        ],
        "edges": [{"from_node_id": "a", "to_node_id": "b", "kind": edge_type}] if edge_type else [],
    }

    spec = build_subworld_map_specs_from_genesis(project_id="p1", map_atlas=atlas)[0]

    assert spec.required_connection_roles == [edge_type or "road"]
    # Include the optional anchor connection alongside the baseline routes.
    result = generate_subworld_map(spec.model_copy(update={"target_edge_density": 3.0}))
    assert result.validation_report.valid is True
    anchor_ids = {node.id for node in result.map_nodes if node.metadata.get("node_role") == "anchor"}
    connections = [edge for edge in result.map_edges if edge.metadata.get("required_connection") and edge.status == "open"]
    assert connections
    assert all(edge.edge_type == (edge_type or "road") for edge in connections)
    assert all({edge.from_node_id, edge.to_node_id} == anchor_ids for edge in connections)


def test_connection_roles_are_scoped_ordered_and_deduplicated() -> None:
    atlas = {
        "submaps": [{"id": "main", "name": "Main"}, {"id": "other", "name": "Other"}],
        "nodes": [
            {"id": "a", "name": "A", "parent_subworld": "main"},
            {"id": "b", "name": "B", "parent_subworld": "other"},
            {"id": "c", "name": "C", "parent_subworld": "other"},
        ],
        "edges": [
            {"source": "b", "target": "c", "kind": "rail"},
            {"from": "a", "to": "b", "edge_type": "portal"},
            {"source_node_id": "a", "target_node_id": "a", "edge_type": "mountain_pass"},
            {"from_node_id": "a", "to_node_id": "a", "kind": "portal"},
            {"from_node_id": "a", "to_node_id": "a", "kind": "mountain_pass"},
            {"from_node_id": "a", "to_node_id": "a", "kind": "传送阵"},
        ],
    }

    main, other = build_subworld_map_specs_from_genesis(project_id="p1", map_atlas=atlas)

    assert main.required_connection_roles == ["mountain_pass", "portal"]
    assert other.required_connection_roles == ["rail"]


@pytest.mark.parametrize("seed", range(5))
def test_cross_subworld_world_gate_does_not_define_internal_routes(seed: int) -> None:
    atlas = {
        "submaps": [{"id": "main", "name": "Main"}, {"id": "other", "name": "Other"}],
        "nodes": [
            {"id": "a", "name": "A", "parent_subworld": "main"},
            {"id": "b", "name": "B", "parent_subworld": "main"},
            {"id": "c", "name": "C", "parent_subworld": "other"},
        ],
        "edges": [
            {"from_node_id": "a", "to_node_id": "b", "kind": "road"},
            {"from_node_id": "a", "to_node_id": "c", "kind": "world_gate"},
        ],
    }

    specs = build_subworld_map_specs_from_genesis(project_id="p1", map_atlas=atlas)

    assert [spec.required_connection_roles for spec in specs] == [["road"], ["road"]]
    for spec in specs:
        result = generate_subworld_map(spec.model_copy(update={"generation_seed": seed, "target_edge_density": 3.0}))
        assert result.validation_report.valid is True
        assert all(edge.edge_type != "world_gate" for edge in result.map_edges)
        if spec.logical_subworld_id == "main":
            anchor_ids = {node.id for node in result.map_nodes if node.metadata.get("node_role") == "anchor"}
            connections = [edge for edge in result.map_edges if edge.metadata.get("required_connection") and edge.status == "open"]
            assert connections
            assert all(edge.edge_type == "road" for edge in connections)
            assert all({edge.from_node_id, edge.to_node_id} == anchor_ids for edge in connections)


@pytest.mark.parametrize("single_subworld", [False, True])
@pytest.mark.parametrize(
    "edge",
    [
        {"from_node_id": "a", "to_node_id": "missing"},
        {"from_node_id": "missing", "to_node_id": "a"},
        {"from_node_id": "a", "to_node_id": "unresolved"},
        {"from_node_id": "a"},
        {"to_node_id": "a"},
        {},
    ],
)
def test_connection_roles_skip_unresolvable_endpoints(single_subworld: bool, edge: dict[str, str]) -> None:
    submaps = [{"id": "main", "name": "Main"}]
    if not single_subworld:
        submaps.append({"id": "other", "name": "Other"})
    atlas = {
        "submaps": submaps,
        "nodes": [
            {"id": "a", "name": "A", "parent_subworld": "main"},
            {"id": "unresolved", "name": "Unresolved", "parent_subworld": "missing"},
        ],
        "edges": [{**edge, "kind": "portal"}],
    }

    specs = build_subworld_map_specs_from_genesis(project_id="p1", map_atlas=atlas)

    assert all(spec.required_connection_roles == ["road"] for spec in specs)


@pytest.mark.parametrize(
    ("source_key", "target_key", "source_ref", "target_ref", "other_ref"),
    [
        ("from_node", "to_node", "A", "B", "C"),
        ("source_node", "target_node", "a", "b", "c"),
        ("from_subworld_id", "to_subworld_id", "main", "main", "other"),
        ("source_subworld_id", "target_subworld_id", "main", "main", "other"),
        ("from_subworld", "to_subworld", "Main", "Main", "Other"),
        ("source_subworld", "target_subworld", "Main", "Main", "Other"),
    ],
)
def test_connection_roles_resolve_endpoint_aliases(
    source_key: str, target_key: str, source_ref: str, target_ref: str, other_ref: str,
) -> None:
    atlas = {
        "submaps": [{"id": "main", "name": "Main"}, {"id": "other", "name": "Other"}],
        "nodes": [
            {"id": "a", "name": "A", "parent_subworld": "main"},
            {"id": "b", "name": "B", "parent_subworld": "main"},
            {"id": "c", "name": "C", "parent_subworld": "other"},
        ],
        "edges": [
            {source_key: source_ref, target_key: target_ref, "kind": "rail"},
            {source_key: source_ref, target_key: other_ref, "kind": "portal"},
        ],
    }

    main, other = build_subworld_map_specs_from_genesis(project_id="p1", map_atlas=atlas)

    assert main.required_connection_roles == ["rail"]
    assert other.required_connection_roles == ["road"]


def test_region_kind_flows_into_region_type() -> None:
    atlas = {
        "regions": [
            {"id": "r1", "name": "宗门山门", "kind": "district", "terrain": "plain"},
            {"id": "r2", "name": "港口"},
            {"id": "r3", "kind": "city_cluster"},
        ],
    }

    spec = build_subworld_map_specs_from_genesis(project_id="p1", map_atlas=atlas)[0]
    result = generate_subworld_map(spec)

    assert spec.region_kinds == {"宗门山门": "district", "地区3": "city_cluster"}
    assert [region.region_type for region in result.regions] == ["district", "region", "city_cluster"]
    assert all(region.terrain == "plain" for region in result.regions)


@pytest.mark.parametrize(
    ("scope", "expected"),
    [("macro_region", "continent"), ("", "continent"), ("realm", "realm"),
     ("plane", "plane"), ("underground", "underground"), ("dream_realm", "continent")],
)
def test_subworld_type_comes_only_from_scope(scope: str, expected: str) -> None:
    spec = build_subworld_map_specs_from_genesis(
        project_id="p1",
        map_atlas={"submaps": [{"id": "main", "name": "上古秘境界地下", "scope": scope}]},
    )[0]

    assert spec.subworld_type == expected
    assert spec.required_region_roles == ["区域1", "区域2", "区域3"]


@pytest.mark.parametrize("danger_level, expected", [(0, 0.0), (4, 4.0), (1.25, 1.25), ("4", 2.0), (None, 2.0), (True, 2.0)])
def test_danger_base_comes_only_from_numeric_danger_level(danger_level, expected: float) -> None:
    spec = build_subworld_map_specs_from_genesis(
        project_id="p1",
        map_atlas={
            "submaps": [{"id": "main", "name": "禁魔战荒遗迹", "danger_level": danger_level}],
            "regions": [{"name": "危险", "summary": "禁区"}],
            "nodes": [{"id": "a", "name": "A", "danger": "危险"}],
        },
    )[0]

    assert spec.danger_profile["base"] == expected
