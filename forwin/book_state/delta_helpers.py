"""Shared patch construction for chapter extraction and world ticks."""

from __future__ import annotations

from hashlib import sha256
from typing import Any
from collections.abc import Callable

from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
from forwin.protocol.book_state import WORLD_EDGE_TYPES_BY_FAMILY, Directionality, EdgePatch, WorldEdge

OBSERVER_TYPES = {"character": "character", "faction": "faction", "organization": "group", "group": "group"}
SYMMETRIC_EDGE_TYPES = frozenset(
    {"family_of", "kin_of", "ally_of", "enemy_of", "rival_of", "romantic_with", "allied_with"}
)
EDGE_SET_FIELDS = frozenset({"weight", "confidence", "status", "visibility", "truth_relation", "visibility_default"})
EDGE_SET_PREFIXES = ("state.", "metadata.")
INTENT_STATUS_BY_EFFECT = {"disrupted": "disrupted", "fulfilled": "resolved"}


def stable_id(project_id: str, kind: str, name: str) -> str:
    """Hyphenated ``kind-<20 hex>`` id, the same scheme as the world seed's ``seed_ref_id``."""
    digest = sha256(f"{project_id}\0{kind}\0{name}".encode()).hexdigest()[:20]
    return f"{kind}-{digest}"


def leaf_patch(payload: dict[str, Any], field_path: str, value: Any) -> tuple[str, str, Any, Any] | None:
    """(op, field_path, old_value, new_value) setting one leaf of ``payload``.

    A leaf without a value yet merges into its parent dict, whose current value is the
    old_value rewind restores; None when that parent does not exist either.
    """
    old_value = get_path(payload, field_path)
    if old_value is not None:
        return "set", field_path, old_value, value
    parent, _, leaf = field_path.rpartition(".")
    current = get_path(payload, parent) if parent else None
    if not isinstance(current, dict):
        return None
    return "merge", parent, dict(current), {leaf: value}


def edge_field_path(field: str) -> str | None:
    text = field.strip()
    if text == "appellation":
        return "metadata.appellation"
    if text in EDGE_SET_FIELDS or (text.startswith(EDGE_SET_PREFIXES) and text.split(".", 1)[1]):
        return text
    return None


def current_node_value(runtime: BookStateRuntime, node_id: str, field_path: str) -> Any:
    node = runtime.world.nodes_by_id.get(node_id)
    if node is None:
        return None
    if field_path.startswith("state."):
        return get_path(runtime.world.get_state(node_id), field_path.removeprefix("state."))
    return get_path(node.model_dump(mode="json"), field_path)


def get_path(payload: Any, field_path: str) -> Any:
    for part in field_path.split("."):
        if not isinstance(payload, dict):
            return None
        payload = payload.get(part)
    return payload


class RelationshipPlanner:
    """Plan relation patches using the caller's source, endpoints and issue context."""

    def __init__(
        self, *, project_id: str, chapter: int, world: ObjectiveWorldGraph, edge_patches: list[EdgePatch],
        source: dict[str, Any], operation: str, reason: str, evidence_ref: str,
        resolve_endpoint: Callable[[str, str], str | None],
        issue: Callable[[str, str, str, str], None],
    ) -> None:
        self.project_id = project_id
        self.chapter = chapter
        self.world = world
        self.edge_patches = edge_patches
        self.operation = operation
        self.reason = reason
        self.evidence_ref = evidence_ref
        self.metadata = source
        self._relation_endpoint = resolve_endpoint
        self._issue = issue

    def plan(self, change: Any, path: str = "") -> None:
        families = [family for family, edge_types in WORLD_EDGE_TYPES_BY_FAMILY.items() if change.edge_type in edge_types]
        if len(families) != 1:
            self._issue("error", "edge_type_invalid", f"{path}.edge_type", f"{change.edge_type!r} has no unique edge family")
            return
        source_id = self._relation_endpoint(change.source_ref, f"{path}.source_ref")
        target_id = self._relation_endpoint(change.target_ref, f"{path}.target_ref")
        if not source_id or not target_id:
            return
        symmetric = change.edge_type in SYMMETRIC_EDGE_TYPES
        same_type = sorted(
            (edge for edge in self.world.edges_by_id.values() if edge.edge_type == change.edge_type),
            key=lambda edge: edge.id,
        )
        forward = [edge for edge in same_type if (edge.source_id, edge.target_id) == (source_id, target_id)]
        backward = [edge for edge in same_type if (edge.source_id, edge.target_id) == (target_id, source_id)]
        matching = forward + backward if symmetric else forward
        active = next((edge for edge in matching if edge.is_active), None)
        if self.operation == "create":
            # A create earlier in this delta counts as existing too.
            pairs = {(source_id, target_id), (target_id, source_id)} if symmetric else {(source_id, target_id)}
            pending = next(
                (
                    patch for patch in self.edge_patches
                    if str(patch.op) == "create" and patch.edge_type == change.edge_type
                    and (patch.source_id, patch.target_id) in pairs
                ),
                None,
            )
            existing_id = active.id if active is not None else pending.edge_id if pending is not None else ""
            if existing_id:
                self._issue("warning", "relation_exists", path, f"relation {existing_id} already exists")
            elif not symmetric and not forward and any(edge.is_active for edge in backward):
                # An ended reverse edge is a past role; only a live one blocks the flip.
                self._direction_mismatch(change, path)
            else:
                self._create_relation(change, matching, source_id, target_id, families[0])
            return
        if active is None:
            if not symmetric and any(edge.is_active for edge in backward):
                self._direction_mismatch(change, path)
            else:
                self._issue("warning", "relation_missing", path, f"no active {change.edge_type} relation to {self.operation}")
            return
        endpoints = {
            "source_id": active.source_id, "target_id": active.target_id,
            "edge_type": active.edge_type, "edge_family": str(active.edge_family),
        }
        if self.operation == "end":
            self.edge_patches.append(EdgePatch(
                edge_id=active.id, op="deactivate", **endpoints,
                field_path="is_active", old_value=True, reason=self.reason,
            ))
            return
        field_path = edge_field_path(change.field)
        if field_path is None:
            self._issue("error", "unknown_field", f"{path}.field", f"{change.field!r} is not a settable relation field")
            return
        leaf = leaf_patch(active.model_dump(mode="json"), field_path, change.new_value)
        if leaf is None:
            self._issue("error", "missing_old_value", f"{path}.field", f"{active.id}:{field_path} has no current value or parent")
            return
        op, patch_path, old_value, new_value = leaf
        self.edge_patches.append(EdgePatch(
            edge_id=active.id, op=op, **endpoints, field_path=patch_path,
            old_value=old_value, new_value=new_value, reason=self.reason,
        ))

    def _create_relation(
        self, change: Any, matching: list[WorldEdge], source_id: str, target_id: str, edge_family: str,
    ) -> None:
        ended = [edge for edge in matching if edge.ended_at_chapter is not None]
        # A create on an ended relation reactivates that same edge.
        symmetric = change.edge_type in SYMMETRIC_EDGE_TYPES
        # A symmetric relation has one id whichever endpoint comes first.
        first, second = sorted((source_id, target_id)) if symmetric else (source_id, target_id)
        edge_id = (
            max(ended, key=lambda edge: (edge.ended_at_chapter, edge.id)).id
            if ended
            else stable_id(self.project_id, "edge", f"{first}/{change.edge_type}/{second}")
        )
        if any(patch.edge_id == edge_id for patch in self.edge_patches):
            return
        endpoints = {
            "source_id": source_id, "target_id": target_id,
            "edge_type": change.edge_type, "edge_family": edge_family,
        }
        directionality = Directionality.UNDIRECTED if symmetric else Directionality.DIRECTED
        self.edge_patches.append(EdgePatch(
            edge_id=edge_id,
            op="create",
            **endpoints,
            new_value={
                "project_id": self.project_id,
                **endpoints,
                "directionality": directionality.value,
                "established_at_chapter": self.chapter,
                "valid_from_chapter": self.chapter,
                "evidence_refs": [self.evidence_ref],
                "metadata": self.metadata,
            },
            reason=self.reason,
        ))

    def _direction_mismatch(self, change: Any, path: str) -> None:
        self._issue(
            "warning", "relation_direction_mismatch", path,
            f"only the reverse {change.edge_type} relation exists; directed relations are not flipped",
        )


