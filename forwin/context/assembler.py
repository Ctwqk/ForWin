"""Context assembler - builds ChapterContextPack from current state."""
from __future__ import annotations
import json
import logging

from forwin.protocol.context import (
    ArcEnvelopeView,
    AudienceHintView,
    ChapterContextPack,
    NPCIntentView,
    TimelineSnapshot,
    WorldPressureView,
)
from forwin.book_state import BookStateProjection, BookStateRepository
from forwin.planning.world_contracts import WorldContractRepository
from forwin.map.pathfinding import MapGraph
from forwin.map.repository import MapRepository
from forwin.map.visibility import is_writer_visible_map_edge
from forwin.personality import CharacterPersonalityLibrary, build_active_personality_contexts
from forwin.protocol.book_state import MapNode, WorldNode
from forwin.protocol.world_model import WorldContextPack
from forwin.world_model.retriever import WorldModelRetriever

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
    site_states: list[dict] = []
    for node in runtime.world.nodes_by_id.values():
        node_type = str(getattr(node, "node_type", "") or "")
        if node_type == "character":
            loadout = node.profile.get("personality_loadout") if isinstance(node.profile, dict) else None
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


def assemble_context(
    repo,  # StateRepository
    project_id: str,
    chapter_plan,  # ChapterPlan ORM object
) -> ChapterContextPack:
    """Build a ChapterContextPack for the writer.

    Args:
        repo: StateRepository instance
        project_id: current project ID
        chapter_plan: ChapterPlan ORM object with chapter_number, title, one_line, goals_json

    Returns:
        ChapterContextPack ready for the writer
    """
    # 1. Get project
    project = repo.get_project(project_id)
    genesis_refs: dict[str, str] = {}
    genesis_world_overview = ""
    genesis_map_overview = ""
    genesis_story_engine_summary = ""
    genesis_map_atlas: dict = {}
    genesis_story_engine: dict = {}
    runtime_region_drafts: list[dict] = []
    genesis_getter = getattr(repo, "get_active_genesis_revision", None)
    if callable(genesis_getter):
        genesis_revision = genesis_getter(project_id)
        if genesis_revision is not None:
            try:
                genesis_pack = json.loads(getattr(genesis_revision, "pack_json", "{}") or "{}") or {}
            except (TypeError, ValueError, json.JSONDecodeError):
                genesis_pack = {}
            if isinstance(genesis_pack, dict):
                world_root = genesis_pack.get("world") if isinstance(genesis_pack.get("world"), dict) else {}
                if not world_root:
                    world_root = {
                        "world_bible": genesis_pack.get("world_bible") if isinstance(genesis_pack.get("world_bible"), dict) else {},
                        "map_atlas": genesis_pack.get("map_atlas") if isinstance(genesis_pack.get("map_atlas"), dict) else {},
                        "story_engine": genesis_pack.get("story_engine") if isinstance(genesis_pack.get("story_engine"), dict) else {},
                    }
                world_bible = world_root.get("world_bible") if isinstance(world_root.get("world_bible"), dict) else {}
                genesis_map_atlas = world_root.get("map_atlas") if isinstance(world_root.get("map_atlas"), dict) else {}
                story_engine = world_root.get("story_engine") if isinstance(world_root.get("story_engine"), dict) else {}
                genesis_story_engine = story_engine
                genesis_world_overview = str(world_bible.get("overview", "") or "")
                long_arcs = story_engine.get("long_arcs") if isinstance(story_engine.get("long_arcs"), list) else []
                genesis_story_engine_summary = "；".join(str(item).strip() for item in long_arcs if str(item).strip())
                genesis_refs = {
                    "genesis_revision_id": str(getattr(genesis_revision, "id", "") or ""),
                    "genesis_revision_number": str(getattr(genesis_revision, "revision", "") or ""),
                }

    # 2. Get chapter-allowed entities with latest states. Older test doubles
    # may only expose the pre-subworld getter.
    allowed_entities_getter = getattr(repo, "get_allowed_entity_snapshots", None)
    if callable(allowed_entities_getter):
        entities = allowed_entities_getter(project_id, chapter_plan.chapter_number)
    else:
        entities = repo.get_active_entities(project_id)

    allowed_entities = [entity.name for entity in entities if entity.kind == "character"]

    # 3. Get relations narrowed to allowed entities
    relations_getter = getattr(repo, "get_active_relations")
    try:
        relations = relations_getter(project_id, entity_names=allowed_entities)
    except TypeError:
        relations = [
            relation
            for relation in relations_getter(project_id)
            if relation.source_name in allowed_entities or relation.target_name in allowed_entities
        ]

    # 4. Get active plot threads with recent beats
    threads = repo.get_active_threads(project_id)

    # 5. Get previous chapter summaries (last 3)
    summaries = repo.get_chapter_summaries(project_id, chapter_plan.chapter_number)

    # 6. Get current timeline
    timeline = repo.get_current_timeline(project_id)

    # 6.5 Get latest NPC intents / world pressure
    npc_intents_getter = getattr(repo, "get_recent_npc_intents", None)
    npc_intents = (
        npc_intents_getter(project_id, before_chapter=chapter_plan.chapter_number)
        if callable(npc_intents_getter)
        else []
    )
    world_pressure_getter = getattr(repo, "get_latest_world_pressure", None)
    world_pressure = (
        world_pressure_getter(project_id, before_chapter=chapter_plan.chapter_number)
        if callable(world_pressure_getter)
        else None
    )
    arc_envelope_getter = getattr(repo, "get_active_arc_envelope", None)
    arc_envelope_row = (
        arc_envelope_getter(project_id)
        if callable(arc_envelope_getter)
        else None
    )
    reader_promise_getter = getattr(repo, "get_reader_promise", None)
    reader_promise = (
        reader_promise_getter(project_id)
        if callable(reader_promise_getter)
        else None
    )
    arc_payoff_map_getter = getattr(repo, "get_arc_payoff_map", None)
    arc_payoff_map = (
        arc_payoff_map_getter(project_id)
        if callable(arc_payoff_map_getter)
        else None
    )
    band_schedule_getter = getattr(repo, "get_band_experience_plan_for_chapter", None)
    band_schedule = (
        band_schedule_getter(project_id, chapter_plan.chapter_number)
        if callable(band_schedule_getter)
        else None
    )
    chapter_experience_getter = getattr(repo, "get_chapter_experience_plan", None)
    chapter_experience_plan = (
        chapter_experience_getter(project_id, chapter_plan.chapter_number)
        if callable(chapter_experience_getter)
        else None
    )
    active_subworld_summary_getter = getattr(repo, "get_active_subworld_summary", None)
    active_subworlds = (
        active_subworld_summary_getter(project_id, chapter_plan.chapter_number)
        if callable(active_subworld_summary_getter)
        else []
    )
    active_subworld_region_drafts_getter = getattr(repo, "get_active_subworld_region_drafts", None)
    runtime_region_drafts = (
        active_subworld_region_drafts_getter(project_id, chapter_plan.chapter_number)
        if callable(active_subworld_region_drafts_getter)
        else []
    )
    genesis_map_overview = _build_genesis_map_overview(genesis_map_atlas, runtime_region_drafts)
    audience_hints_getter = getattr(repo, "get_audience_hints", None)
    audience_hints_raw = (
        audience_hints_getter(project_id, before_chapter=chapter_plan.chapter_number)
        if callable(audience_hints_getter)
        else None
    )
    chapter_task_contract_getter = getattr(repo, "get_chapter_task_contract", None)
    chapter_task_contract = (
        chapter_task_contract_getter(project_id, chapter_plan.chapter_number)
        if callable(chapter_task_contract_getter)
        else []
    )
    band_task_contract_getter = getattr(repo, "get_band_task_contract_for_chapter", None)
    band_task_contract = (
        band_task_contract_getter(project_id, chapter_plan.chapter_number)
        if callable(band_task_contract_getter)
        else []
    )
    constraints_enabled_getter = getattr(repo, "future_constraints_enabled", None)
    constraints_enabled = (
        bool(constraints_enabled_getter(project_id))
        if callable(constraints_enabled_getter)
        else True
    )
    active_constraints_getter = getattr(repo, "list_active_narrative_constraints", None)
    active_constraints = (
        active_constraints_getter(project_id, chapter_number=chapter_plan.chapter_number)
        if constraints_enabled and callable(active_constraints_getter)
        else []
    )
    next_band_summary_getter = getattr(repo, "get_next_band_summary", None)
    next_band_summary = (
        next_band_summary_getter(project_id, chapter_plan.chapter_number)
        if callable(next_band_summary_getter)
        else None
    )
    chapter_world_delta_intent = None
    arc_world_contract = None
    band_world_contract = None
    repo_session = getattr(repo, "session", None)
    if repo_session is not None:
        world_contract_repo = WorldContractRepository(repo_session)
        arc_world_contract = world_contract_repo.get_arc_contract(
            project_id,
            chapter_plan.arc_plan_id,
        )
        band_world_contract = world_contract_repo.get_band_contract_for_chapter(
            project_id,
            chapter_plan.chapter_number,
        )
        chapter_world_delta_intent = world_contract_repo.get_chapter_intent(
            project_id,
            chapter_plan.chapter_number,
        )

    # 7. Parse chapter goals from goals_json
    try:
        goals = json.loads(chapter_plan.goals_json) if chapter_plan.goals_json else []
    except json.JSONDecodeError:
        goals = []

    world_context = WorldContextPack()
    repo_session = getattr(repo, "session", None)
    if repo_session is not None:
        try:
            query_terms = [
                chapter_plan.title,
                chapter_plan.one_line,
                *goals,
                *(entity.name for entity in entities[:8]),
                *(thread.name for thread in threads[:4]),
            ]
            world_context = WorldModelRetriever(repo_session).build_context(
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
                query_terms=query_terms,
                max_pages=6,
            )
        except Exception:
            logger.warning("Failed to assemble world model context.", exc_info=True)
    map_context = _build_map_context(
        repo_session,
        project_id,
        entities,
        genesis_story_engine=genesis_story_engine,
    )
    book_state_overlay = _book_state_context_overlay(
        repo_session,
        project_id,
        chapter_plan.chapter_number,
    )
    map_context = _merge_book_state_map_overlay(map_context, book_state_overlay)
    book_state_world_lines = [
        str(item)
        for item in book_state_overlay.get("active_world_lines", [])
        if str(item).strip()
    ]
    book_state_knowledge_gaps = [
        str(item)
        for item in book_state_overlay.get("active_knowledge_gaps", [])
        if str(item).strip()
    ]
    active_personality_contexts: list[dict] = []
    try:
        pressure_triggers = []
        if world_pressure is not None:
            pressure_triggers.extend(
                [
                    str(world_pressure.pressure_level or "").strip(),
                    str(world_pressure.pressure_summary or "").strip(),
                ]
            )
            pressure_triggers.extend(str(item).strip() for item in world_pressure.notable_shifts if str(item).strip())
        active_personality_contexts = [
            item.model_dump(mode="json")
            for item in build_active_personality_contexts(
                [
                    item
                    for item in (book_state_overlay.get("personality_characters") or [])
                    if str(item.get("character_name") or "") in allowed_entities
                    or str(item.get("character_id") or "") in allowed_entities
                ],
                library=CharacterPersonalityLibrary(),
                scene_flags=["chapter_generation"],
                pressure_triggers=pressure_triggers,
            )
        ]
    except Exception:
        logger.warning("Failed to build active personality contexts.", exc_info=True)

    # 8. Build and return pack
    return ChapterContextPack(
        project_id=project_id,
        project_title=project.title,
        premise=project.premise,
        genre=project.genre,
        setting_summary=project.setting_summary,
        genesis_context_refs=genesis_refs,
        genesis_world_overview=genesis_world_overview,
        genesis_map_overview=genesis_map_overview,
        genesis_story_engine_summary=genesis_story_engine_summary,
        chapter_number=chapter_plan.chapter_number,
        chapter_plan_title=chapter_plan.title,
        chapter_plan_one_line=chapter_plan.one_line,
        chapter_goals=goals,
        previous_chapter_summaries=summaries,
        active_entities=entities,
        active_relations=relations,
        active_threads=threads,
        timeline=timeline,
        npc_intents=npc_intents,
        world_pressure=world_pressure,
        reader_feedback=None,
        current_arc_envelope=(
            ArcEnvelopeView(
                source_policy_tier=arc_envelope_row.source_policy_tier,
                base_target_size=arc_envelope_row.base_target_size,
                base_soft_min=arc_envelope_row.base_soft_min,
                base_soft_max=arc_envelope_row.base_soft_max,
                resolved_target_size=arc_envelope_row.resolved_target_size,
                resolved_soft_min=arc_envelope_row.resolved_soft_min,
                resolved_soft_max=arc_envelope_row.resolved_soft_max,
                detailed_band_size=arc_envelope_row.detailed_band_size,
                frozen_zone_size=arc_envelope_row.frozen_zone_size,
                current_projected_size=arc_envelope_row.current_projected_size,
                current_confidence=arc_envelope_row.current_confidence,
            )
            if arc_envelope_row is not None
            else None
        ),
        audience_hints=(
            AudienceHintView(
                pacing_hints=audience_hints_raw.pacing_hints,
                clarity_hints=audience_hints_raw.clarity_hints,
                character_heat_changes=audience_hints_raw.character_heat_changes,
                risk_flags=audience_hints_raw.risk_flags,
            )
            if audience_hints_raw is not None
            else None
        ),
        reader_promise=reader_promise,
        arc_payoff_map=arc_payoff_map,
        band_delight_schedule=band_schedule,
        chapter_experience_plan=chapter_experience_plan,
        active_subworlds=active_subworlds,
        allowed_entities=allowed_entities,
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
        chapter_task_contract=chapter_task_contract,
        band_task_contract=band_task_contract,
        active_future_constraints=active_constraints,
        next_band_summary=next_band_summary,
        world_context=world_context,
        map_context=map_context,
        active_world_lines=list(
            dict.fromkeys(
                [
                    *book_state_world_lines,
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
        active_knowledge_gaps=(
            list(
                dict.fromkeys(
                    [
                        *book_state_knowledge_gaps,
                        *(arc_world_contract.major_gap_ids if arc_world_contract else []),
                    ]
                )
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
        active_personality_contexts=active_personality_contexts,
    )
