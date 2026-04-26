from __future__ import annotations

from forwin.map.pathfinding import MapGraph
from forwin.map.protocol import MapEdge, MapNode


def _nodes() -> list[MapNode]:
    return [
        MapNode(id="a", project_id="p1", subworld_id="sw1", region_id="r1", node_type="settlement"),
        MapNode(id="b", project_id="p1", subworld_id="sw1", region_id="r1", node_type="waypoint"),
        MapNode(id="c", project_id="p1", subworld_id="sw1", region_id="r1", node_type="site"),
    ]


def test_dijkstra_supports_directed_edges_and_unreachable_result() -> None:
    graph = MapGraph(
        nodes=_nodes(),
        edges=[
            MapEdge(
                id="ab",
                project_id="p1",
                subworld_id="sw1",
                from_node_id="a",
                to_node_id="b",
                edge_type="road",
                travel_time=1,
            )
        ],
    )

    assert graph.shortest_path("a", "b").reachable is True
    reverse = graph.shortest_path("b", "a")
    assert reverse.reachable is False
    assert reverse.blocked_reason == "no accessible path"


def test_bidirectional_and_multiedge_choose_metric_best_route() -> None:
    graph = MapGraph(
        nodes=_nodes(),
        edges=[
            MapEdge(id="ab_slow", project_id="p1", subworld_id="sw1", from_node_id="a", to_node_id="b", edge_type="road", bidirectional=True, travel_time=10, distance=1),
            MapEdge(id="ab_fast", project_id="p1", subworld_id="sw1", from_node_id="a", to_node_id="b", edge_type="portal", travel_time=1, distance=100, travel_cost=50),
            MapEdge(id="bc", project_id="p1", subworld_id="sw1", from_node_id="b", to_node_id="c", edge_type="road", travel_time=1, distance=1),
        ],
    )

    assert graph.shortest_path("b", "a").path_edge_ids == ["ab_slow__reverse"]
    fastest = graph.shortest_path("a", "b", metric="travel_time")
    shortest = graph.shortest_path("a", "b", metric="distance")
    assert fastest.path_edge_ids == ["ab_fast"]
    assert shortest.path_edge_ids == ["ab_slow"]


def test_blocked_edges_are_skipped() -> None:
    graph = MapGraph(
        nodes=_nodes(),
        edges=[
            MapEdge(id="ab_blocked", project_id="p1", subworld_id="sw1", from_node_id="a", to_node_id="b", edge_type="road", status="blocked", travel_time=1),
            MapEdge(id="ac", project_id="p1", subworld_id="sw1", from_node_id="a", to_node_id="c", edge_type="road", travel_time=2),
            MapEdge(id="cb", project_id="p1", subworld_id="sw1", from_node_id="c", to_node_id="b", edge_type="road", travel_time=2),
        ],
    )

    result = graph.shortest_path("a", "b")

    assert result.reachable is True
    assert result.path_edge_ids == ["ac", "cb"]
