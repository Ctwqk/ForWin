from __future__ import annotations

import pytest

from forwin.genesis.normalize import _normalize_map_payload
from forwin.map import genesis_adapter
from forwin.protocol.book_state import MapEdgeType


def normalize(payload):
    return _normalize_map_payload(None, payload=payload, fallback={})


def test_map_normalizer_structures_edges_scope_and_danger():
    result = normalize({
        "submaps": [{"id": "main", "scope": "arbitrary", "danger_level": "4"}],
        "nodes": [
            {"id": "a", "name": "A", "danger_level": 0},
            {"id": "b", "name": "B", "danger_level": 6},
        ],
        "edges": [
            {"from_node_id": "a", "to_node_id": "missing", "kind": "road"},
            {"id": "old", "from_node_id": "a", "to_node_id": "b", "kind": "portal", "bidirectional": False, "hidden": True},
            {"from_node_id": "b", "to_node_id": "a", "kind": "invalid"},
            {"from_node_id": "missing", "to_node_id": "b"},
        ],
    })
    assert result["submaps"][0]["scope"] == "continent"
    assert result["submaps"][0]["danger_level"] == 4
    assert [node["danger_level"] for node in result["nodes"]] == [0, None]
    assert result["edges"] == [
        {"id": "edge-1", "from_node_id": "a", "to_node_id": "b", "kind": "portal", "bidirectional": False, "hidden": True},
        {"id": "edge-2", "from_node_id": "b", "to_node_id": "a", "kind": "road", "bidirectional": True, "hidden": False},
    ]
    assert normalize(result) == result


@pytest.mark.parametrize("scope", ["continent", "realm", "plane", "underground"])
def test_map_scopes_are_preserved(scope):
    assert normalize({"submaps": [{"scope": scope}]})["submaps"][0]["scope"] == scope


@pytest.mark.parametrize("value,expected", [
    (0, 0), (5, 5), ("3", 3), (2.0, 2), (None, None), ("", None),
    (-1, None), (6, None), ("bad", None), (2.5, None), (True, None),
    (float("nan"), None), (float("inf"), None), ({}, None),
])
def test_map_danger_levels_are_optional_integers(value, expected):
    result = normalize({"submaps": [{"danger_level": value}], "nodes": [{"danger_level": value}, {}]})
    for row in (result["submaps"][0], result["nodes"][0]):
        assert row["danger_level"] == expected
        assert row["danger_level"] is None or type(row["danger_level"]) is int
    assert result["nodes"][1]["danger_level"] is None


@pytest.mark.parametrize("kind", [kind.value for kind in MapEdgeType])
def test_map_edge_kinds_reach_adapter_connection_roles(kind):
    from forwin.book_state.map_graph import MapGraph
    from forwin.map.generator import generate_subworld_map

    atlas = normalize({
        "submaps": [{"id": "main", "name": "Main"}],
        "nodes": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}],
        "edges": [{"from_node_id": "a", "to_node_id": "b", "kind": kind}],
    })
    assert atlas["edges"][0]["kind"] == kind
    specs = genesis_adapter.build_subworld_map_specs_from_genesis(project_id="p", map_atlas=atlas)
    assert specs[0].required_connection_roles == [kind]
    # Include every candidate so edge-budget selection cannot mask type propagation.
    generated = generate_subworld_map(specs[0].model_copy(update={"target_edge_density": 10.0}))
    connections = [edge for edge in generated.map_edges
                   if edge.metadata.get("required_connection") and edge.status == "open"]
    assert connections
    assert all(edge.edge_type == kind for edge in connections)
    ids = {node.metadata["source_node_id"]: node.id for node in generated.map_nodes
           if node.metadata.get("source_node_id")}
    graph = MapGraph(nodes=generated.map_nodes, edges=connections)
    assert graph.shortest_path(ids["a"], ids["b"], allowed_edge_types=frozenset({kind})).reachable
    excluded_kind = "portal" if kind == "road" else "road"
    assert not graph.shortest_path(ids["a"], ids["b"], allowed_edge_types=frozenset({excluded_kind})).reachable


def test_map_edge_legacy_keys_and_boolean_strings():
    result = normalize({
        "nodes": [{"id": "a"}, {"id": "b"}],
        "edges": [{"from": "a", "to": "b", "edge_type": "rail", "bidirectional": "false", "hidden": "true"}],
    })
    assert result["edges"] == [
        {"id": "edge-1", "from_node_id": "a", "to_node_id": "b", "kind": "rail", "bidirectional": False, "hidden": True},
    ]


@pytest.mark.parametrize("from_key,to_key", [
    ("from_node_id", "to_node_id"), ("source_node_id", "target_node_id"),
    ("from_node", "to_node"), ("source_node", "target_node"),
    ("from", "to"), ("source", "target"),
])
def test_map_edge_name_endpoints_resolve_to_unique_node_ids(from_key, to_key):
    atlas = normalize({
        "nodes": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}],
        "edges": [{from_key: "A", to_key: "B", "kind": "river"}],
    })
    assert atlas["edges"] == [{
        "id": "edge-1", "from_node_id": "a", "to_node_id": "b",
        "kind": "river", "bidirectional": True, "hidden": False,
    }]
    assert normalize(atlas) == atlas


@pytest.mark.parametrize("from_key,to_key", [
    ("from_subworld", "to_subworld"), ("from_subworld_id", "to_subworld_id"),
    ("source_subworld", "target_subworld"), ("source_subworld_id", "target_subworld_id"),
])
def test_map_edge_subworld_refs_are_kept_and_build_interconnections(from_key, to_key):
    from forwin.map.service import build_interconnections_from_genesis_atlas

    atlas = normalize({
        "submaps": [{"id": "main", "name": "Main"}, {"id": "other", "name": "Other"}],
        "edges": [{from_key: "main", to_key: "Other", "kind": "rail", "hidden": True}],
    })
    assert atlas["edges"] == [{
        "id": "edge-1", from_key: "main", to_key: "Other", "kind": "rail",
        "bidirectional": True, "hidden": True,
    }]
    assert normalize(atlas) == atlas
    specs = genesis_adapter.build_subworld_map_specs_from_genesis(project_id="p", map_atlas=atlas)
    connections, source = build_interconnections_from_genesis_atlas(project_id="p", specs=specs, map_atlas=atlas)
    assert source == "atlas_edges"
    assert len(connections) == 1
    assert connections[0].from_subworld_id == specs[0].subworld_id
    assert connections[0].to_subworld_id == specs[1].subworld_id
    assert connections[0].metadata["source_edge_type"] == "rail"
    assert connections[0].hidden is True


@pytest.mark.parametrize("flags", [{"status": "hidden"}, {"visibility": "secret"},
                                  {"status": "open", "visibility": "secret", "hidden": False}])
def test_map_edge_legacy_hidden_flags(flags):
    atlas = normalize({
        "nodes": [{"id": "a"}, {"id": "b"}],
        "edges": [{"from": "a", "to": "b", **flags}],
    })
    assert atlas["edges"][0]["hidden"] is True


def test_map_edge_drops_log_index_and_reason_and_names_match_exactly(caplog):
    atlas = normalize({
        "nodes": [{"id": "a", "name": "Same"}, {"id": "b", "name": "Same"},
                  {"id": "c", "name": "Unique"}],
        "edges": [None, {"from": "Same", "to": "c"},
                  {"from": "a", "to": "unique"}, {"from": "a", "to": "missing"},
                  {"from": "a", "to": "Unique"}],
    })
    assert len(atlas["edges"]) == 1
    assert atlas["edges"][0]["to_node_id"] == "c"
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 4
    for index, record in enumerate(warnings, start=1):
        assert f"edge {index}" in record.message
    assert "object" in warnings[0].message
    assert "ambiguous" in warnings[1].message
    assert "unresolved" in warnings[2].message
    assert "unresolved" in warnings[3].message


def test_map_kind_and_scope_are_case_insensitive():
    atlas = normalize({
        "submaps": [{"scope": " Realm "}],
        "nodes": [{"id": "a"}, {"id": "b"}],
        "edges": [{"from": "a", "to": "b", "kind": " River "}],
    })
    assert atlas["submaps"][0]["scope"] == "realm"
    assert atlas["edges"][0]["kind"] == "river"
    assert genesis_adapter._subworld_type({"scope": " Realm "}) == "realm"


def test_map_scope_defaults_and_editor_match_shared_vocabulary():
    import re
    from pathlib import Path

    from forwin.genesis.fallbacks import _fallback_map
    from forwin.genesis.messages import _map_output_requirement
    from forwin.protocol.book_state import MAP_SUBMAP_SCOPES

    assert MAP_SUBMAP_SCOPES == ("continent", "realm", "plane", "underground")
    assert _fallback_map({})["submaps"][0]["scope"] == "continent"
    assert genesis_adapter._normalized_submaps({})[0]["scope"] == "continent"
    assert " / ".join(MAP_SUBMAP_SCOPES) in _map_output_requirement()
    source = (Path(__file__).resolve().parents[1] / "forwin/ui_assets/home/app_state.js").read_text()
    submaps = source.split("collection: 'submaps',", 1)[1].split("collection:", 1)[0]
    assert re.search(r"scope: '([^']+)'", submaps).group(1) == "continent"
    options = submaps.split("path: 'scope',", 1)[1].split("options: [", 1)[1].split("]", 1)[0]
    assert tuple(re.findall(r"value: '([^']+)'", options)) == MAP_SUBMAP_SCOPES


@pytest.mark.parametrize("target_path,wrap_scalar", [("nodes[0]", False), ("overview", True), ("", True)])
def test_map_targeted_refine_omits_whole_atlas_requirement(target_path, wrap_scalar):
    from forwin.genesis.messages import _build_stage_refine_messages, _map_output_requirement

    messages = _build_stage_refine_messages(
        None, pack={}, stage_key="map", instruction="Update", target_path=target_path,
        current_payload={}, support_context={}, fallback_stage_payload={},
        wrap_scalar_value=wrap_scalar,
    )
    assert _map_output_requirement() not in messages[-1]["content"]


@pytest.mark.parametrize("refine", [False, True])
def test_map_prompts_require_structured_fields(refine):
    from forwin.genesis.messages import _build_stage_generation_messages, _build_stage_refine_messages
    from forwin.models.project import Project

    if refine:
        messages = _build_stage_refine_messages(
            None, pack={}, stage_key="map", instruction="Update", target_path="",
            current_payload={}, support_context={}, fallback_stage_payload={},
        )
    else:
        messages = _build_stage_generation_messages(
            None, project=Project(id="p", title="T"), pack={}, stage_key="map", fallback={},
        )
    content = messages[-1]["content"]
    for token in ("continent", "realm", "plane", "underground", "danger_level", "0–5", "null",
                  "edge-{n}", "from_node_id", "to_node_id", "bidirectional", "hidden"):
        assert token in content
    for kind in MapEdgeType:
        assert kind.value in content


def test_normalized_cross_subworld_edges_preserve_existing_bootstrap_semantics():
    from forwin.map.service import build_interconnections_from_genesis_atlas

    atlas = normalize({
        "submaps": [{"id": "main", "name": "Main"}, {"id": "other", "name": "Other"}],
        "nodes": [{"id": "a", "name": "A", "parent_subworld": "main"},
                  {"id": "b", "name": "B", "parent_subworld": "other"}],
        "edges": [{"from_node_id": "a", "to_node_id": "b", "kind": "portal",
                   "bidirectional": False, "hidden": True}],
    })
    specs = genesis_adapter.build_subworld_map_specs_from_genesis(project_id="p", map_atlas=atlas)
    connections, source = build_interconnections_from_genesis_atlas(project_id="p", specs=specs, map_atlas=atlas)
    assert source == "atlas_edges"
    assert len(connections) == 1
    connection = connections[0]
    assert connection.from_subworld_id == specs[0].subworld_id
    assert connection.to_subworld_id == specs[1].subworld_id
    assert connection.edge_type == "world_gate"
    assert connection.hidden is True
    assert connection.bidirectional is False
    assert connection.metadata["source_edge_type"] == "portal"
    assert connection.metadata["source_from_ref"] == "a"
    assert connection.metadata["source_to_ref"] == "b"


@pytest.mark.parametrize("submaps,parents,expected", [
    ([], ["", "other"], {"a"}),
    ([{"id": "main", "name": "Main"}], ["other", "other"], {"a", "b"}),
    ([{"id": "main", "name": "Main"}, {"id": "other", "name": "Other"}], ["Main", "missing"], {"a"}),
    ([{"id": "main", "name": "Main"}, {"id": "other", "name": "Other"}], ["main", "Other"], {"a", "b"}),
])
def test_buildable_node_ids_match_generated_anchor_metadata(submaps, parents, expected):
    from forwin.map.generator import generate_subworld_map

    atlas = {"submaps": submaps, "nodes": [
        {"id": "a", "name": "A", "parent_subworld": parents[0]},
        {"id": "b", "name": "B", "parent_subworld": parents[1]},
        {"id": "nameless", "parent_subworld": parents[0]},
        {"id": "blank", "name": "  ", "parent_subworld": parents[0]},
        {"name": "No ID", "parent_subworld": parents[0]},
    ]}
    refs = genesis_adapter.buildable_atlas_node_ids(atlas)
    assert refs == frozenset(expected)
    assert isinstance(refs, frozenset)
    specs = genesis_adapter.build_subworld_map_specs_from_genesis(project_id="p", map_atlas=atlas)
    actual = {
        node.metadata["source_node_id"]
        for spec in specs for node in generate_subworld_map(spec).map_nodes
        if node.metadata.get("source_node_id")
    }
    assert actual == refs
