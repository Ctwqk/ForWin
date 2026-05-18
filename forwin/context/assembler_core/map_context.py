"""Context assembler - builds ChapterContextPack from current state."""
from __future__ import annotations
import json
import logging
import re
from typing import Any

from sqlalchemy import func, select

from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.models.project import ChapterPlan
from forwin.protocol.context import (
    ArcEnvelopeView,
    AudienceHintView,
    ChapterContextPack,
    NPCIntentView,
    TimelineSnapshot,
    WorldPressureView,
)
from forwin.characters.events import CHARACTER_INTEGRITY_CHECK_FAILED
from forwin.canon_quality.character_state import extract_candidate_character_names
from forwin.canon_quality.rule_profile import CanonGlossary
from forwin.governance import DecisionEventInfo
from forwin.observability.context import OperationContext
from forwin.observability.ports import NullObservability
from forwin.planning.world_contracts import WorldContractRepository
from forwin.state.updater import StateUpdater

logger = logging.getLogger(__name__)

_MAP_CONTEXT_NEIGHBOR_LIMIT = 8
_MAP_CONTEXT_REVIEW_GRAPH_NODE_LIMIT = 256
_MAP_CONTEXT_REVIEW_GRAPH_EDGE_LIMIT = 512


def _build_genesis_map_overview(map_atlas: dict, runtime_region_drafts: list[dict]) -> str:
    parts: list[str] = []
    overview = str(map_atlas.get("overview", "") or "").strip()
    if overview:
        parts.append(overview)
    submaps = map_atlas.get("submaps") if isinstance(map_atlas.get("submaps"), list) else []
    regions = map_atlas.get("regions") if isinstance(map_atlas.get("regions"), list) else []
    nodes = map_atlas.get("nodes") if isinstance(map_atlas.get("nodes"), list) else []
    if submaps:
        submap_names = [str(item.get("name", "") or "").strip() for item in submaps if isinstance(item, dict)]
        submap_names = [item for item in submap_names if item]
        if submap_names:
            parts.append(f"Genesis 小世界：{'、'.join(submap_names[:6])}")
    if regions:
        region_lines: list[str] = []
        for region in regions[:8]:
            if not isinstance(region, dict):
                continue
            name = str(region.get("name", "") or "").strip()
            if not name:
                continue
            subworld_name = str(region.get("subworld_name", "") or "").strip()
            level = str(region.get("level", "") or "").strip()
            region_lines.append(f"{name}{f'@{subworld_name}' if subworld_name else ''}{f'·L{level}' if level else ''}")
        if region_lines:
            parts.append(f"Genesis 地区：{'、'.join(region_lines)}")
    if nodes:
        node_lines: list[str] = []
        for node in nodes[:8]:
            if not isinstance(node, dict):
                continue
            name = str(node.get("name", "") or "").strip()
            if not name:
                continue
            parent_region = str(node.get("parent_region_id", "") or "").strip()
            node_lines.append(f"{name}{f'@{parent_region}' if parent_region else ''}")
        if node_lines:
            parts.append(f"Genesis 地点：{'、'.join(node_lines)}")
    if runtime_region_drafts:
        draft_lines: list[str] = []
        for draft in runtime_region_drafts[:8]:
            if not isinstance(draft, dict):
                continue
            name = str(draft.get("name", "") or "").strip()
            if not name:
                continue
            subworld_name = str(draft.get("subworld_name", "") or "").strip()
            level = str(draft.get("level", "") or "").strip()
            draft_lines.append(f"{name}{f'@{subworld_name}' if subworld_name else ''}{f'·L{level}' if level else ''}")
        if draft_lines:
            parts.append(f"运行时地区草案：{'、'.join(draft_lines)}")
    return "；".join(part for part in parts if part)


def _visible_map_edge(edge) -> bool:
    from forwin.map.visibility import is_writer_visible_map_edge

    return is_writer_visible_map_edge(edge)


def _resolve_map_node_id(
    raw_location: str,
    *,
    node_by_id: dict,
    node_id_by_source: dict[str, str],
    node_id_by_name: dict[str, str],
) -> str:
    text = str(raw_location or "").strip()
    if not text:
        return ""
    if text in node_by_id:
        return text
    if text in node_id_by_source:
        return node_id_by_source[text]
    return node_id_by_name.get(text, "")


def _visible_neighbors(graph: MapGraph, location_id: str, node_by_id: dict) -> list[tuple[str, object]]:
    result: list[tuple[str, object]] = []
    for edge_id in graph.outgoing_edges.get(location_id, []):
        edge = graph.edges_by_id.get(edge_id)
        if edge is None or not _visible_map_edge(edge):
            continue
        if edge.to_node_id in node_by_id:
            result.append((edge.to_node_id, edge))
    return result


def _genesis_active_location_refs(story_engine: dict) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    core_cast = story_engine.get("core_cast") if isinstance(story_engine.get("core_cast"), list) else []
    for item in core_cast[:8]:
        if not isinstance(item, dict):
            continue
        location_ref = str(item.get("current_base", "") or item.get("home_location", "") or "").strip()
        if not location_ref:
            continue
        refs.append(
            {
                "entity_id": "",
                "entity_name": str(item.get("name", "") or "Genesis 核心角色").strip(),
                "location_ref": location_ref,
                "source": "genesis_story_engine",
            }
        )
    return refs


def _append_review_node_id(node_ids: list[str], seen: set[str], node_id: str, node_by_id: dict) -> None:
    if node_id in seen or node_id not in node_by_id:
        return
    if len(node_ids) >= _MAP_CONTEXT_REVIEW_GRAPH_NODE_LIMIT:
        return
    seen.add(node_id)
    node_ids.append(node_id)


def _review_graph_node_ids(
    *,
    node_by_id: dict,
    visible_node_ids: set[str],
    visible_edges: list,
    active_locations: list[dict],
    visible_anchor_nodes: list[dict],
    available: bool,
) -> list[str]:
    if available:
        return sorted(visible_node_ids)

    selected: list[str] = []
    seen: set[str] = set()
    for location in active_locations:
        if not isinstance(location, dict):
            continue
        _append_review_node_id(selected, seen, str(location.get("location_id", "") or ""), node_by_id)
        for key in ("nearby_nodes", "reachable_nodes"):
            for item in location.get(key, []) if isinstance(location.get(key), list) else []:
                if isinstance(item, dict):
                    _append_review_node_id(selected, seen, str(item.get("node_id", "") or ""), node_by_id)
    for anchor in visible_anchor_nodes:
        if isinstance(anchor, dict):
            _append_review_node_id(selected, seen, str(anchor.get("node_id", "") or ""), node_by_id)
    for edge in visible_edges:
        _append_review_node_id(selected, seen, edge.from_node_id, node_by_id)
        _append_review_node_id(selected, seen, edge.to_node_id, node_by_id)
        if len(selected) >= _MAP_CONTEXT_REVIEW_GRAPH_NODE_LIMIT:
            break
    return sorted(selected)


def _review_graph_edges(visible_edges: list, selected_node_ids: set[str], *, available: bool) -> list:
    if available:
        return visible_edges
    selected = [
        edge
        for edge in visible_edges
        if edge.from_node_id in selected_node_ids and edge.to_node_id in selected_node_ids
    ]
    return selected[:_MAP_CONTEXT_REVIEW_GRAPH_EDGE_LIMIT]


def _build_map_context(repo_session, project_id: str, entities: list, genesis_story_engine: dict | None = None) -> dict:
    from forwin.map.pathfinding import MapGraph
    from forwin.map.repository import MapRepository

    if repo_session is None:
        return {}
    try:
        repo = MapRepository(repo_session)
        nodes = repo.list_map_nodes(project_id)
        edges = repo.list_map_edges(project_id)
    except Exception:
        logger.warning("Failed to load map context.", exc_info=True)
        return {}
    if not nodes:
        return {}
    node_by_id = {node.id: node for node in nodes}
    node_id_by_source = {
        str(node.metadata.get("source_node_id", "") or ""): node.id
        for node in nodes
        if str(node.metadata.get("source_node_id", "") or "").strip()
    }
    node_id_by_name = {node.name: node.id for node in nodes if node.name}
    region_by_id = {region.id: region for region in repo.list_regions(project_id)}
    visible_edges = [edge for edge in edges if _visible_map_edge(edge)]
    visible_node_ids = {
        node_id
        for edge in visible_edges
        for node_id in (edge.from_node_id, edge.to_node_id)
        if node_id in node_by_id
    }
    graph = MapGraph(nodes=nodes, edges=visible_edges)
    active_locations: list[dict] = []
    for entity in entities:
        state = getattr(entity, "current_state", {}) or {}
        location_id = _resolve_map_node_id(
            str(state.get("location_id", "") or state.get("location", "") or ""),
            node_by_id=node_by_id,
            node_id_by_source=node_id_by_source,
            node_id_by_name=node_id_by_name,
        )
        if not location_id:
            continue
        visible_node_ids.add(location_id)
        node = node_by_id[location_id]
        neighbors = _visible_neighbors(graph, location_id, node_by_id)[:_MAP_CONTEXT_NEIGHBOR_LIMIT]
        active_locations.append(
            {
                "entity_id": getattr(entity, "entity_id", ""),
                "entity_name": getattr(entity, "name", ""),
                "location_id": location_id,
                "location_name": node.name,
                "region_id": node.region_id,
                "region_name": region_by_id.get(node.region_id).name if node.region_id in region_by_id else "",
                "nearby_nodes": [
                    {
                        "node_id": neighbor_id,
                        "name": node_by_id[neighbor_id].name,
                        "edge_id": edge.id,
                        "travel_time": edge.travel_time,
                        "risk_level": edge.risk_level,
                    }
                    for neighbor_id, edge in neighbors
                    if neighbor_id in node_by_id
                ],
                "reachable_nodes": [
                    {
                        "node_id": neighbor_id,
                        "name": node_by_id[neighbor_id].name,
                        "travel_time": edge.travel_time,
                    }
                    for neighbor_id, edge in neighbors
                    if neighbor_id in node_by_id
                ],
            }
        )
    seen_location_keys = {(item["entity_name"], item["location_id"]) for item in active_locations}
    for ref in _genesis_active_location_refs(genesis_story_engine or {}):
        location_id = _resolve_map_node_id(
            ref["location_ref"],
            node_by_id=node_by_id,
            node_id_by_source=node_id_by_source,
            node_id_by_name=node_id_by_name,
        )
        if not location_id:
            continue
        visible_node_ids.add(location_id)
        key = (ref["entity_name"], location_id)
        if key in seen_location_keys:
            continue
        node = node_by_id[location_id]
        neighbors = _visible_neighbors(graph, location_id, node_by_id)[:_MAP_CONTEXT_NEIGHBOR_LIMIT]
        active_locations.append(
            {
                "entity_id": "",
                "entity_name": ref["entity_name"],
                "location_id": location_id,
                "location_name": node.name,
                "region_id": node.region_id,
                "region_name": region_by_id.get(node.region_id).name if node.region_id in region_by_id else "",
                "source": ref["source"],
                "nearby_nodes": [
                    {
                        "node_id": neighbor_id,
                        "name": node_by_id[neighbor_id].name,
                        "edge_id": edge.id,
                        "travel_time": edge.travel_time,
                        "risk_level": edge.risk_level,
                    }
                    for neighbor_id, edge in neighbors
                    if neighbor_id in node_by_id
                ],
                "reachable_nodes": [
                    {
                        "node_id": neighbor_id,
                        "name": node_by_id[neighbor_id].name,
                        "travel_time": edge.travel_time,
                    }
                    for neighbor_id, edge in neighbors
                    if neighbor_id in node_by_id
                ],
            }
        )
        seen_location_keys.add(key)
    visible_anchor_nodes = [
        {
            "node_id": node.id,
            "name": node.name,
            "region_id": node.region_id,
            "region_name": region_by_id.get(node.region_id).name if node.region_id in region_by_id else "",
        }
        for node in nodes
        if str(node.metadata.get("node_role", "") or "") in {"anchor", "hub"}
        and str(node.status or "").lower() not in {"hidden", "destroyed", "inactive"}
    ][:12]
    review_graph_available = (
        len(visible_node_ids) <= _MAP_CONTEXT_REVIEW_GRAPH_NODE_LIMIT
        and len(visible_edges) <= _MAP_CONTEXT_REVIEW_GRAPH_EDGE_LIMIT
    )
    review_node_ids = _review_graph_node_ids(
        node_by_id=node_by_id,
        visible_node_ids=visible_node_ids,
        visible_edges=visible_edges,
        active_locations=active_locations,
        visible_anchor_nodes=visible_anchor_nodes,
        available=review_graph_available,
    )
    review_node_id_set = set(review_node_ids)
    review_edges = _review_graph_edges(
        visible_edges,
        review_node_id_set,
        available=review_graph_available,
    )
    review_graph = {
        "available": review_graph_available,
        "node_count": len(visible_node_ids),
        "edge_count": len(visible_edges),
        "reason": "" if review_graph_available else "map_context_graph_cap_exceeded",
        "map_nodes": _map_node_payloads([node_by_id[node_id] for node_id in review_node_ids]),
        "map_edges": [_map_edge_payload(edge) for edge in review_edges],
    }
    return {
        "active_locations": active_locations,
        "visible_anchor_nodes": visible_anchor_nodes,
        "map_node_count": len(nodes),
        "map_edge_count": len(edges),
        "visible_map_node_count": len(visible_node_ids),
        "visible_map_edge_count": len(visible_edges),
        "regions": [
            {
                "id": region.id,
                "subworld_id": region.subworld_id,
                "name": region.name,
                "region_type": str(region.region_type),
                "danger_level": region.danger_level,
                "status": region.status,
            }
            for region in sorted(region_by_id.values(), key=lambda item: item.id)[:32]
        ],
        "review_graph": review_graph,
    }


def _map_node_payloads(nodes) -> list[dict]:
    return [
        {
            "id": node.id,
            "project_id": node.project_id,
            "subworld_id": node.subworld_id,
            "region_id": node.region_id,
            "node_type": str(node.node_type),
            "name": node.name,
            "terrain": node.terrain,
            "culture_tag": node.culture_tag,
            "default_danger_level": node.default_danger_level,
            "access_level": node.access_level,
            "status": node.status,
        }
        for node in nodes
    ]


def _map_edge_payload(edge) -> dict:
    return {
        "id": edge.id,
        "project_id": edge.project_id,
        "subworld_id": edge.subworld_id,
        "from_node_id": edge.from_node_id,
        "to_node_id": edge.to_node_id,
        "edge_type": str(edge.edge_type),
        "bidirectional": edge.bidirectional,
        "distance": edge.distance,
        "travel_time": edge.travel_time,
        "travel_cost": edge.travel_cost,
        "risk_level": edge.risk_level,
        "narrative_cost": edge.narrative_cost,
        "status": edge.status,
        "discovered_by_default": edge.discovered_by_default,
        "visibility_default": edge.visibility_default,
    }


__all__ = [
    '_build_genesis_map_overview',
    '_visible_map_edge',
    '_resolve_map_node_id',
    '_visible_neighbors',
    '_genesis_active_location_refs',
    '_append_review_node_id',
    '_review_graph_node_ids',
    '_review_graph_edges',
    '_build_map_context',
    '_map_node_payloads',
    '_map_edge_payload',
]
