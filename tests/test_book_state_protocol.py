from __future__ import annotations

import pytest
from pydantic import ValidationError

from forwin.protocol.book_state import (
    CognitionOverlay,
    EdgePatch,
    GraphDelta,
    MapEdge,
    MapNode,
    NodePatch,
    WorldEdge,
    WorldNode,
)


def test_world_node_and_edge_validate_final_type_sets() -> None:
    node = WorldNode(
        id="char_mc",
        project_id="project-1",
        node_type="character",
        name="陆沉",
        profile={"species": "human"},
        state={"location_id": "loc_blackstone_city"},
    )
    edge = WorldEdge(
        id="edge_mc_ability",
        project_id="project-1",
        source_id="char_mc",
        target_id="ability_sword",
        edge_type="has_ability",
        edge_family="capability_rule",
    )

    assert node.node_type == "character"
    assert edge.edge_type == "has_ability"


def test_world_edge_rejects_family_mismatch() -> None:
    with pytest.raises(ValidationError):
        WorldEdge(
            id="bad_edge",
            project_id="project-1",
            source_id="char_mc",
            target_id="ability_sword",
            edge_type="has_ability",
            edge_family="social",
        )


def test_map_models_and_graph_delta_accept_patch_payloads() -> None:
    map_node = MapNode(
        id="loc_blackstone_city",
        project_id="project-1",
        subworld_id="sw_canglan",
        region_id="region_border",
        node_type="settlement",
        name="黑石城",
    )
    map_edge = MapEdge(
        id="edge_city_gate",
        project_id="project-1",
        subworld_id="sw_canglan",
        from_node_id="loc_blackstone_city",
        to_node_id="loc_forest_gate",
        edge_type="road",
        distance=10.0,
        travel_time=1.0,
    )
    delta = GraphDelta(
        id="delta_ch1",
        project_id="project-1",
        chapter_number=1,
        node_patches=[
            NodePatch(
                node_id="char_mc",
                node_type="character",
                op="set",
                field_path="state.location_id",
                old_value="loc_village",
                new_value="loc_blackstone_city",
            )
        ],
        edge_patches=[
            EdgePatch(
                edge_id="edge_mc_enemy",
                op="create",
                source_id="char_mc",
                target_id="char_enemy",
                edge_type="enemy_of",
                edge_family="social",
                new_value={
                    "project_id": "project-1",
                    "directionality": "symmetric",
                },
            )
        ],
    )

    assert map_node.node_type == "settlement"
    assert map_node.subworld_id == "sw_canglan"
    assert map_edge.edge_type == "road"
    assert map_edge.subworld_id == "sw_canglan"
    assert delta.node_patches[0].field_path == "state.location_id"


def test_cognition_overlay_uses_sparse_refs_and_false_edges() -> None:
    overlay = CognitionOverlay(
        id="cog_mc_ch1",
        project_id="project-1",
        observer_type="character",
        observer_id="char_mc",
        as_of_chapter=1,
        hidden_refs=["fact:fact_li_ming_is_spy"],
        suspected_refs=["map_edge:edge_secret_tunnel"],
        field_overrides={"field:edge_secret_tunnel:status": "blocked"},
        false_edges={
            "edge_false_shortcut": MapEdge(
                id="edge_false_shortcut",
                project_id="project-1",
                from_node_id="loc_a",
                to_node_id="loc_b",
                edge_type="hidden_route",
            )
        },
    )

    assert overlay.hidden_refs == ["fact:fact_li_ming_is_spy"]
    assert "edge_false_shortcut" in overlay.false_edges
