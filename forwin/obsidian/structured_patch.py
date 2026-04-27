from __future__ import annotations

import json
import re
from typing import Any

from forwin.book_state.projection import BookStateProjection
from forwin.models.base import new_id
from forwin.models.world_model import WorldEditProposalRow
from forwin.protocol.book_state import (
    EdgePatch,
    FactPatch,
    GraphDelta,
    GraphDeltaType,
    MapPatch,
    NodePatch,
    WORLD_EDGE_TYPES_BY_FAMILY,
)
from forwin.world_model.store import load_json


_PATCH_BLOCK_RE = re.compile(
    r"```(?:json\s+)?forwin-patch\s*(.*?)```",
    flags=re.IGNORECASE | re.DOTALL,
)


def proposal_to_graph_delta(session, row: WorldEditProposalRow, *, reason: str = "") -> GraphDelta:
    payload = load_json(row.proposed_patch_json, {})
    operations = _extract_patch_operations(payload)
    if not operations:
        return audit_delta_from_proposal(row, reason=reason)
    runtime = BookStateProjection(session).load_runtime_as_of(
        row.project_id,
        as_of_chapter=max(_proposal_chapter(row), 0),
    )
    source_refs = _source_refs(row)
    node_patches: list[NodePatch] = []
    edge_patches: list[EdgePatch] = []
    fact_patches: list[FactPatch] = []
    map_patches: list[MapPatch] = []

    for operation in operations:
        op = str(operation.get("op") or "").strip()
        if not op:
            raise ValueError("forwin-patch operation is missing op")
        if op in {"set_node_field", "merge_node_metadata", "rename_node", "append_alias", "append_manual_note"}:
            node_patches.append(_node_patch(operation, row, runtime, reason=reason))
        elif op in {"create_edge", "set_edge_field", "deactivate_edge"}:
            edge_patches.append(_edge_patch(operation, row, runtime, reason=reason, source_refs=source_refs))
        elif op in {"create_map_node", "set_map_node_field", "create_map_edge", "set_map_edge_field", "deactivate_map_edge"}:
            map_patches.append(_map_patch(operation, row, runtime, reason=reason, source_refs=source_refs))
        elif op in {"create_fact", "set_fact_field"}:
            fact_patches.append(_fact_patch(operation, row, runtime, reason=reason, source_refs=source_refs))
        else:
            raise ValueError(f"unsupported forwin-patch op: {op}")

    return GraphDelta(
        id=f"obsidian_delta_{row.id}_{new_id()}",
        project_id=row.project_id,
        chapter_number=_proposal_chapter(row),
        delta_type=GraphDeltaType.REPAIR,
        operation="structured_proposal_patch",
        target_type="obsidian_proposal",
        target_id=row.id,
        source_type="obsidian_proposal",
        source_id=row.id,
        summary=f"Approved structured Obsidian proposal {row.id}",
        node_patches=node_patches,
        edge_patches=edge_patches,
        fact_patches=fact_patches,
        map_patches=map_patches,
        evidence_refs=source_refs,
        review_verdict_id=f"obsidian_proposal_review_{row.id}",
        allowed_for_canon=True,
        metadata={
            "source": "obsidian",
            "proposal_id": row.id,
            "proposal_type": getattr(row, "proposal_type", "") or "",
            "target_page_key": row.target_page_key,
            "target_node_id": getattr(row, "target_node_id", "") or "",
            "structured_patch_count": len(operations),
        },
    )


def audit_delta_from_proposal(row: WorldEditProposalRow, *, reason: str = "") -> GraphDelta:
    payload = load_json(row.proposed_patch_json, {})
    new_value = payload.get("new_value", "")
    target_node_id = getattr(row, "target_node_id", "") or ""
    proposal_type = getattr(row, "proposal_type", "") or "CanonCorrectionProposal"
    field_name = row.target_field or str(payload.get("field", ""))
    proposition = (
        f"Approved {proposal_type} for {row.target_page_key}"
        f"{f' / {field_name}' if field_name else ''}: {str(new_value).strip()[:500]}"
    )
    source_refs = _source_refs(row)
    return GraphDelta(
        id=f"obsidian_delta_{row.id}_{new_id()}",
        project_id=row.project_id,
        chapter_number=_proposal_chapter(row),
        delta_type=GraphDeltaType.REPAIR,
        operation="create_fact",
        target_type="proposal",
        target_id=row.id,
        source_type="obsidian_proposal",
        source_id=row.id,
        summary=proposition,
        fact_patches=[
            FactPatch(
                fact_id=f"proposal_fact_{row.id}",
                op="create",
                proposition=proposition,
                truth_value="proposal_approved",
                related_refs=[ref for ref in [f"node:{target_node_id}" if target_node_id else ""] if ref],
                reason=reason or row.reason,
                new_value={
                    "project_id": row.project_id,
                    "proposition": proposition,
                    "fact_type": "world_edit_proposal",
                    "truth_value": "proposal_approved",
                    "confidence": 1.0,
                    "related_node_refs": [target_node_id] if target_node_id else [],
                    "source_refs": source_refs,
                    "created_at_chapter": _proposal_chapter(row),
                    "narrative_function": proposal_type,
                    "state": {
                        "target_page_key": row.target_page_key,
                        "target_field": field_name,
                        "proposal_type": proposal_type,
                        "patch": payload,
                    },
                },
            )
        ],
        evidence_refs=source_refs,
        review_verdict_id=f"obsidian_proposal_review_{row.id}",
        allowed_for_canon=True,
        metadata={
            "source": "obsidian",
            "proposal_id": row.id,
            "proposal_type": proposal_type,
            "target_page_key": row.target_page_key,
            "target_node_id": target_node_id,
            "proposed_patch": json.dumps(payload, ensure_ascii=False, sort_keys=True),
        },
    )


def _extract_patch_operations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[Any] = [
        payload.get("forwin_patch"),
        payload.get("structured_patch"),
        payload.get("proposed_patch"),
    ]
    for key in ("new_value", "human_notes", "reason"):
        value = payload.get(key)
        if isinstance(value, str):
            candidates.extend(_parse_fenced_blocks(value))
    for candidate in candidates:
        parsed = _normalize_operations(candidate)
        if parsed:
            return parsed
    return []


def _parse_fenced_blocks(text: str) -> list[Any]:
    parsed: list[Any] = []
    for match in _PATCH_BLOCK_RE.finditer(text or ""):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            parsed.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in forwin-patch block: {exc}") from exc
    return parsed


def _normalize_operations(candidate: Any) -> list[dict[str, Any]]:
    if candidate in (None, "", []):
        return []
    if isinstance(candidate, dict) and isinstance(candidate.get("patches"), list):
        candidate = candidate["patches"]
    elif isinstance(candidate, dict) and candidate.get("op"):
        candidate = [candidate]
    if not isinstance(candidate, list):
        return []
    operations = []
    for item in candidate:
        if not isinstance(item, dict):
            raise ValueError("forwin-patch entries must be objects")
        operations.append(dict(item))
    return operations


def _node_patch(operation: dict[str, Any], row: WorldEditProposalRow, runtime, *, reason: str) -> NodePatch:
    op = str(operation.get("op") or "")
    node_id = str(operation.get("node_id") or getattr(row, "target_node_id", "") or "")
    if not node_id:
        raise ValueError(f"{op} requires node_id")
    node = runtime.world.nodes_by_id.get(node_id)
    if node is None:
        raise ValueError(f"unknown BookState node_id: {node_id}")
    field_path = str(operation.get("field_path") or "")
    patch_op = "set"
    new_value = operation.get("new_value")
    if op == "merge_node_metadata":
        field_path = "metadata"
        patch_op = "merge"
        new_value = operation.get("metadata", operation.get("new_value", {}))
        if not isinstance(new_value, dict):
            raise ValueError("merge_node_metadata requires an object metadata/new_value")
    elif op == "rename_node":
        field_path = "name"
        new_value = operation.get("name", operation.get("new_value"))
    elif op == "append_alias":
        field_path = "aliases"
        patch_op = "append"
        new_value = operation.get("alias", operation.get("new_value"))
    elif op == "append_manual_note":
        field_path = "metadata.manual_notes"
        patch_op = "append"
        new_value = operation.get("text", operation.get("new_value"))
    if not field_path:
        raise ValueError(f"{op} requires field_path")
    return NodePatch(
        node_id=node_id,
        node_type=node.node_type,
        op=patch_op,
        field_path=field_path,
        old_value=operation.get("old_value", _get_path(node.model_dump(mode="json"), field_path)),
        new_value=new_value,
        reason=reason or str(operation.get("reason") or row.reason or ""),
        visibility_default=str(operation.get("visibility_default") or node.metadata.get("visibility") or "visible"),
    )


def _edge_patch(
    operation: dict[str, Any],
    row: WorldEditProposalRow,
    runtime,
    *,
    reason: str,
    source_refs: list[str],
) -> EdgePatch:
    op = str(operation.get("op") or "")
    if op == "create_edge":
        edge_id = str(operation.get("edge_id") or f"edge_{new_id()}")
        edge_type = str(operation.get("edge_type") or "")
        source_id = str(operation.get("source_id") or "")
        target_id = str(operation.get("target_id") or "")
        edge_family = str(operation.get("edge_family") or _infer_edge_family(edge_type))
        if not source_id or not target_id or not edge_type:
            raise ValueError("create_edge requires source_id, target_id, and edge_type")
        return EdgePatch(
            edge_id=edge_id,
            op="create",
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_type,
            edge_family=edge_family,
            new_value={
                "project_id": row.project_id,
                "source_id": source_id,
                "target_id": target_id,
                "edge_type": edge_type,
                "edge_family": edge_family,
                "directionality": operation.get("directionality", "directed"),
                "weight": operation.get("weight", 1.0),
                "confidence": operation.get("confidence", 1.0),
                "established_at_chapter": _proposal_chapter(row),
                "valid_from_chapter": _proposal_chapter(row),
                "status": operation.get("status", "active"),
                "visibility": operation.get("visibility", ""),
                "truth_relation": operation.get("truth_relation", "true"),
                "source_refs": source_refs,
                "metadata": operation.get("metadata", {}),
            },
            reason=reason or str(operation.get("reason") or row.reason or ""),
        )
    edge_id = str(operation.get("edge_id") or "")
    edge = runtime.world.edges_by_id.get(edge_id)
    if edge is None:
        raise ValueError(f"unknown BookState edge_id: {edge_id}")
    if op == "deactivate_edge":
        return EdgePatch(
            edge_id=edge_id,
            op="deactivate",
            source_id=edge.source_id,
            target_id=edge.target_id,
            edge_type=edge.edge_type,
            edge_family=edge.edge_family,
            old_value=operation.get("old_value"),
            reason=reason or str(operation.get("reason") or row.reason or ""),
        )
    field_path = str(operation.get("field_path") or "")
    if not field_path:
        raise ValueError("set_edge_field requires field_path")
    return EdgePatch(
        edge_id=edge_id,
        op="set",
        source_id=edge.source_id,
        target_id=edge.target_id,
        edge_type=edge.edge_type,
        edge_family=edge.edge_family,
        field_path=field_path,
        old_value=operation.get("old_value", _get_path(edge.model_dump(mode="json"), field_path)),
        new_value=operation.get("new_value"),
        reason=reason or str(operation.get("reason") or row.reason or ""),
    )


def _map_patch(
    operation: dict[str, Any],
    row: WorldEditProposalRow,
    runtime,
    *,
    reason: str,
    source_refs: list[str],
) -> MapPatch:
    op = str(operation.get("op") or "")
    if op == "create_map_node":
        target_id = str(operation.get("node_id") or operation.get("target_id") or f"map_node_{new_id()}")
        node_type = str(operation.get("node_type") or "site")
        return MapPatch(
            target_type="map_node",
            target_id=target_id,
            op="create",
            new_value={
                "id": target_id,
                "project_id": row.project_id,
                "node_type": node_type,
                "name": operation.get("name", ""),
                "subworld_id": operation.get("subworld_id", ""),
                "region_id": operation.get("region_id", ""),
                "status": operation.get("status", "normal"),
                "metadata": {"source_refs": source_refs, **operation.get("metadata", {})},
            },
            reason=reason or str(operation.get("reason") or row.reason or ""),
            visibility_default=str(operation.get("visibility_default") or "visible"),
        )
    if op == "create_map_edge":
        target_id = str(operation.get("edge_id") or operation.get("target_id") or f"map_edge_{new_id()}")
        from_node_id = str(operation.get("from_node_id") or "")
        to_node_id = str(operation.get("to_node_id") or "")
        edge_type = str(operation.get("edge_type") or "road")
        if not from_node_id or not to_node_id:
            raise ValueError("create_map_edge requires from_node_id and to_node_id")
        return MapPatch(
            target_type="map_edge",
            target_id=target_id,
            op="create",
            new_value={
                "id": target_id,
                "project_id": row.project_id,
                "from_node_id": from_node_id,
                "to_node_id": to_node_id,
                "edge_type": edge_type,
                "bidirectional": bool(operation.get("bidirectional", False)),
                "distance": operation.get("distance", 0.0),
                "travel_time": operation.get("travel_time", 0.0),
                "travel_cost": operation.get("travel_cost", 0.0),
                "risk_level": operation.get("risk_level", 0.0),
                "status": operation.get("status", "open"),
                "discovered_by_default": bool(operation.get("discovered_by_default", True)),
                "visibility_default": str(operation.get("visibility_default") or "visible"),
                "metadata": {"source_refs": source_refs, **operation.get("metadata", {})},
            },
            reason=reason or str(operation.get("reason") or row.reason or ""),
        )
    target_type = "map_node" if op == "set_map_node_field" else "map_edge"
    target_id = str(operation.get("target_id") or operation.get("node_id") or operation.get("edge_id") or "")
    current = (
        runtime.map.nodes_by_id.get(target_id)
        if target_type == "map_node"
        else runtime.map.edges_by_id.get(target_id)
    )
    if current is None:
        raise ValueError(f"unknown BookState {target_type}: {target_id}")
    if op == "deactivate_map_edge":
        return MapPatch(
            target_type=target_type,
            target_id=target_id,
            op="deactivate",
            old_value=operation.get("old_value"),
            reason=reason or str(operation.get("reason") or row.reason or ""),
        )
    field_path = str(operation.get("field_path") or "")
    if not field_path:
        raise ValueError(f"{op} requires field_path")
    return MapPatch(
        target_type=target_type,
        target_id=target_id,
        op="set",
        field_path=field_path,
        old_value=operation.get("old_value", _get_path(current.model_dump(mode="json"), field_path)),
        new_value=operation.get("new_value"),
        reason=reason or str(operation.get("reason") or row.reason or ""),
        visibility_default=str(operation.get("visibility_default") or getattr(current, "visibility_default", "visible")),
    )


def _fact_patch(
    operation: dict[str, Any],
    row: WorldEditProposalRow,
    runtime,
    *,
    reason: str,
    source_refs: list[str],
) -> FactPatch:
    op = str(operation.get("op") or "")
    if op == "create_fact":
        fact_id = str(operation.get("fact_id") or f"fact_{new_id()}")
        proposition = str(operation.get("proposition") or operation.get("new_value") or "")
        if not proposition:
            raise ValueError("create_fact requires proposition")
        return FactPatch(
            fact_id=fact_id,
            op="create",
            proposition=proposition,
            truth_value=str(operation.get("truth_value") or "true"),
            related_refs=list(operation.get("related_refs", [])),
            reason=reason or str(operation.get("reason") or row.reason or ""),
            sensitivity_level=str(operation.get("sensitivity_level") or ""),
            new_value={
                "project_id": row.project_id,
                "proposition": proposition,
                "fact_type": operation.get("fact_type", ""),
                "truth_value": operation.get("truth_value", "true"),
                "confidence": operation.get("confidence", 1.0),
                "related_node_refs": operation.get("related_node_refs", []),
                "related_edge_refs": operation.get("related_edge_refs", []),
                "source_refs": source_refs,
                "created_at_chapter": _proposal_chapter(row),
                "sensitivity_level": operation.get("sensitivity_level", ""),
                "narrative_function": operation.get("narrative_function", ""),
                "state": operation.get("state", {}),
                "metadata": operation.get("metadata", {}),
            },
        )
    fact_id = str(operation.get("fact_id") or "")
    fact = runtime.world.facts_by_id.get(fact_id)
    if fact is None:
        raise ValueError(f"unknown BookState fact_id: {fact_id}")
    field_path = str(operation.get("field_path") or "")
    if not field_path:
        raise ValueError("set_fact_field requires field_path")
    return FactPatch(
        fact_id=fact_id,
        op="set",
        field_path=field_path,
        old_value=operation.get("old_value", _get_path(fact.model_dump(mode="json"), field_path)),
        new_value=operation.get("new_value"),
        reason=reason or str(operation.get("reason") or row.reason or ""),
        proposition=fact.proposition,
        truth_value=fact.truth_value,
        sensitivity_level=fact.sensitivity_level,
    )


def _proposal_chapter(row: WorldEditProposalRow) -> int:
    payload = load_json(row.proposed_patch_json, {})
    frontmatter = payload.get("frontmatter") if isinstance(payload.get("frontmatter"), dict) else {}
    try:
        return int(frontmatter.get("as_of_chapter") or 0)
    except (TypeError, ValueError):
        return 0


def _source_refs(row: WorldEditProposalRow) -> list[str]:
    refs = [f"obsidian_proposal:{row.id}", f"obsidian_page:{row.target_page_key}"]
    target_node_id = getattr(row, "target_node_id", "") or ""
    if target_node_id:
        refs.append(f"node:{target_node_id}")
    return refs


def _infer_edge_family(edge_type: str) -> str:
    for family, edge_types in WORLD_EDGE_TYPES_BY_FAMILY.items():
        if edge_type in edge_types:
            return family
    return ""


def _get_path(payload: dict[str, Any], field_path: str) -> Any:
    if not field_path:
        return payload
    cursor: Any = payload
    for part in [item for item in field_path.split(".") if item]:
        if isinstance(cursor, dict):
            cursor = cursor.get(part)
        else:
            return None
    return cursor
