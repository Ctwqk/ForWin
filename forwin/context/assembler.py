"""Context assembler - builds ChapterContextPack from current state."""
from __future__ import annotations
import json
import logging
from typing import Any

from sqlalchemy import func, select

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
                    "legacy_entity_id": str(metadata.get("legacy_entity_id") or ""),
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
        state.get("location"),
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


def _project_personality_integrity_strict(project) -> bool:
    try:
        automation = json.loads(getattr(project, "automation_json", "{}") or "{}") or {}
    except (TypeError, ValueError, json.JSONDecodeError):
        automation = {}
    personality_policy = automation.get("character_personality") if isinstance(automation, dict) else {}
    if isinstance(personality_policy, dict) and "strict_integrity" in personality_policy:
        return bool(personality_policy.get("strict_integrity"))
    return str(getattr(project, "creation_status", "") or "legacy") != "legacy"


def _personality_integrity_issues(
    *,
    book_state_overlay: dict,
    allowed_entities: list[str],
    active_entities: list,
    library: CharacterPersonalityLibrary,
) -> list[dict[str, Any]]:
    from forwin.personality import PersonalityLoadoutAssigner

    allowed_names = {str(item or "").strip() for item in allowed_entities if str(item or "").strip()}
    allowed_ids = {
        str(getattr(item, "entity_id", "") or "").strip()
        for item in active_entities
        if str(getattr(item, "kind", "") or "") == "character" and str(getattr(item, "entity_id", "") or "").strip()
    }
    assigner = PersonalityLoadoutAssigner(library)
    issues: list[dict[str, Any]] = []
    for character in book_state_overlay.get("character_nodes", []):
        if not isinstance(character, dict):
            continue
        character_id = str(character.get("character_id") or "").strip()
        character_name = str(character.get("character_name") or "").strip()
        legacy_entity_id = str(character.get("legacy_entity_id") or "").strip()
        if not (
            character_id in allowed_ids
            or legacy_entity_id in allowed_ids
            or character_name in allowed_names
            or character_id in allowed_names
        ):
            continue
        loadout = character.get("personality_loadout") if isinstance(character.get("personality_loadout"), dict) else {}
        if not loadout:
            issues.append(
                {
                    "code": "personality_missing_loadout",
                    "severity": "error",
                    "character_id": character_id,
                    "character_name": character_name,
                    "message": "named character is missing personality_loadout",
                }
            )
            continue
        validation = assigner.validate(loadout)
        for error in validation.errors:
            issues.append(
                {
                    "code": error,
                    "severity": "error",
                    "character_id": character_id,
                    "character_name": character_name,
                    "message": error,
                }
            )
        for warning in validation.warnings:
            issues.append(
                {
                    "code": warning,
                    "severity": "warning",
                    "character_id": character_id,
                    "character_name": character_name,
                    "message": warning,
                }
            )
    return issues


def _save_personality_integrity_failure(repo_session, project_id: str, chapter_number: int, issues: list[dict[str, Any]]) -> None:
    if repo_session is None:
        return
    StateUpdater(repo_session).save_decision_event(
        DecisionEventInfo(
            project_id=project_id,
            chapter_number=int(chapter_number or 0),
            scope="character_creation",
            event_family="audit_action",
            event_type=CHARACTER_INTEGRITY_CHECK_FAILED,
            actor_type="system",
            summary="人物 personality_loadout integrity gate failed before writer context assembly.",
            reason="writer context assembly",
            payload={"issues": issues},
            related_object_type="project",
            related_object_id=project_id,
        )
    )


def _build_canon_quality_context(
    *,
    session,
    project_id: str,
    chapter_number: int,
    target_total_chapters: int,
    chapter_title: str = "",
    chapter_summary: str = "",
) -> dict[str, Any]:
    is_final_chapter = _is_final_chapter_for_context(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        target_total_chapters=target_total_chapters,
        title=chapter_title,
        summary=chapter_summary,
    )
    base = {
        "target_total_chapters": int(target_total_chapters or 0),
        "is_final_chapter": is_final_chapter,
        "countdown_constraints": [],
        "open_signals": [],
    }
    if session is None:
        return base
    try:
        from forwin.canon_quality.repository import CanonQualityRepository

        repo = CanonQualityRepository(session)
        entries = repo.list_countdown_entries(
            project_id,
            before_chapter=int(chapter_number or 0),
            include_details=True,
        )
        latest_by_key: dict[str, dict[str, Any]] = {}
        for entry in entries:
            key = str(entry.get("countdown_key") or "").strip()
            if not key:
                continue
            if bool(entry.get("is_resolution_event")) or str(entry.get("status") or "") == "resolved":
                latest_by_key.pop(key, None)
                continue
            remaining = entry.get("normalized_remaining_minutes")
            if remaining is None:
                continue
            latest_by_key[key] = entry
        countdown_constraints = [
            {
                "countdown_key": key,
                "label": str(item.get("label") or key),
                "latest_remaining_minutes": int(item.get("normalized_remaining_minutes") or 0),
                "latest_chapter": int(item.get("chapter_number") or 0),
                "raw_mention": str(item.get("raw_mention") or ""),
                "status": str(item.get("status") or ""),
            }
            for key, item in sorted(latest_by_key.items())
            if int(item.get("normalized_remaining_minutes") or 0) > 0
        ]
        open_signals = [
            {
                "signal_type": signal.signal_type,
                "severity": signal.severity,
                "chapter_number": signal.chapter_number,
                "subject_key": signal.subject_key,
                "description": signal.description,
            }
            for signal in repo.list_open_signals(project_id, before_chapter=chapter_number, limit=10)
        ]
        return {
            **base,
            "countdown_constraints": countdown_constraints,
            "open_signals": open_signals,
        }
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to build canon quality context for project %s chapter %s",
            project_id,
            chapter_number,
        )
        return base


def _is_final_chapter_for_context(
    *,
    session,
    project_id: str,
    chapter_number: int,
    target_total_chapters: int,
    title: str = "",
    summary: str = "",
) -> bool:
    current = int(chapter_number or 0)
    if current <= 0:
        return False
    target_total = int(target_total_chapters or 0)
    if target_total and current >= target_total:
        return True
    if session is None or not _looks_like_final_chapter_label(title=title, summary=summary):
        return False
    try:
        max_materialized = session.execute(
            select(func.max(ChapterPlan.chapter_number)).where(ChapterPlan.project_id == project_id)
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to infer final chapter context for project %s chapter %s",
            project_id,
            chapter_number,
        )
        return False
    return max_materialized is not None and current >= int(max_materialized or 0)


def _looks_like_final_chapter_label(*, title: str, summary: str = "") -> bool:
    text = f"{title}\n{summary}"
    return any(
        marker in text
        for marker in (
            "终章",
            "尾声",
            "大结局",
            "最终章",
            "最后一章",
            "最后一日",
            "最后一天",
            "最终决战",
            "finale",
            "Finale",
        )
    )


class ChapterContextAssembler:
    def __init__(self, *, providers: list | None = None, gates: list | None = None, observability=None) -> None:
        self.providers = providers or self._default_providers()
        self.gates = gates or self._default_gates()
        self.observability = observability or NullObservability()

    @property
    def provider_names(self) -> list[str]:
        return [str(getattr(provider, "name", provider.__class__.__name__)) for provider in self.providers]

    def assemble(
        self,
        repo,
        project_id: str,
        chapter_plan,
    ) -> ChapterContextPack:
        from forwin.context.request import ContextDraft, ContextRequest

        request = ContextRequest(
            project_id=project_id,
            chapter_plan=chapter_plan,
            repo=repo,
            session=getattr(repo, "session", None),
        )
        draft = ContextDraft(data={}, issues=[])
        base_context = OperationContext(
            project_id=project_id,
            chapter_number=int(getattr(chapter_plan, "chapter_number", 0) or 0),
            stage="chapter.assemble_context",
        )
        for provider in self.providers:
            provider_name = str(getattr(provider, "name", provider.__class__.__name__))
            with self.observability.span(
                base_context,
                f"context.provider.{provider_name}",
                span_kind="context",
                component="context",
                tags={"provider": provider_name},
            ) as span:
                before_issue_count = len(draft.issues)
                before_key_count = len(draft.data)
                provider.contribute(request, draft)
                span.metric("data_key_count", len(draft.data))
                span.metric("added_data_keys", max(0, len(draft.data) - before_key_count))
                span.metric("provider_issue_count", max(0, len(draft.issues) - before_issue_count))
        for gate in self.gates:
            gate_name = str(getattr(gate, "name", gate.__class__.__name__))
            with self.observability.span(
                base_context,
                f"context.gate.{gate_name}",
                span_kind="context",
                component="context",
                tags={"gate": gate_name},
            ) as span:
                issues = gate.validate(request, draft)
                draft.issues.extend(issues)
                span.metric("issue_count", len(issues))
        return self._build_pack(
            project_id=project_id,
            chapter_plan=chapter_plan,
            draft=draft,
            session=getattr(repo, "session", None),
        )

    def _build_pack(self, *, project_id: str, chapter_plan, draft, session=None) -> ChapterContextPack:
        from forwin.protocol.world_model import WorldContextPack

        data = draft.data
        project = data["project"]
        chapter_experience_plan = data.get("chapter_experience_plan")
        arc_world_contract = data.get("arc_world_contract")
        band_world_contract = data.get("band_world_contract")
        chapter_world_delta_intent = data.get("chapter_world_delta_intent")
        return ChapterContextPack(
            project_id=project_id,
            project_title=project.title,
            premise=project.premise,
            genre=project.genre,
            setting_summary=project.setting_summary,
            project_target_total_chapters=int(getattr(project, "target_total_chapters", 0) or 0),
            genesis_context_refs=data.get("genesis_refs", {}),
            genesis_world_overview=data.get("genesis_world_overview", ""),
            genesis_map_overview=data.get("genesis_map_overview", ""),
            genesis_story_engine_summary=data.get("genesis_story_engine_summary", ""),
            chapter_number=chapter_plan.chapter_number,
            chapter_plan_title=chapter_plan.title,
            chapter_plan_one_line=chapter_plan.one_line,
            chapter_goals=data.get("goals", []),
            previous_chapter_summaries=data.get("summaries", []),
            active_entities=data.get("entities", []),
            active_relations=data.get("relations", []),
            active_threads=data.get("threads", []),
            timeline=data.get("timeline"),
            npc_intents=data.get("npc_intents", []),
            world_pressure=data.get("world_pressure"),
            reader_feedback=data.get("reader_feedback"),
            current_arc_envelope=data.get("current_arc_envelope"),
            audience_hints=data.get("audience_hints"),
            reader_promise=data.get("reader_promise"),
            arc_payoff_map=data.get("arc_payoff_map"),
            band_delight_schedule=data.get("band_schedule"),
            chapter_experience_plan=chapter_experience_plan,
            active_subworlds=data.get("active_subworlds", []),
            allowed_entities=data.get("allowed_entities", []),
            chapter_entry_targets=(
                list(chapter_experience_plan.chapter_entry_targets)
                if chapter_experience_plan is not None
                else []
            ),
            entity_admission_rule=(
                str(chapter_experience_plan.entity_admission_rule or "").strip()
                if chapter_experience_plan is not None
                else ""
            ),
            chapter_task_contract=data.get("chapter_task_contract", []),
            band_task_contract=data.get("band_task_contract", []),
            active_future_constraints=data.get("active_constraints", []),
            next_band_summary=data.get("next_band_summary"),
            world_context=data.get("world_context", WorldContextPack()),
            map_context=data.get("map_context", {}),
            active_world_lines=list(
                dict.fromkeys(
                    [
                        *data.get("book_state_world_lines", []),
                        *(arc_world_contract.primary_world_line_ids if arc_world_contract else []),
                        *(arc_world_contract.hidden_world_line_ids if arc_world_contract else []),
                    ]
                )
            ),
            visible_world_lines=(
                list(arc_world_contract.primary_world_line_ids)
                if arc_world_contract is not None
                else []
            ),
            hidden_world_lines=(
                list(arc_world_contract.hidden_world_line_ids)
                if arc_world_contract is not None
                else []
            ),
            active_knowledge_gaps=list(
                dict.fromkeys(
                    [
                        *data.get("book_state_knowledge_gaps", []),
                        *(arc_world_contract.major_gap_ids if arc_world_contract else []),
                    ]
                )
            ),
            planned_reveal_ladder=(
                list(arc_world_contract.reveal_ladder)
                if arc_world_contract is not None
                else []
            ),
            reader_cognition_state=(
                band_world_contract.band_exit_reader_state
                if band_world_contract is not None
                else ""
            ),
            observer_visibility_states=(
                dict(chapter_world_delta_intent.expected_observer_state_changes)
                if chapter_world_delta_intent is not None
                else {}
            ),
            must_not_reveal=(
                list(chapter_world_delta_intent.must_not_reveal)
                if chapter_world_delta_intent is not None
                else []
            ),
            fair_misdirection_requirements=(
                list(band_world_contract.required_hints)
                if band_world_contract is not None
                else []
            ),
            chapter_world_delta_intent=chapter_world_delta_intent,
            active_personality_contexts=data.get("active_personality_contexts", []),
            personality_integrity_issues=data.get("personality_integrity_issues", []),
            canon_quality_context=_build_canon_quality_context(
                session=session,
                project_id=project_id,
                chapter_number=int(getattr(chapter_plan, "chapter_number", 0) or 0),
                target_total_chapters=int(getattr(project, "target_total_chapters", 0) or 0),
                chapter_title=str(getattr(chapter_plan, "title", "") or ""),
                chapter_summary=str(getattr(chapter_plan, "one_line", "") or ""),
            ),
        )

    @staticmethod
    def _default_providers() -> list:
        from forwin.context.providers import (
            BookStateContextProvider,
            ExperienceContextProvider,
            FeedbackContextProvider,
            GenesisContextProvider,
            MapContextProvider,
            PersonalityContextProvider,
            StateContextProvider,
        )

        return [
            GenesisContextProvider(),
            StateContextProvider(),
            ExperienceContextProvider(),
            MapContextProvider(),
            BookStateContextProvider(),
            PersonalityContextProvider(),
            FeedbackContextProvider(),
        ]

    @staticmethod
    def _default_gates() -> list:
        from forwin.context.gates import ContextIntegrityGate, PersonalityIntegrityGate

        return [PersonalityIntegrityGate(), ContextIntegrityGate()]


def assemble_context(
    repo,  # StateRepository
    project_id: str,
    chapter_plan,  # ChapterPlan ORM object
) -> ChapterContextPack:
    """Build a ChapterContextPack for the writer through the provider chain."""
    return ChapterContextAssembler().assemble(repo, project_id, chapter_plan)
