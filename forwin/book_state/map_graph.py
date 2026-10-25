from __future__ import annotations

import heapq
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from forwin.book_state.cognition import CognitionView
from forwin.book_state.path_patch import apply_path_patch
from forwin.protocol.book_state import MapEdge, MapNode, MapPatch, PathMetric, PathResult


_BLOCKED_EDGE_STATUSES = {"blocked", "destroyed", "sealed"}
_HIDDEN_EDGE_STATUSES = {"hidden"}


@dataclass(frozen=True)
class _QueueItem:
    cost: float
    node_id: str

    def __lt__(self, other: "_QueueItem") -> bool:
        return (self.cost, self.node_id) < (other.cost, other.node_id)


class MapGraph:
    """Weighted directed multigraph with observer-aware path search."""

    def __init__(
        self,
        *,
        nodes: list[MapNode] | None = None,
        edges: list[MapEdge] | None = None,
        cognition_by_observer: dict[tuple[str, str], CognitionView] | None = None,
    ) -> None:
        self.nodes_by_id: dict[str, MapNode] = {}
        self.edges_by_id: dict[str, MapEdge] = {}
        self.outgoing_edges: dict[str, list[str]] = defaultdict(list)
        self.incoming_edges: dict[str, list[str]] = defaultdict(list)
        self.children_by_parent: dict[str, list[str]] = defaultdict(list)
        self.path_cache: dict[tuple[str, str, str, str], PathResult] = {}
        self.cognition_by_observer = cognition_by_observer or {}

        for node in nodes or []:
            self.add_node(node)
        for edge in edges or []:
            self.add_edge(edge)

    def add_node(self, node: MapNode) -> None:
        self.nodes_by_id[node.id] = node
        if node.parent_id:
            self.children_by_parent[node.parent_id].append(node.id)

    def add_edge(self, edge: MapEdge) -> None:
        self._add_directed_edge(edge)
        if edge.bidirectional:
            reverse = edge.model_copy(
                update={
                    "id": f"{edge.id}__reverse",
                    "from_node_id": edge.to_node_id,
                    "to_node_id": edge.from_node_id,
                    "bidirectional": False,
                    "metadata": {
                        **edge.metadata,
                        "runtime_reverse_of": edge.id,
                    },
                }
            )
            self._add_directed_edge(reverse)

    def _add_directed_edge(self, edge: MapEdge) -> None:
        self.edges_by_id[edge.id] = edge
        self.outgoing_edges[edge.from_node_id].append(edge.id)
        self.incoming_edges[edge.to_node_id].append(edge.id)

    def get_accessible_neighbors(
        self,
        node_id: str,
        *,
        observer: tuple[str, str] | None = None,
        allow_hidden: bool = False,
        allow_blocked: bool = False,
    ) -> list[tuple[str, MapEdge]]:
        cognition = self.cognition_by_observer.get(observer) if observer else None
        edges = [
            self.edges_by_id[edge_id]
            for edge_id in self.outgoing_edges.get(node_id, [])
            if edge_id in self.edges_by_id
        ]
        if cognition is not None:
            for false_edge in cognition.false_edges.values():
                if isinstance(false_edge, MapEdge) and false_edge.from_node_id == node_id:
                    edges.append(false_edge)

        result: list[tuple[str, MapEdge]] = []
        for edge in edges:
            if self._edge_available(
                edge,
                cognition=cognition,
                allow_hidden=allow_hidden,
                allow_blocked=allow_blocked,
            ):
                result.append((edge.to_node_id, edge))
        return result

    def shortest_path(
        self,
        from_node_id: str,
        to_node_id: str,
        *,
        metric: str = PathMetric.TRAVEL_TIME,
        observer: tuple[str, str] | None = None,
        allow_hidden: bool = False,
        allow_blocked: bool = False,
        algorithm: str = "dijkstra",
        composite_weights: dict[str, float] | None = None,
        allowed_edge_types: frozenset[str] | None = None,
    ) -> PathResult:
        """Filter by edge type or a world gate's source type, e.g. one travel mode's."""
        observer_key = f"{observer[0]}:{observer[1]}" if observer else "objective"
        weights_key = ",".join(f"{key}={value}" for key, value in sorted((composite_weights or {}).items()))
        search_key = f"{metric}:{algorithm}:{weights_key}"
        if allowed_edge_types is not None:
            search_key += ":edge_types=" + ",".join(sorted(allowed_edge_types))
        cache_key = (from_node_id, to_node_id, search_key, observer_key)
        if not allow_hidden and not allow_blocked and cache_key in self.path_cache:
            return self.path_cache[cache_key]

        if from_node_id not in self.nodes_by_id:
            return self._unreachable(from_node_id, to_node_id, metric, f"unknown from_node_id: {from_node_id}")
        if to_node_id not in self.nodes_by_id:
            return self._unreachable(from_node_id, to_node_id, metric, f"unknown to_node_id: {to_node_id}")
        if from_node_id == to_node_id:
            return PathResult(
                reachable=True,
                from_node_id=from_node_id,
                to_node_id=to_node_id,
                metric=str(metric),
                path_node_ids=[from_node_id],
                explanation="起点与终点相同。",
            )

        distances: dict[str, float] = {from_node_id: 0.0}
        previous: dict[str, tuple[str, MapEdge]] = {}
        use_astar = algorithm == "astar" or (algorithm == "auto" and self._has_coordinates(from_node_id, to_node_id))
        queue: list[_QueueItem] = [_QueueItem(0.0, from_node_id)]
        visited: set[str] = set()

        while queue:
            current = heapq.heappop(queue)
            if current.node_id in visited:
                continue
            visited.add(current.node_id)
            if current.node_id == to_node_id:
                break
            for neighbor_id, edge in self.get_accessible_neighbors(
                current.node_id,
                observer=observer,
                allow_hidden=allow_hidden,
                allow_blocked=allow_blocked,
            ):
                if not self._edge_type_allowed(edge, allowed_edge_types):
                    continue
                weight = self._metric_weight(edge, str(metric), composite_weights=composite_weights)
                current_cost = distances.get(current.node_id, float("inf"))
                next_cost = current_cost + weight
                if next_cost < distances.get(neighbor_id, float("inf")):
                    distances[neighbor_id] = next_cost
                    previous[neighbor_id] = (current.node_id, edge)
                    priority = next_cost
                    if use_astar:
                        priority += self._heuristic(neighbor_id, to_node_id, str(metric))
                    heapq.heappush(queue, _QueueItem(priority, neighbor_id))

        if to_node_id not in distances:
            return self._unreachable(from_node_id, to_node_id, metric, "no accessible path")

        path_node_ids, path_edges = self._reconstruct_path(from_node_id, to_node_id, previous)
        result = self._path_result(from_node_id, to_node_id, str(metric), path_node_ids, path_edges)
        if not allow_hidden and not allow_blocked:
            self.path_cache[cache_key] = result
        return result

    @staticmethod
    def _edge_type_allowed(edge: MapEdge, allowed_edge_types: frozenset[str] | None) -> bool:
        return (allowed_edge_types is None or str(edge.edge_type) in allowed_edge_types
                or (edge.edge_type == "world_gate"
                    and str(edge.metadata.get("source_edge_type", "")) in allowed_edge_types))

    def single_source_travel_paths(
        self,
        source_id: str,
        *,
        profiles: list[tuple[tuple[str, str] | None, frozenset[str] | None]],
    ) -> list[dict[str, PathResult]]:
        """One Dijkstra over independent observer/mode layers, without switching modes mid-route.

        Search weights, edge availability and reported path times match ``shortest_path``.
        Results belong to this map snapshot; callers keep them local to one actor or event.
        """
        distances = [{source_id: 0.0} for _ in profiles]
        previous: list[dict[str, tuple[str, MapEdge]]] = [{} for _ in profiles]
        queue = [(0.0, source_id, index) for index in range(len(profiles))
                 if source_id in self.nodes_by_id]
        heapq.heapify(queue)
        visited: set[tuple[int, str]] = set()
        while queue:
            cost, node_id, index = heapq.heappop(queue)
            if (index, node_id) in visited:
                continue
            visited.add((index, node_id))
            observer, allowed = profiles[index]
            for neighbor, edge in self.get_accessible_neighbors(node_id, observer=observer):
                if not self._edge_type_allowed(edge, allowed):
                    continue
                next_cost = cost + self._metric_weight(edge, "travel_time")
                if next_cost < distances[index].get(neighbor, float("inf")):
                    distances[index][neighbor] = next_cost
                    previous[index][neighbor] = (node_id, edge)
                    heapq.heappush(queue, (next_cost, neighbor, index))
        results = []
        for index in range(len(profiles)):
            paths = {}
            for target in self.nodes_by_id:
                if source_id not in self.nodes_by_id:
                    paths[target] = self._unreachable(source_id, target, "travel_time",
                                                      f"unknown from_node_id: {source_id}")
                elif target not in distances[index]:
                    paths[target] = self._unreachable(source_id, target, "travel_time", "no accessible path")
                else:
                    nodes, edges = self._reconstruct_path(source_id, target, previous[index])
                    paths[target] = self._path_result(source_id, target, "travel_time", nodes, edges)
            results.append(paths)
        return results

    def all_pairs_shortest_paths(
        self,
        *,
        metric: str = PathMetric.TRAVEL_TIME,
        node_ids: list[str] | None = None,
        observer: tuple[str, str] | None = None,
        max_nodes: int = 64,
    ) -> dict[tuple[str, str], PathResult]:
        selected = node_ids or sorted(self.nodes_by_id)
        if len(selected) > max_nodes:
            raise ValueError(f"all-pairs path index is limited to {max_nodes} nodes")
        results: dict[tuple[str, str], PathResult] = {}
        for source_id in selected:
            results.update(
                self._single_source_shortest_paths(
                    source_id,
                    target_ids=[target_id for target_id in selected if target_id != source_id],
                    metric=str(metric),
                    observer=observer,
                )
            )
        return results

    def apply_map_patch(self, patch: MapPatch) -> None:
        if patch.target_type == "map_node":
            node = self.nodes_by_id.get(patch.target_id)
            if node is None and patch.op == "create" and isinstance(patch.new_value, dict):
                self.add_node(MapNode.model_validate(patch.new_value))
            elif node is not None:
                self.nodes_by_id[node.id] = _apply_model_patch(node, patch.field_path, str(patch.op), patch.new_value)
        elif patch.target_type == "map_edge":
            edge = self.edges_by_id.get(patch.target_id)
            if edge is None and patch.op == "create" and isinstance(patch.new_value, dict):
                self.add_edge(MapEdge.model_validate(patch.new_value))
            elif edge is not None:
                self.edges_by_id[edge.id] = _apply_model_patch(edge, patch.field_path, str(patch.op), patch.new_value)
        self.invalidate_path_cache(patch)

    def invalidate_path_cache(self, patch: MapPatch | None = None) -> None:
        if patch is None or not patch.affected_path_cache_keys:
            self.path_cache.clear()
            return
        for key_text in patch.affected_path_cache_keys:
            for key in list(self.path_cache):
                if key_text in ":".join(key):
                    self.path_cache.pop(key, None)

    def _edge_available(
        self,
        edge: MapEdge,
        *,
        cognition: CognitionView | None,
        allow_hidden: bool,
        allow_blocked: bool,
    ) -> bool:
        status = edge.status
        discovered = edge.discovered_by_default
        if cognition is not None:
            override = cognition.get_field_override(edge.id, "status", None)
            if override is not None:
                status = str(override)
            discovered_override = cognition.get_field_override(edge.id, "discovered_by_default", None)
            if discovered_override is not None:
                discovered = bool(discovered_override)

        if status in _BLOCKED_EDGE_STATUSES and not allow_blocked:
            return False

        edge_ref = f"map_edge:{edge.id}"
        hidden_by_status = status in _HIDDEN_EDGE_STATUSES or edge.edge_type == "hidden_route"
        hidden_by_discovery = not discovered
        if cognition is not None and edge_ref in cognition.hidden_refs and not allow_hidden:
            return False
        if cognition is not None and (hidden_by_status or hidden_by_discovery):
            if allow_hidden:
                return True
            return edge_ref in cognition.visible_refs or edge_ref in cognition.confirmed_refs or edge_ref in cognition.suspected_refs
        if cognition is None and (hidden_by_status or hidden_by_discovery):
            return allow_hidden or True
        return True

    @staticmethod
    def _metric_weight(
        edge: MapEdge,
        metric: str,
        *,
        composite_weights: dict[str, float] | None = None,
    ) -> float:
        if metric in {PathMetric.PHYSICAL_DISTANCE, PathMetric.DISTANCE, "physical_distance", "distance", "known_distance"}:
            return float(edge.distance or 0.0)
        if metric == PathMetric.TRAVEL_TIME or metric == "travel_time":
            return float(edge.travel_time if edge.travel_time is not None else edge.distance or 0.0)
        if metric == PathMetric.TRAVEL_COST or metric == "travel_cost":
            return float(edge.travel_cost or 0.0)
        if metric in {PathMetric.RISK, PathMetric.RISK_COST, "risk_cost", "risk"}:
            return float(edge.risk_level or 0.0)
        if metric == PathMetric.NARRATIVE_COST or metric == "narrative_cost":
            return float(edge.narrative_cost or 0.0)
        if metric in {PathMetric.COMPOSITE, PathMetric.COMPOSITE_COST, "composite", "composite_cost"}:
            weights = {
                "distance": 1.0,
                "travel_time": 1.0,
                "travel_cost": 1.0,
                "risk_level": 1.0,
                "narrative_cost": 1.0,
                **(composite_weights or {}),
            }
            return (
                float(edge.distance or 0.0) * float(weights["distance"])
                + float(edge.travel_time or 0.0) * float(weights["travel_time"])
                + float(edge.travel_cost or 0.0) * float(weights["travel_cost"])
                + float(edge.risk_level or 0.0) * float(weights["risk_level"])
                + float(edge.narrative_cost or 0.0) * float(weights["narrative_cost"])
            )
        return (
            float(edge.distance or 0.0)
            + float(edge.travel_time or 0.0)
            + float(edge.travel_cost or 0.0)
            + float(edge.risk_level or 0.0)
            + float(edge.narrative_cost or 0.0)
        )

    def _has_coordinates(self, from_node_id: str, to_node_id: str) -> bool:
        return _coordinate_pair(self.nodes_by_id.get(from_node_id)) is not None and _coordinate_pair(
            self.nodes_by_id.get(to_node_id)
        ) is not None

    def _heuristic(self, from_node_id: str, to_node_id: str, metric: str) -> float:
        if metric not in {"physical_distance", "distance", "known_distance"}:
            return 0.0
        left = _coordinate_pair(self.nodes_by_id.get(from_node_id))
        right = _coordinate_pair(self.nodes_by_id.get(to_node_id))
        if left is None or right is None:
            return 0.0
        return ((left[0] - right[0]) ** 2 + (left[1] - right[1]) ** 2) ** 0.5

    @staticmethod
    def _reconstruct_path(
        from_node_id: str,
        to_node_id: str,
        previous: dict[str, tuple[str, MapEdge]],
    ) -> tuple[list[str], list[MapEdge]]:
        nodes = [to_node_id]
        edges: list[MapEdge] = []
        current = to_node_id
        while current != from_node_id:
            prev_node, edge = previous[current]
            edges.append(edge)
            nodes.append(prev_node)
            current = prev_node
        nodes.reverse()
        edges.reverse()
        return nodes, edges

    def _path_result(
        self,
        from_node_id: str,
        to_node_id: str,
        metric: str,
        path_node_ids: list[str],
        path_edges: list[MapEdge],
    ) -> PathResult:
        return PathResult(
            reachable=True,
            from_node_id=from_node_id,
            to_node_id=to_node_id,
            metric=metric,
            total_distance=sum(float(edge.distance or 0.0) for edge in path_edges),
            total_travel_time=sum(float(edge.travel_time or 0.0) for edge in path_edges),
            total_travel_cost=sum(float(edge.travel_cost or 0.0) for edge in path_edges),
            total_risk=sum(float(edge.risk_level or 0.0) for edge in path_edges),
            total_narrative_cost=sum(float(edge.narrative_cost or 0.0) for edge in path_edges),
            path_node_ids=path_node_ids,
            path_edge_ids=[edge.id for edge in path_edges],
            explanation=" -> ".join(path_node_ids),
        )

    def _single_source_shortest_paths(
        self,
        source_id: str,
        *,
        target_ids: list[str],
        metric: str,
        observer: tuple[str, str] | None,
    ) -> dict[tuple[str, str], PathResult]:
        if source_id not in self.nodes_by_id:
            return {
                (source_id, target_id): self._unreachable(
                    source_id,
                    target_id,
                    metric,
                    f"unknown from_node_id: {source_id}",
                )
                for target_id in target_ids
            }

        distances: dict[str, float] = {source_id: 0.0}
        previous: dict[str, tuple[str, MapEdge]] = {}
        queue: list[_QueueItem] = [_QueueItem(0.0, source_id)]
        visited: set[str] = set()
        remaining_targets = {target_id for target_id in target_ids if target_id in self.nodes_by_id}

        while queue and remaining_targets:
            current = heapq.heappop(queue)
            if current.node_id in visited:
                continue
            visited.add(current.node_id)
            remaining_targets.discard(current.node_id)
            for neighbor_id, edge in self.get_accessible_neighbors(current.node_id, observer=observer):
                weight = self._metric_weight(edge, metric)
                next_cost = distances.get(current.node_id, float("inf")) + weight
                if next_cost < distances.get(neighbor_id, float("inf")):
                    distances[neighbor_id] = next_cost
                    previous[neighbor_id] = (current.node_id, edge)
                    heapq.heappush(queue, _QueueItem(next_cost, neighbor_id))

        results: dict[tuple[str, str], PathResult] = {}
        for target_id in target_ids:
            if target_id not in self.nodes_by_id:
                results[(source_id, target_id)] = self._unreachable(
                    source_id,
                    target_id,
                    metric,
                    f"unknown to_node_id: {target_id}",
                )
                continue
            if target_id not in distances:
                results[(source_id, target_id)] = self._unreachable(source_id, target_id, metric, "no accessible path")
                continue
            path_node_ids, path_edges = self._reconstruct_path(source_id, target_id, previous)
            results[(source_id, target_id)] = self._path_result(
                source_id,
                target_id,
                metric,
                path_node_ids,
                path_edges,
            )
        return results

    @staticmethod
    def _unreachable(from_node_id: str, to_node_id: str, metric: str, reason: str) -> PathResult:
        return PathResult(
            reachable=False,
            from_node_id=from_node_id,
            to_node_id=to_node_id,
            metric=str(metric),
            blocked_reason=reason,
            explanation=reason,
        )


def _apply_model_patch(model: MapNode | MapEdge, field_path: str, op: str, new_value: Any) -> Any:
    payload = model.model_dump(mode="json")
    if op == "deactivate":
        payload["status"] = "destroyed"
    elif not field_path:
        if op in {"replace", "set"} and isinstance(new_value, dict):
            payload.update(new_value)
    else:
        apply_path_patch(payload, field_path, new_value, op=op)
    return type(model).model_validate(payload)


def _coordinate_pair(node: MapNode | None) -> tuple[float, float] | None:
    if node is None or not isinstance(node.coordinates, dict):
        return None
    raw_x = node.coordinates.get("x", node.coordinates.get("lon", node.coordinates.get("longitude")))
    raw_y = node.coordinates.get("y", node.coordinates.get("lat", node.coordinates.get("latitude")))
    try:
        return float(raw_x), float(raw_y)
    except (TypeError, ValueError):
        return None
