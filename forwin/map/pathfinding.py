from __future__ import annotations

from forwin.book_state.map_graph import MapGraph
from forwin.book_state.runtime import (
    ObjectiveWorldGraph,
    distance_between_world_nodes,
)
from forwin.protocol.book_state import MapEdge, MapNode, PathResult


def build_runtime_adjacency(map_edges: list[MapEdge]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    outgoing: dict[str, list[str]] = {}
    incoming: dict[str, list[str]] = {}
    for edge in map_edges:
        outgoing.setdefault(edge.from_node_id, []).append(edge.id)
        incoming.setdefault(edge.to_node_id, []).append(edge.id)
    return outgoing, incoming


def apply_observer_filter(
    graph: MapGraph,
    node_id: str,
    *,
    observer: tuple[str, str] | None = None,
    allow_hidden: bool = False,
    allow_blocked: bool = False,
) -> list[tuple[str, MapEdge]]:
    return graph.get_accessible_neighbors(
        node_id,
        observer=observer,
        allow_hidden=allow_hidden,
        allow_blocked=allow_blocked,
    )


def shortest_path(
    map_nodes: list[MapNode],
    map_edges: list[MapEdge],
    from_node_id: str,
    to_node_id: str,
    *,
    metric: str = "travel_time",
    observer: tuple[str, str] | None = None,
    allow_hidden: bool = False,
    allow_blocked: bool = False,
) -> PathResult:
    graph = MapGraph(nodes=map_nodes, edges=map_edges)
    return graph.shortest_path(
        from_node_id,
        to_node_id,
        metric=metric,
        observer=observer,
        allow_hidden=allow_hidden,
        allow_blocked=allow_blocked,
    )


__all__ = [
    "MapGraph",
    "ObjectiveWorldGraph",
    "apply_observer_filter",
    "build_runtime_adjacency",
    "distance_between_world_nodes",
    "shortest_path",
]
