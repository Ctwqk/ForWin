from __future__ import annotations

import hashlib

import pytest

from forwin.map.generator import generate_subworld_map
from forwin.map.genesis_adapter import _stable_seed, build_subworld_map_specs_from_genesis
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
    result = generate_subworld_map(spec)
    assert result.validation_report.valid is True
    anchor_ids = {node.id for node in result.map_nodes if node.metadata.get("node_role") == "anchor"}
    connections = [edge for edge in result.map_edges if edge.metadata.get("required_connection") and edge.status == "open"]
    assert connections
    assert all(edge.edge_type == (edge_type or "road") for edge in connections)
    assert all({edge.from_node_id, edge.to_node_id} == anchor_ids for edge in connections)


def _three_anchor_atlas(**edge_flags) -> dict:
    return {
        "submaps": [{"id": "main", "name": "Main"}],
        "nodes": [
            {"id": name.lower(), "name": name, "parent_subworld": "main"}
            for name in ("A", "B", "C")
        ],
        "edges": [{"id": "ac", "from_node_id": "a", "to_node_id": "c", "kind": "river", **edge_flags}],
    }


def _generate_atlas(atlas, *, density=1.0):
    spec = build_subworld_map_specs_from_genesis(project_id="p1", map_atlas=atlas)[0]
    result = generate_subworld_map(spec.model_copy(update={"target_edge_density": density}))
    ids = {node.metadata["source_node_id"]: node.id for node in result.map_nodes
           if node.metadata.get("source_node_id")}
    connections = [edge for edge in result.map_edges if edge.metadata.get("required_connection")]
    return spec, result, ids, connections


def test_declared_atlas_edges_connect_their_endpoints() -> None:
    spec, result, ids, connections = _generate_atlas(_three_anchor_atlas())

    assert [(edge.from_node_id, edge.to_node_id, edge.edge_type) for edge in connections] == [
        (ids["a"], ids["c"], "river"),
    ]
    assert connections[0].metadata["source_edge_id"] == "ac"
    assert spec.required_connections[0].from_source_node_id == "a"
    assert spec.required_connections[0].to_source_node_id == "c"
    assert result.validation_report.valid
    assert len(result.map_edges) == result.summary["target_edge_count"]


def test_declared_hidden_edge_is_hidden_and_undiscovered() -> None:
    _, result, ids, connections = _generate_atlas(_three_anchor_atlas(hidden=True))

    assert len(connections) == 1
    edge = connections[0]
    assert (edge.from_node_id, edge.to_node_id, edge.edge_type) == (ids["a"], ids["c"], "river")
    assert edge.status == "hidden"
    assert edge.discovered_by_default is False
    assert edge.visibility_default == "hidden"
    # Hidden declarations must not replace the visible spanning tree.
    from forwin.book_state.map_graph import MapGraph
    graph = MapGraph(nodes=result.map_nodes, edges=[edge for edge in result.map_edges if edge.status == "open"])
    assert all(graph.shortest_path(result.map_nodes[0].id, node.id).reachable for node in result.map_nodes)


def test_declared_one_way_edge_is_not_bidirectional() -> None:
    _, _, ids, connections = _generate_atlas(_three_anchor_atlas(bidirectional=False))

    assert len(connections) == 1
    edge = connections[0]
    assert (edge.from_node_id, edge.to_node_id) == (ids["a"], ids["c"])
    assert edge.bidirectional is False


def test_seed_ignores_revision_and_atlas_changes() -> None:
    atlas = _three_anchor_atlas()
    first = build_subworld_map_specs_from_genesis(project_id="p1", genesis_revision_id="rev1", map_atlas=atlas)[0]
    second = build_subworld_map_specs_from_genesis(
        project_id="p1", genesis_revision_id="rev2", map_atlas={**atlas, "overview": "Revised atlas", "edges": []},
    )[0]

    assert first.generation_seed == second.generation_seed
    assert generate_subworld_map(first).map_nodes == generate_subworld_map(second).map_nodes
    other_project = build_subworld_map_specs_from_genesis(project_id="p2", map_atlas=atlas)[0]
    other_world = build_subworld_map_specs_from_genesis(project_id="p1", map_atlas={"submaps": [{"id": "other"}]})[0]
    assert first.generation_seed != other_project.generation_seed
    assert first.generation_seed != other_world.generation_seed


def test_stable_seed_uses_exact_sha256_formula() -> None:
    expected = int(hashlib.sha256(b"p1|main").hexdigest()[:12], 16) % 2_000_000_000
    assert _stable_seed("p1", "main") == expected
    spec = build_subworld_map_specs_from_genesis(project_id="p1", map_atlas=_three_anchor_atlas())[0]
    assert spec.generation_seed == expected


def test_subworld_without_declared_edges_keeps_anchor_chain() -> None:
    _, _, ids, connections = _generate_atlas({**_three_anchor_atlas(), "edges": []})

    assert {(edge.from_node_id, edge.to_node_id) for edge in connections} == {
        (ids["a"], ids["b"]), (ids["b"], ids["c"]),
    }
    assert all(edge.bidirectional and edge.edge_type == "road" for edge in connections)


def test_declared_cycles_survive_while_duplicates_do_not_use_the_budget() -> None:
    atlas = _three_anchor_atlas()
    atlas["edges"] = [
        {"id": str(index), "from_node_id": left, "to_node_id": right, "kind": "river"}
        for index, (left, right) in enumerate([("a", "b"), ("b", "c"), ("c", "a")] * 5)
    ]
    _, result, _, connections = _generate_atlas(atlas)

    assert len(connections) == 3
    assert [edge.metadata["source_edge_id"] for edge in connections] == ["0", "1", "2"]
    assert result.validation_report.valid
    assert len(result.map_edges) == result.summary["target_edge_count"]


def test_self_loop_and_duplicate_declarations_produce_one_edge() -> None:
    atlas = _three_anchor_atlas()
    atlas["edges"] = [
        {"id": edge_id, "from_node_id": left, "to_node_id": right, "kind": "river"}
        for edge_id, left, right in (
            ("loop", "a", "a"), ("first", "a", "b"),
            ("duplicate", "a", "b"), ("reverse", "b", "a"),
        )
    ]

    spec, result, ids, connections = _generate_atlas(atlas)

    assert [edge.source_edge_id for edge in spec.required_connections] == ["first"]
    assert [(edge.from_node_id, edge.to_node_id) for edge in connections] == [(ids["a"], ids["b"])]
    assert connections[0].metadata["source_edge_id"] == "first"
    assert all(edge.from_node_id != edge.to_node_id for edge in result.map_edges)
    assert result.validation_report.valid
    assert len(result.map_edges) == result.summary["target_edge_count"]


def test_declaration_deduplication_preserves_direction_type_and_hidden_flag() -> None:
    atlas = _three_anchor_atlas()
    atlas["edges"] = [
        {"id": "both", "from_node_id": "a", "to_node_id": "b", "kind": "river"},
        {"id": "forward", "from_node_id": "a", "to_node_id": "b", "kind": "river", "bidirectional": False},
        {"id": "reverse", "from_node_id": "b", "to_node_id": "a", "kind": "river", "bidirectional": False},
        {"id": "road", "from_node_id": "a", "to_node_id": "b", "kind": "road"},
        {"id": "hidden", "from_node_id": "a", "to_node_id": "b", "kind": "river", "hidden": True},
        {"id": "duplicate", "from_node_id": "a", "to_node_id": "b", "kind": "river", "bidirectional": False},
    ]

    spec, result, _, connections = _generate_atlas(atlas)

    expected_ids = ["both", "forward", "reverse", "road", "hidden"]
    assert [edge.source_edge_id for edge in spec.required_connections] == expected_ids
    assert [edge.metadata["source_edge_id"] for edge in connections] == expected_ids
    assert result.validation_report.valid


@pytest.mark.parametrize("endpoint", ["from", "to"])
def test_contradictory_subworld_and_node_keys_skip_declared_connection(endpoint: str) -> None:
    atlas = _three_anchor_atlas()
    atlas["submaps"].append({"id": "other", "name": "Other"})
    atlas["nodes"].append({"id": "outside", "name": "Outside", "parent_subworld": "other"})
    atlas["edges"][0].update({f"{endpoint}_subworld_id": "main", f"{endpoint}_node_id": "outside"})

    specs = build_subworld_map_specs_from_genesis(project_id="p1", map_atlas=atlas)

    assert all(spec.required_connections == [] for spec in specs)
    for spec in specs:
        result = generate_subworld_map(spec)
        assert result.validation_report.valid
        assert all(edge.metadata.get("source_edge_id") != "ac" for edge in result.map_edges)


@pytest.mark.parametrize(
    ("bidirectional", "hidden", "expected_direction", "expected_hidden"),
    [(None, None, True, False), ("false", "secret", True, False),
     (0, 1, True, False), (False, True, False, True), (True, False, True, False)],
)
def test_raw_atlas_edge_flags_are_coerced_defensively(
    bidirectional, hidden, expected_direction: bool, expected_hidden: bool,
) -> None:
    spec, result, _, connections = _generate_atlas(_three_anchor_atlas(bidirectional=bidirectional, hidden=hidden))

    (connection,) = spec.required_connections
    assert connection.bidirectional is expected_direction
    assert connection.hidden is expected_hidden
    (edge,) = connections
    assert edge.bidirectional is expected_direction
    assert edge.status == ("hidden" if expected_hidden else "open")
    assert edge.discovered_by_default is not expected_hidden
    assert result.validation_report.valid


def test_invalid_declared_edge_type_is_skipped() -> None:
    atlas = _three_anchor_atlas()
    atlas["edges"].append({"id": "invalid", "from_node_id": "a", "to_node_id": "b", "edge_type": "secret"})

    spec, result, _, connections = _generate_atlas(atlas)

    assert [edge.source_edge_id for edge in spec.required_connections] == ["ac"]
    assert [edge.metadata["source_edge_id"] for edge in connections] == ["ac"]
    assert result.validation_report.valid


def test_declared_endpoint_ids_take_precedence_over_node_names() -> None:
    atlas = _three_anchor_atlas()
    atlas["nodes"][1]["name"] = "a"
    _, _, ids, connections = _generate_atlas(atlas)

    assert [(edge.from_node_id, edge.to_node_id) for edge in connections] == [(ids["a"], ids["c"])]


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
    assert main.required_connections == []
    assert [(connection.from_source_node_id, connection.to_source_node_id) for connection in other.required_connections] == [("b", "c")]


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
    assert all(spec.required_connections == [] for spec in specs)


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
    if "subworld" not in source_key:
        assert [(edge.from_source_node_id, edge.to_source_node_id) for edge in main.required_connections] == [("a", "b")]
    else:
        assert main.required_connections == []
    assert other.required_connections == []


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
