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
from forwin.canon_names import extract_candidate_character_names
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
from .map_context import _visible_map_edge


def _book_state_context_overlay(
    repo_session,
    project_id: str,
    chapter_number: int,
) -> dict:
    from forwin.book_state import BookStateProjection, BookStateRepository

    if repo_session is None:
        return {}
    try:
        repository = BookStateRepository(repo_session)
        latest_chapter = repository.latest_available_chapter(project_id)
        as_of_chapter = max(0, min(int(latest_chapter or 0), int(chapter_number or 0) - 1))
        runtime = BookStateProjection(repo_session).load_runtime_as_of(
            project_id,
            as_of_chapter=as_of_chapter,
        )
    except Exception:
        logger.warning("Failed to load BookState context overlay.", exc_info=True)
        return {}

    map_nodes_by_id = runtime.map.nodes_by_id
    node_id_by_name = {node.name: node.id for node in map_nodes_by_id.values() if node.name}
    active_locations: list[dict] = []
    personality_characters: list[dict] = []
    character_nodes: list[dict] = []
    site_states: list[dict] = []
    for node in runtime.world.nodes_by_id.values():
        node_type = str(getattr(node, "node_type", "") or "")
        if node_type == "character":
            loadout = node.profile.get("personality_loadout") if isinstance(node.profile, dict) else None
            metadata = dict(node.metadata) if isinstance(node.metadata, dict) else {}
            character_nodes.append(
                {
                    "character_id": node.id,
                    "character_name": node.name,
                    "personality_loadout": loadout if isinstance(loadout, dict) else {},
                    "personality_assignment": metadata.get("personality_assignment")
                    if isinstance(metadata.get("personality_assignment"), dict)
                    else {},
                }
            )
            if isinstance(loadout, dict) and loadout:
                personality_characters.append(
                    {
                        "character_id": node.id,
                        "character_name": node.name,
                        "personality_loadout": loadout,
                    }
                )
            location_id = _resolve_book_state_location_id(
                node,
                runtime.world.get_state(node.id),
                map_nodes_by_id=map_nodes_by_id,
                node_id_by_name=node_id_by_name,
            )
            if not location_id:
                continue
            location = map_nodes_by_id.get(location_id)
            active_locations.append(
                {
                    "entity_id": node.id,
                    "entity_name": node.name,
                    "location_id": location_id,
                    "location_name": location.name if location else location_id,
                    "region_id": location.region_id if location else "",
                    "region_name": "",
                    "source": "book_state",
                    "nearby_nodes": _book_state_neighbors(runtime.map, location_id, map_nodes_by_id),
                    "reachable_nodes": [
                        {
                            "node_id": item["node_id"],
                            "name": item["name"],
                            "travel_time": item["travel_time"],
                        }
                        for item in _book_state_neighbors(runtime.map, location_id, map_nodes_by_id)
                    ],
                }
            )
        elif node_type == "site_state":
            map_node_id = str(node.profile.get("map_node_id", "") or "").strip()
            if not map_node_id:
                continue
            map_node = map_nodes_by_id.get(map_node_id)
            site_states.append(
                {
                    "site_state_id": node.id,
                    "site_state_name": node.name,
                    "map_node_id": map_node_id,
                    "map_node_name": map_node.name if map_node else map_node_id,
                    "source": "book_state",
                }
            )
    return {
        "as_of_chapter": as_of_chapter,
        "active_world_lines": runtime.narrative.active_world_line_ids(),
        "active_knowledge_gaps": runtime.narrative.open_gap_ids(),
        "active_locations": active_locations,
        "personality_characters": personality_characters,
        "character_nodes": character_nodes,
        "site_states": site_states,
    }


def _resolve_book_state_location_id(
    node: WorldNode,
    state: dict,
    *,
    map_nodes_by_id: dict[str, MapNode],
    node_id_by_name: dict[str, str],
) -> str:
    for raw in (
        state.get("location_id"),
        state.get("current_location_id"),
        node.profile.get("map_node_id"),
        node.profile.get("location_id"),
    ):
        text = str(raw or "").strip()
        if not text:
            continue
        if text in map_nodes_by_id:
            return text
        if text in node_id_by_name:
            return node_id_by_name[text]
    return ""


def _book_state_neighbors(graph, location_id: str, map_nodes_by_id: dict[str, MapNode]) -> list[dict]:
    neighbors: list[dict] = []
    for edge_id in graph.outgoing_edges.get(location_id, []):
        edge = graph.edges_by_id.get(edge_id)
        if edge is None or edge.to_node_id not in map_nodes_by_id or not _visible_map_edge(edge):
            continue
        node = map_nodes_by_id[edge.to_node_id]
        neighbors.append(
            {
                "node_id": node.id,
                "name": node.name,
                "edge_id": edge.id,
                "travel_time": edge.travel_time,
                "risk_level": edge.risk_level,
            }
        )
        if len(neighbors) >= _MAP_CONTEXT_NEIGHBOR_LIMIT:
            break
    return neighbors


def _merge_book_state_map_overlay(map_context: dict, overlay: dict) -> dict:
    if not overlay:
        return map_context
    merged = dict(map_context or {})
    active_locations = [
        item
        for item in merged.get("active_locations", [])
        if isinstance(item, dict)
    ]
    for overlay_location in overlay.get("active_locations", []):
        if not isinstance(overlay_location, dict):
            continue
        entity_id = str(overlay_location.get("entity_id", "") or "")
        entity_name = str(overlay_location.get("entity_name", "") or "")
        active_locations = [
            item
            for item in active_locations
            if not (
                (entity_id and str(item.get("entity_id", "") or "") == entity_id)
                or (entity_name and str(item.get("entity_name", "") or "") == entity_name)
            )
        ]
        active_locations.append(overlay_location)
    merged["active_locations"] = active_locations
    if overlay.get("site_states"):
        existing_site_states = [
            item
            for item in merged.get("site_states", [])
            if isinstance(item, dict)
        ]
        merged["site_states"] = [*existing_site_states, *overlay["site_states"]]
    if "as_of_chapter" in overlay:
        merged["book_state_as_of_chapter"] = overlay["as_of_chapter"]
    return merged


__all__ = [
    '_book_state_context_overlay',
    '_resolve_book_state_location_id',
    '_book_state_neighbors',
    '_merge_book_state_map_overlay',
]
