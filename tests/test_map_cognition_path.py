from __future__ import annotations

from forwin.book_state.cognition import CognitionView
from forwin.map.pathfinding import MapGraph
from forwin.map.protocol import MapEdge, MapNode
from forwin.protocol.book_state import CognitionOverlay


def _nodes() -> list[MapNode]:
    return [
        MapNode(id="city", project_id="p1", subworld_id="sw1", region_id="r1", node_type="settlement"),
        MapNode(id="gate", project_id="p1", subworld_id="sw1", region_id="r1", node_type="waypoint"),
        MapNode(id="inner", project_id="p1", subworld_id="sw1", region_id="r1", node_type="room"),
        MapNode(id="false_goal", project_id="p1", subworld_id="sw1", region_id="r1", node_type="site"),
    ]


def test_observer_path_filters_hidden_route_and_uses_visible_detour() -> None:
    edges = [
        MapEdge(id="city_gate", project_id="p1", subworld_id="sw1", from_node_id="city", to_node_id="gate", edge_type="road", travel_time=5),
        MapEdge(id="gate_inner", project_id="p1", subworld_id="sw1", from_node_id="gate", to_node_id="inner", edge_type="road", travel_time=5),
        MapEdge(id="secret", project_id="p1", subworld_id="sw1", from_node_id="city", to_node_id="inner", edge_type="hidden_route", status="hidden", discovered_by_default=False, travel_time=1),
    ]
    overlay = CognitionOverlay(
        id="cog",
        project_id="p1",
        observer_type="character",
        observer_id="mc",
        hidden_refs=["map_edge:secret"],
    )
    graph = MapGraph(nodes=_nodes(), edges=edges, cognition_by_observer={("character", "mc"): CognitionView(overlay)})

    objective = graph.shortest_path("city", "inner")
    known = graph.shortest_path("city", "inner", observer=("character", "mc"))

    assert objective.path_edge_ids == ["secret"]
    assert known.path_edge_ids == ["city_gate", "gate_inner"]


def test_field_overrides_and_false_edges_change_observer_path() -> None:
    overlay = CognitionOverlay(
        id="cog",
        project_id="p1",
        observer_type="character",
        observer_id="mc",
        field_overrides={"field:city_gate:status": "blocked"},
        false_edges={
            "false_shortcut": MapEdge(
                id="false_shortcut",
                project_id="p1",
                subworld_id="sw1",
                from_node_id="city",
                to_node_id="false_goal",
                edge_type="road",
                distance=0.2,
                travel_time=0.1,
                travel_cost=3,
                risk_level=4,
                narrative_cost=5,
            )
        },
    )
    graph = MapGraph(
        nodes=_nodes(),
        edges=[
            MapEdge(id="city_gate", project_id="p1", subworld_id="sw1", from_node_id="city", to_node_id="gate", edge_type="road", travel_time=1),
            MapEdge(id="gate_inner", project_id="p1", subworld_id="sw1", from_node_id="gate", to_node_id="inner", edge_type="road", travel_time=1),
        ],
        cognition_by_observer={("character", "mc"): CognitionView(overlay)},
    )

    blocked_known = graph.shortest_path("city", "inner", observer=("character", "mc"))
    false_known = graph.shortest_path("city", "false_goal", observer=("character", "mc"))

    assert blocked_known.reachable is False
    assert false_known.reachable is True
    assert false_known.path_edge_ids == ["false_shortcut"]
    assert false_known.total_distance == 0.2
    assert false_known.total_travel_time == 0.1
    assert false_known.total_travel_cost == 3
    assert false_known.total_risk == 4
    assert false_known.total_narrative_cost == 5
