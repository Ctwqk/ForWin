from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any

from forwin.book_state.cognition import CognitionView
from forwin.book_state.map_graph import MapGraph
from forwin.book_state.narrative import NarrativeControlGraph
from forwin.protocol.book_state import (
    EdgePatch,
    FactNode,
    FactPatch,
    GraphDelta,
    NodePatch,
    PathResult,
    WorldEdge,
    WorldNode,
)


class ObjectiveWorldGraph:
    """Typed property graph for objective world truth."""

    def __init__(
        self,
        *,
        nodes: list[WorldNode] | None = None,
        states_by_node_id: dict[str, dict[str, Any]] | None = None,
        edges: list[WorldEdge] | None = None,
        facts: list[FactNode] | None = None,
    ) -> None:
        self.nodes_by_id: dict[str, WorldNode] = {node.id: node for node in nodes or []}
        self.states_by_node_id: dict[str, dict[str, Any]] = {
            node_id: dict(state)
            for node_id, state in (states_by_node_id or {}).items()
        }
        for node in nodes or []:
            self.states_by_node_id.setdefault(node.id, dict(node.state))
        self.edges_by_id: dict[str, WorldEdge] = {}
        self.outgoing_edges: dict[str, list[str]] = defaultdict(list)
        self.incoming_edges: dict[str, list[str]] = defaultdict(list)
        self.facts_by_id: dict[str, FactNode] = {fact.id: fact for fact in facts or []}
        self.facts_by_related_ref: dict[str, list[str]] = defaultdict(list)

        for edge in edges or []:
            self.add_edge(edge)
        for fact in facts or []:
            self.add_fact(fact)

    def get_node(self, node_id: str) -> WorldNode | None:
        return self.nodes_by_id.get(node_id)

    def get_state(self, node_id: str) -> dict[str, Any]:
        return dict(self.states_by_node_id.get(node_id, {}))

    def add_node(self, node: WorldNode) -> None:
        self.nodes_by_id[node.id] = node
        self.states_by_node_id.setdefault(node.id, dict(node.state))

    def add_edge(self, edge: WorldEdge) -> None:
        self.edges_by_id[edge.id] = edge
        self.outgoing_edges[edge.source_id].append(edge.id)
        self.incoming_edges[edge.target_id].append(edge.id)

    def add_fact(self, fact: FactNode) -> None:
        self.facts_by_id[fact.id] = fact
        for ref in fact.related_node_refs + fact.related_edge_refs:
            self.facts_by_related_ref[ref].append(fact.id)

    def get_edges(self, source_id: str, edge_type: str | None = None) -> list[WorldEdge]:
        edges = [
            self.edges_by_id[edge_id]
            for edge_id in self.outgoing_edges.get(source_id, [])
            if edge_id in self.edges_by_id
        ]
        if edge_type is not None:
            edges = [edge for edge in edges if edge.edge_type == edge_type]
        return edges

    def get_neighbors(self, node_id: str, edge_type: str | None = None) -> list[WorldNode]:
        neighbors: list[WorldNode] = []
        for edge in self.get_edges(node_id, edge_type=edge_type):
            target = self.nodes_by_id.get(edge.target_id)
            if target is not None:
                neighbors.append(target)
        return neighbors

    def get_facts_about(self, ref: str) -> list[FactNode]:
        return [
            self.facts_by_id[fact_id]
            for fact_id in self.facts_by_related_ref.get(ref, [])
            if fact_id in self.facts_by_id
        ]

    def apply_delta(self, graph_delta: GraphDelta) -> None:
        for patch in graph_delta.node_patches:
            self.apply_node_patch(patch)
        for patch in graph_delta.edge_patches:
            self.apply_edge_patch(patch)
        for patch in graph_delta.fact_patches:
            self.apply_fact_patch(patch)

    def apply_node_patch(self, patch: NodePatch) -> None:
        op = str(patch.op)
        if op == "create":
            payload = patch.new_value if isinstance(patch.new_value, dict) else {}
            node = WorldNode.model_validate(
                {
                    "id": patch.node_id,
                    "node_type": patch.node_type,
                    **payload,
                }
            )
            self.add_node(node)
            return

        node = self.nodes_by_id.get(patch.node_id)
        if node is None:
            raise KeyError(f"unknown world node: {patch.node_id}")
        if op == "deactivate":
            self.nodes_by_id[patch.node_id] = node.model_copy(update={"is_active": False})
            return

        if patch.field_path.startswith("state."):
            state = dict(self.states_by_node_id.get(patch.node_id, {}))
            _set_path(state, patch.field_path.removeprefix("state."), patch.new_value, op=op)
            self.states_by_node_id[patch.node_id] = state
            return

        payload = node.model_dump(mode="json")
        _set_path(payload, patch.field_path, patch.new_value, op=op)
        updated = WorldNode.model_validate(payload)
        self.nodes_by_id[patch.node_id] = updated
        if "state" in payload:
            self.states_by_node_id[patch.node_id] = dict(updated.state)

    def apply_edge_patch(self, patch: EdgePatch) -> None:
        op = str(patch.op)
        if op == "create":
            payload = patch.new_value if isinstance(patch.new_value, dict) else {}
            edge = WorldEdge.model_validate(
                {
                    "id": patch.edge_id,
                    "source_id": patch.source_id or payload.get("source_id", ""),
                    "target_id": patch.target_id or payload.get("target_id", ""),
                    "edge_type": patch.edge_type or payload.get("edge_type", ""),
                    "edge_family": patch.edge_family or payload.get("edge_family", ""),
                    **payload,
                }
            )
            self.add_edge(edge)
            return
        edge = self.edges_by_id.get(patch.edge_id)
        if edge is None:
            raise KeyError(f"unknown world edge: {patch.edge_id}")
        if op == "deactivate":
            self.edges_by_id[patch.edge_id] = edge.model_copy(update={"is_active": False})
            return
        payload = edge.model_dump(mode="json")
        _set_path(payload, patch.field_path, patch.new_value, op=op)
        self.edges_by_id[patch.edge_id] = WorldEdge.model_validate(payload)

    def apply_fact_patch(self, patch: FactPatch) -> None:
        op = str(patch.op)
        if op == "create":
            payload = patch.new_value if isinstance(patch.new_value, dict) else {}
            fact = FactNode.model_validate(
                {
                    "id": patch.fact_id,
                    "project_id": payload.get("project_id", ""),
                    "proposition": patch.proposition or payload.get("proposition", ""),
                    "truth_value": patch.truth_value or payload.get("truth_value", "true"),
                    **payload,
                }
            )
            self.add_fact(fact)
            return
        fact = self.facts_by_id.get(patch.fact_id)
        if fact is None:
            raise KeyError(f"unknown fact node: {patch.fact_id}")
        payload = fact.model_dump(mode="json")
        field_path = patch.field_path if hasattr(patch, "field_path") else ""
        if field_path:
            _set_path(payload, field_path, patch.new_value, op=op)
        elif patch.new_value is not None:
            payload["state"] = patch.new_value
        self.facts_by_id[patch.fact_id] = FactNode.model_validate(payload)

    def snapshot(self) -> dict[str, Any]:
        return {
            "nodes_by_id": {
                node_id: node.model_dump(mode="json")
                for node_id, node in self.nodes_by_id.items()
            },
            "states_by_node_id": dict(self.states_by_node_id),
            "edges_by_id": {
                edge_id: edge.model_dump(mode="json")
                for edge_id, edge in self.edges_by_id.items()
            },
            "facts_by_id": {
                fact_id: fact.model_dump(mode="json")
                for fact_id, fact in self.facts_by_id.items()
            },
        }


class BookStateRuntime:
    def __init__(
        self,
        *,
        project_id: str,
        as_of_chapter: int,
        world: ObjectiveWorldGraph,
        map_graph: MapGraph,
        cognition_by_observer: dict[tuple[str, str], CognitionView] | None = None,
        narrative: NarrativeControlGraph | None = None,
    ) -> None:
        self.project_id = project_id
        self.as_of_chapter = as_of_chapter
        self.world = world
        self.map = map_graph
        self.cognition_by_observer = cognition_by_observer or {}
        self.narrative = narrative or NarrativeControlGraph()
        self.map.cognition_by_observer.update(self.cognition_by_observer)


def distance_between_world_nodes(
    world: ObjectiveWorldGraph,
    map_graph: MapGraph,
    source_node_id: str,
    target_node_id: str,
    *,
    metric: str = "travel_time",
    observer: tuple[str, str] | None = None,
    legacy_compat_observer: Callable[[dict[str, Any]], None] | None = None,
) -> PathResult:
    source_location = _resolve_location(
        world,
        source_node_id,
        legacy_compat_observer=legacy_compat_observer,
    )
    target_location = _resolve_location(
        world,
        target_node_id,
        legacy_compat_observer=legacy_compat_observer,
    )
    if not source_location:
        return PathResult(
            reachable=False,
            from_node_id="",
            to_node_id=target_location or "",
            metric=metric,
            blocked_reason=f"missing location_id for {source_node_id}",
            explanation=f"缺少 {source_node_id} 的 location_id。",
        )
    if not target_location:
        return PathResult(
            reachable=False,
            from_node_id=source_location,
            to_node_id="",
            metric=metric,
            blocked_reason=f"missing location_id for {target_node_id}",
            explanation=f"缺少 {target_node_id} 的 location_id。",
        )
    return map_graph.shortest_path(
        source_location,
        target_location,
        metric=metric,
        observer=observer,
    )


def _resolve_location(
    world: ObjectiveWorldGraph,
    node_id: str,
    *,
    legacy_compat_observer: Callable[[dict[str, Any]], None] | None = None,
) -> str:
    state = world.get_state(node_id)
    location_id = str(state.get("location_id", "") or "").strip()
    if location_id:
        return location_id
    legacy_location = str(state.get("location", "") or "").strip()
    if legacy_location:
        if legacy_compat_observer is not None:
            legacy_compat_observer(
                {
                    "compat_layer": "book_state",
                    "compat_feature": "book_state.state.location_fallback",
                    "usage_kind": "read_fallback",
                    "source_module": "forwin.book_state.runtime",
                    "usage_reason": "state.location used because location_id is missing",
                    "compat_key": "state.location",
                    "legacy_identifier": legacy_location,
                    "metadata": {"node_id": node_id, "field_path": "state.location"},
                }
            )
        return legacy_location
    current_activity_id = str(state.get("current_activity_id", "") or "").strip()
    if current_activity_id:
        activity_state = world.get_state(current_activity_id)
        activity_location = str(activity_state.get("current_location_id", "") or "").strip()
        if activity_location:
            return activity_location
    node = world.get_node(node_id)
    if node is not None and node.node_type == "faction":
        headquarters = str(state.get("headquarters_location_id", "") or "").strip()
        if headquarters:
            return headquarters
    if node is not None and node.node_type == "site_state":
        map_node_id = str(node.profile.get("map_node_id", "") or "").strip()
        if map_node_id:
            return map_node_id
    return ""


def _set_path(payload: dict[str, Any], field_path: str, value: Any, *, op: str) -> None:
    parts = [part for part in field_path.split(".") if part]
    if not parts:
        return
    cursor = payload
    for part in parts[:-1]:
        nested = cursor.get(part)
        if not isinstance(nested, dict):
            nested = {}
            cursor[part] = nested
        cursor = nested
    key = parts[-1]
    if op == "append":
        current = cursor.setdefault(key, [])
        if isinstance(current, list):
            current.append(value)
        else:
            cursor[key] = [current, value]
    elif op == "remove":
        current = cursor.get(key)
        if isinstance(current, list):
            cursor[key] = [item for item in current if item != value]
        else:
            cursor.pop(key, None)
    elif op == "merge" and isinstance(value, dict):
        current = cursor.get(key)
        if isinstance(current, dict):
            current.update(value)
        else:
            cursor[key] = dict(value)
    else:
        cursor[key] = value
