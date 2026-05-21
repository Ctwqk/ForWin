from __future__ import annotations

from forwin.book_state import CognitionView, MapGraph, ObjectiveWorldGraph, distance_between_world_nodes
from forwin.protocol.book_state import (
    CognitionOverlay,
    GraphDelta,
    MapEdge,
    MapNode,
    MapPatch,
    NodePatch,
    WorldNode,
)


def _map_nodes(project_id: str = "project-1") -> list[MapNode]:
    return [
        MapNode(id="loc_city", project_id=project_id, node_type="settlement", name="黑石城"),
        MapNode(id="loc_gate", project_id=project_id, node_type="waypoint", name="黑石林道"),
        MapNode(id="loc_ruin", project_id=project_id, node_type="site", name="上古遗迹"),
        MapNode(id="loc_inner", project_id=project_id, node_type="room", name="遗迹内殿"),
    ]


def test_map_graph_objective_path_can_use_hidden_route_but_observer_path_filters_it() -> None:
    project_id = "project-1"
    edges = [
        MapEdge(
            id="edge_city_gate",
            project_id=project_id,
            from_node_id="loc_city",
            to_node_id="loc_gate",
            edge_type="path",
            distance=100,
            travel_time=5,
        ),
        MapEdge(
            id="edge_gate_inner",
            project_id=project_id,
            from_node_id="loc_gate",
            to_node_id="loc_inner",
            edge_type="path",
            distance=20,
            travel_time=2,
        ),
        MapEdge(
            id="edge_secret_tunnel",
            project_id=project_id,
            from_node_id="loc_city",
            to_node_id="loc_inner",
            edge_type="hidden_route",
            distance=5,
            travel_time=0.5,
            status="hidden",
            discovered_by_default=False,
        ),
    ]
    overlay = CognitionOverlay(
        id="cog_mc",
        project_id=project_id,
        observer_type="character",
        observer_id="char_mc",
        hidden_refs=["map_edge:edge_secret_tunnel"],
    )
    graph = MapGraph(
        nodes=_map_nodes(project_id),
        edges=edges,
        cognition_by_observer={("character", "char_mc"): CognitionView(overlay)},
    )

    objective = graph.shortest_path("loc_city", "loc_inner", metric="travel_time")
    known = graph.shortest_path(
        "loc_city",
        "loc_inner",
        metric="travel_time",
        observer=("character", "char_mc"),
    )

    assert objective.reachable is True
    assert objective.path_edge_ids == ["edge_secret_tunnel"]
    assert objective.total_travel_time == 0.5
    assert known.reachable is True
    assert known.path_edge_ids == ["edge_city_gate", "edge_gate_inner"]
    assert known.total_travel_time == 7


def test_map_graph_expands_bidirectional_edge_at_runtime() -> None:
    graph = MapGraph(
        nodes=_map_nodes(),
        edges=[
            MapEdge(
                id="edge_city_gate",
                project_id="project-1",
                from_node_id="loc_city",
                to_node_id="loc_gate",
                edge_type="road",
                bidirectional=True,
                travel_time=1,
            )
        ],
    )

    reverse = graph.shortest_path("loc_gate", "loc_city")

    assert reverse.reachable is True
    assert reverse.path_edge_ids == ["edge_city_gate__reverse"]


def test_distance_between_world_nodes_uses_location_id_and_map_graph() -> None:
    world = ObjectiveWorldGraph(
        nodes=[
            WorldNode(
                id="char_mc",
                project_id="project-1",
                node_type="character",
                state={"location_id": "loc_city"},
            ),
            WorldNode(
                id="char_enemy",
                project_id="project-1",
                node_type="character",
                state={"location_id": "loc_inner"},
            ),
        ]
    )
    map_graph = MapGraph(
        nodes=_map_nodes(),
        edges=[
            MapEdge(
                id="edge_secret_tunnel",
                project_id="project-1",
                from_node_id="loc_city",
                to_node_id="loc_inner",
                edge_type="hidden_route",
                travel_time=0.5,
                status="hidden",
                discovered_by_default=False,
            )
        ],
    )

    result = distance_between_world_nodes(world, map_graph, "char_mc", "char_enemy")

    assert result.reachable is True
    assert result.total_travel_time == 0.5


def test_distance_between_world_nodes_reports_legacy_location_fallback() -> None:
    world = ObjectiveWorldGraph(
        nodes=[
            WorldNode(
                id="char_mc",
                project_id="project-1",
                node_type="character",
                state={"location": "loc_city"},
            ),
            WorldNode(
                id="char_enemy",
                project_id="project-1",
                node_type="character",
                state={"location_id": "loc_inner"},
            ),
        ]
    )
    map_graph = MapGraph(
        nodes=_map_nodes(),
        edges=[
            MapEdge(
                id="edge_secret_tunnel",
                project_id="project-1",
                from_node_id="loc_city",
                to_node_id="loc_inner",
                edge_type="hidden_route",
                travel_time=0.5,
                status="hidden",
                discovered_by_default=False,
            )
        ],
    )
    facts: list[dict[str, object]] = []

    result = distance_between_world_nodes(
        world,
        map_graph,
        "char_mc",
        "char_enemy",
        legacy_compat_observer=facts.append,
    )

    assert result.reachable is True
    assert facts == [
        {
            "compat_layer": "book_state",
            "compat_feature": "book_state.state.location_fallback",
            "usage_kind": "read_fallback",
            "source_module": "forwin.book_state.runtime",
            "usage_reason": "state.location used because location_id is missing",
            "compat_key": "state.location",
            "legacy_identifier": "loc_city",
            "metadata": {"node_id": "char_mc", "field_path": "state.location"},
        }
    ]


def test_objective_world_graph_applies_node_and_map_patches() -> None:
    world = ObjectiveWorldGraph(
        nodes=[
            WorldNode(
                id="char_mc",
                project_id="project-1",
                node_type="character",
                state={"location_id": "loc_village"},
            )
        ]
    )
    world.apply_delta(
        GraphDelta(
            id="delta_ch1",
            project_id="project-1",
            node_patches=[
                NodePatch(
                    node_id="char_mc",
                    node_type="character",
                    op="set",
                    field_path="state.location_id",
                    new_value="loc_city",
                )
            ],
        )
    )
    graph = MapGraph(
        nodes=_map_nodes(),
        edges=[
            MapEdge(
                id="edge_secret_tunnel",
                project_id="project-1",
                from_node_id="loc_city",
                to_node_id="loc_inner",
                edge_type="hidden_route",
                status="hidden",
            )
        ],
    )
    graph.apply_map_patch(
        MapPatch(
            target_type="map_edge",
            target_id="edge_secret_tunnel",
            op="set",
            field_path="status",
            new_value="open",
        )
    )

    assert world.get_state("char_mc")["location_id"] == "loc_city"
    assert graph.edges_by_id["edge_secret_tunnel"].status == "open"
