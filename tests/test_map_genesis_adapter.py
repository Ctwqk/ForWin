from __future__ import annotations

from forwin.map.generator import generate_subworld_map
from forwin.map.genesis_adapter import build_subworld_map_specs_from_genesis


def test_overview_only_map_atlas_builds_valid_default_spec() -> None:
    specs = build_subworld_map_specs_from_genesis(
        project_id="p1",
        genesis_revision_id="rev1",
        map_atlas={"overview": "旧城、城外荒原、地下遗迹。"},
    )

    assert len(specs) == 1
    assert specs[0].subworld_id == "subworld-main-stage"
    assert specs[0].target_region_count >= 3
    assert specs[0].target_node_count >= specs[0].target_region_count * 3

    result = generate_subworld_map(specs[0])
    assert result.validation_report.valid is True
    assert result.map_nodes


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
