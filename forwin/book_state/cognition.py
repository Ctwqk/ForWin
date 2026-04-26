from __future__ import annotations

from typing import Any

from forwin.protocol.book_state import CognitionOverlay, FactNode, MapEdge, WorldEdge, WorldNode


class CognitionView:
    """Sparse observer overlay over objective world/map state."""

    def __init__(self, overlay: CognitionOverlay) -> None:
        self.overlay = overlay
        self.observer_type = str(overlay.observer_type)
        self.observer_id = overlay.observer_id
        self.as_of_chapter = overlay.as_of_chapter
        self.visible_refs = set(overlay.visible_refs)
        self.hidden_refs = set(overlay.hidden_refs)
        self.suspected_refs = set(overlay.suspected_refs)
        self.confirmed_refs = set(overlay.confirmed_refs)
        self.field_overrides = dict(overlay.field_overrides)
        self.false_nodes: dict[str, WorldNode] = dict(overlay.false_nodes)
        self.false_edges: dict[str, WorldEdge | MapEdge] = dict(overlay.false_edges)
        self.false_facts: dict[str, FactNode] = dict(overlay.false_facts)
        self.evidence_by_ref: dict[str, list[str]] = dict(overlay.evidence_by_ref)

    @property
    def key(self) -> tuple[str, str]:
        return (self.observer_type, self.observer_id)

    def can_see(self, ref: str) -> bool:
        if ref in self.hidden_refs:
            return False
        if ref in self.visible_refs or ref in self.confirmed_refs or ref in self.suspected_refs:
            return True
        return True

    def get_belief(self, ref: str) -> str:
        if ref in self.confirmed_refs:
            return "confirmed"
        if ref in self.suspected_refs:
            return "suspected"
        if ref in self.visible_refs:
            return "known"
        if ref in self.hidden_refs:
            return "hidden"
        return "unknown"

    def field_ref(self, target_id: str, field_path: str) -> str:
        return f"field:{target_id}:{field_path}"

    def get_field_override(self, target_id: str, field_path: str, default: Any = None) -> Any:
        return self.field_overrides.get(self.field_ref(target_id, field_path), default)

    def get_visible_state(self, node_id: str, objective_state: dict[str, Any]) -> dict[str, Any]:
        visible = dict(objective_state)
        prefix = f"field:{node_id}:"
        for ref, value in self.field_overrides.items():
            if not ref.startswith(prefix):
                continue
            field_path = ref.removeprefix(prefix)
            _set_path(visible, field_path, value)
        return visible

    def apply_cognition_patch(self, field_path: str, op: str, value: Any) -> None:
        target = getattr(self, field_path, None)
        if isinstance(target, set):
            if op in {"append", "create", "set"}:
                target.add(str(value))
            elif op == "remove":
                target.discard(str(value))
            return
        if field_path == "field_overrides" and isinstance(value, dict):
            if op == "merge":
                self.field_overrides.update(value)
            elif op in {"set", "replace"}:
                self.field_overrides = dict(value)
            return
        if field_path in {"false_nodes", "false_edges", "false_facts", "evidence_by_ref"} and isinstance(value, dict):
            current = getattr(self, field_path)
            if op == "merge":
                current.update(value)
            elif op in {"set", "replace"}:
                setattr(self, field_path, dict(value))

    def diff_against_objective(self) -> dict[str, Any]:
        return {
            "hidden_refs": sorted(self.hidden_refs),
            "suspected_refs": sorted(self.suspected_refs),
            "confirmed_refs": sorted(self.confirmed_refs),
            "field_overrides": dict(self.field_overrides),
            "false_nodes": sorted(self.false_nodes),
            "false_edges": sorted(self.false_edges),
            "false_facts": sorted(self.false_facts),
        }


def _set_path(payload: dict[str, Any], field_path: str, value: Any) -> None:
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
    cursor[parts[-1]] = value
