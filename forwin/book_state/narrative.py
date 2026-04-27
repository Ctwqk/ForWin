from __future__ import annotations

from collections import defaultdict
from typing import Any

from forwin.protocol.book_state import NarrativeEdge, NarrativeNode, NarrativePatch


NARRATIVE_NODE_TYPES: set[str] = {
    "world_line",
    "plot_thread",
    "promise",
    "knowledge_gap",
    "reveal_plan",
    "review_constraint",
}

NARRATIVE_EDGE_TYPES: set[str] = {
    "opens",
    "advances",
    "delays",
    "blocks",
    "resolves",
    "foreshadows",
    "pays_off",
    "creates_gap",
    "closes_gap",
    "reveals",
    "hides",
    "escalates",
    "protects_until",
    "contradicts_plan",
}


class NarrativeControlGraph:
    """Runtime graph for writing-control state: lines, promises, gaps, and reveals."""

    def __init__(
        self,
        *,
        nodes: list[NarrativeNode] | None = None,
        edges: list[NarrativeEdge] | None = None,
    ) -> None:
        self.nodes_by_id: dict[str, NarrativeNode] = {}
        self.edges_by_id: dict[str, NarrativeEdge] = {}
        self.outgoing_edges: dict[str, list[str]] = defaultdict(list)
        self.incoming_edges: dict[str, list[str]] = defaultdict(list)
        for node in nodes or []:
            self.add_node(node)
        for edge in edges or []:
            self.add_edge(edge)

    def add_node(self, node: NarrativeNode) -> None:
        if node.node_type not in NARRATIVE_NODE_TYPES:
            raise ValueError(f"unknown narrative node type: {node.node_type}")
        self.nodes_by_id[node.id] = node

    def add_edge(self, edge: NarrativeEdge) -> None:
        if edge.edge_type not in NARRATIVE_EDGE_TYPES:
            raise ValueError(f"unknown narrative edge type: {edge.edge_type}")
        existing = self.edges_by_id.get(edge.id)
        if existing is not None:
            if edge.id in self.outgoing_edges.get(existing.source_id, []):
                self.outgoing_edges[existing.source_id] = [
                    edge_id for edge_id in self.outgoing_edges[existing.source_id] if edge_id != edge.id
                ]
            if edge.id in self.incoming_edges.get(existing.target_id, []):
                self.incoming_edges[existing.target_id] = [
                    edge_id for edge_id in self.incoming_edges[existing.target_id] if edge_id != edge.id
                ]
        self.edges_by_id[edge.id] = edge
        self.outgoing_edges[edge.source_id].append(edge.id)
        self.incoming_edges[edge.target_id].append(edge.id)

    def get_node(self, node_id: str) -> NarrativeNode | None:
        return self.nodes_by_id.get(node_id)

    def get_edges(self, source_id: str, edge_type: str | None = None) -> list[NarrativeEdge]:
        edges = [
            self.edges_by_id[edge_id]
            for edge_id in self.outgoing_edges.get(source_id, [])
            if edge_id in self.edges_by_id
        ]
        if edge_type is not None:
            edges = [edge for edge in edges if edge.edge_type == edge_type]
        return edges

    def active_world_line_ids(self) -> list[str]:
        return [
            node_id
            for node_id, node in self.nodes_by_id.items()
            if node.node_type == "world_line" and node.status not in {"resolved", "closed", "inactive"}
        ]

    def open_gap_ids(self) -> list[str]:
        return [
            node_id
            for node_id, node in self.nodes_by_id.items()
            if node.node_type == "knowledge_gap" and node.status in {"active", "open", "hinted", "partially_closed"}
        ]

    def apply_narrative_patch(self, patch: NarrativePatch, *, project_id: str) -> None:
        op = str(patch.op)
        target_kind, target_id = _split_target_ref(patch.target_ref)
        if op == "create":
            payload = patch.new_value if isinstance(patch.new_value, dict) else {}
            if target_kind == "narrative_edge":
                self.add_edge(
                    NarrativeEdge.model_validate(
                        {
                            "id": target_id,
                            "project_id": project_id,
                            **payload,
                        }
                    )
                )
            else:
                self.add_node(
                    NarrativeNode.model_validate(
                        {
                            "id": target_id,
                            "project_id": project_id,
                            "node_type": _node_type_from_target(target_kind, payload),
                            **payload,
                        }
                    )
                )
            return

        if target_kind == "narrative_edge":
            edge = self.edges_by_id.get(target_id)
            if edge is None:
                return
            if op == "deactivate":
                self.edges_by_id.pop(target_id, None)
                return
            payload = edge.model_dump(mode="json")
            _apply_payload_patch(payload, patch.field_path, op, patch.new_value)
            self.edges_by_id[target_id] = NarrativeEdge.model_validate(payload)
            return

        node = self.nodes_by_id.get(target_id)
        if node is None:
            return
        payload = node.model_dump(mode="json")
        if op == "deactivate":
            payload["status"] = "inactive"
        else:
            _apply_payload_patch(payload, patch.field_path, op, patch.new_value)
        self.nodes_by_id[target_id] = NarrativeNode.model_validate(payload)

    def snapshot(self) -> dict[str, Any]:
        return {
            "nodes_by_id": {
                node_id: node.model_dump(mode="json")
                for node_id, node in self.nodes_by_id.items()
            },
            "edges_by_id": {
                edge_id: edge.model_dump(mode="json")
                for edge_id, edge in self.edges_by_id.items()
            },
            "active_world_line_ids": self.active_world_line_ids(),
            "open_gap_ids": self.open_gap_ids(),
        }


def _split_target_ref(target_ref: str) -> tuple[str, str]:
    if ":" not in target_ref:
        return "narrative_node", target_ref
    kind, target_id = target_ref.split(":", 1)
    return kind, target_id


def _node_type_from_target(target_kind: str, payload: dict[str, Any]) -> str:
    node_type = str(payload.get("node_type", "") or "")
    if node_type:
        return node_type
    if target_kind in NARRATIVE_NODE_TYPES:
        return target_kind
    return "plot_thread"


def _apply_payload_patch(payload: dict[str, Any], field_path: str, op: str, value: Any) -> None:
    if not field_path:
        if op in {"set", "replace"} and isinstance(value, dict):
            payload.clear()
            payload.update(value)
        elif op == "merge" and isinstance(value, dict):
            payload.update(value)
        return
    parts = [part for part in field_path.split(".") if part]
    cursor: dict[str, Any] = payload
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
        if not isinstance(current, dict):
            current = {}
        current.update(value)
        cursor[key] = current
    else:
        cursor[key] = value


__all__ = [
    "NARRATIVE_EDGE_TYPES",
    "NARRATIVE_NODE_TYPES",
    "NarrativeControlGraph",
]
