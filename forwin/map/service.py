from __future__ import annotations

import json
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.book_state.cognition import CognitionView
from forwin.book_state.runtime import ObjectiveWorldGraph
from forwin.models.book_state import CognitionOverlayRow
from forwin.protocol.book_state import CognitionOverlay, FactNode, MapEdge, MapNode, PathResult, WorldEdge, WorldNode

from .generator import generate_subworld_map
from .pathfinding import MapGraph
from .protocol import (
    BookMapGenerationResult,
    BookMapRuntime,
    InterSubWorldConnectionSpec,
    MapGenerationResult,
    MapValidationReport,
    SubWorldMapSpec,
)
from .repository import MapRepository


def create_or_update_subworld_map(
    session: Session,
    spec: SubWorldMapSpec,
    *,
    commit: bool = False,
) -> MapGenerationResult:
    result = generate_subworld_map(spec)
    if not result.validation_report.valid:
        return result
    repo = MapRepository(session)
    repo.ensure_subworld_map_metadata(spec)
    repo.persist_generation_result(spec=spec, result=result)
    if commit:
        session.commit()
    return result


def create_or_update_book_map(
    session: Session,
    specs: list[SubWorldMapSpec],
    *,
    interconnections: list[InterSubWorldConnectionSpec] | None = None,
    commit: bool = False,
) -> BookMapGenerationResult:
    if not specs:
        return BookMapGenerationResult(project_id="", summary={"subworld_count": 0})
    project_id = specs[0].project_id
    if any(spec.project_id != project_id for spec in specs):
        raise ValueError("all SubWorldMapSpec entries must share project_id")

    subworld_results = [
        create_or_update_subworld_map(session, spec, commit=False)
        for spec in specs
    ]
    errors = [
        error
        for result in subworld_results
        for error in result.validation_report.errors
    ]
    inter_edges: list[MapEdge] = []
    if not errors:
        repo = MapRepository(session)
        for connection in list(interconnections or _default_interconnections(specs)):
            if connection.project_id != project_id:
                raise ValueError("all InterSubWorldConnectionSpec entries must share project_id")
            inter_edges.append(_persist_inter_subworld_connection(repo, connection))
    if commit:
        session.commit()
    return BookMapGenerationResult(
        project_id=project_id,
        subworld_results=subworld_results,
        inter_subworld_edges=inter_edges,
        validation_report=MapValidationReport(valid=not errors, errors=errors),
        summary={
            "subworld_count": len(subworld_results),
            "inter_subworld_edge_count": len(inter_edges),
        },
    )


def get_subworld_map(
    session: Session,
    project_id: str,
    subworld_id: str,
) -> MapGenerationResult:
    repo = MapRepository(session)
    regions = repo.list_regions(project_id, subworld_id)
    region_edges = repo.list_region_edges(project_id, subworld_id)
    nodes = repo.list_map_nodes(project_id, subworld_id)
    edges = repo.list_map_edges(project_id, subworld_id)
    generation_seed = 0
    if nodes:
        generation_seed = int(nodes[0].metadata.get("generation_seed", 0) or 0)
    return MapGenerationResult(
        project_id=project_id,
        subworld_id=subworld_id,
        generation_seed=generation_seed,
        regions=regions,
        region_edges=region_edges,
        map_nodes=nodes,
        map_edges=edges,
        summary={
            "region_count": len(regions),
            "region_edge_count": len(region_edges),
            "node_count": len(nodes),
            "edge_count": len(edges),
        },
    )


def get_book_map_runtime(
    session: Session,
    project_id: str,
) -> BookMapRuntime:
    return MapRepository(session).get_book_map_runtime(project_id)


def compute_distance(
    session: Session,
    project_id: str,
    from_node_id: str,
    to_node_id: str,
    *,
    metric: str = "travel_time",
    allow_hidden: bool = False,
    allow_blocked: bool = False,
) -> PathResult:
    repo = MapRepository(session)
    graph = MapGraph(
        nodes=repo.list_map_nodes(project_id),
        edges=repo.list_map_edges(project_id),
    )
    return graph.shortest_path(
        from_node_id,
        to_node_id,
        metric=metric,
        allow_hidden=allow_hidden,
        allow_blocked=allow_blocked,
    )


def compute_known_distance(
    session: Session,
    project_id: str,
    from_node_id: str,
    to_node_id: str,
    *,
    observer: tuple[str, str],
    metric: str = "travel_time",
    allow_hidden: bool = False,
    allow_blocked: bool = False,
) -> PathResult:
    repo = MapRepository(session)
    overlay = _latest_overlay(session, project_id, observer)
    cognition_by_observer = {observer: CognitionView(overlay)} if overlay is not None else {}
    graph = MapGraph(
        nodes=repo.list_map_nodes(project_id),
        edges=repo.list_map_edges(project_id),
        cognition_by_observer=cognition_by_observer,
    )
    return graph.shortest_path(
        from_node_id,
        to_node_id,
        metric=metric,
        observer=observer,
        allow_hidden=allow_hidden,
        allow_blocked=allow_blocked,
    )


def resolve_world_node_location_id(world: ObjectiveWorldGraph, node_id: str) -> str:
    state = world.get_state(node_id)
    location_id = str(state.get("location_id", "") or "").strip()
    if location_id:
        return location_id
    legacy_location = str(state.get("location", "") or "").strip()
    if legacy_location:
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


def _default_interconnections(specs: list[SubWorldMapSpec]) -> list[InterSubWorldConnectionSpec]:
    return [
        InterSubWorldConnectionSpec(
            project_id=left.project_id,
            from_subworld_id=left.subworld_id,
            to_subworld_id=right.subworld_id,
            edge_type="world_gate",
            bidirectional=True,
            metadata={"source": "default_book_map_link"},
        )
        for left, right in zip(specs, specs[1:])
    ]


def _persist_inter_subworld_connection(
    repo: MapRepository,
    connection: InterSubWorldConnectionSpec,
) -> MapEdge:
    from_node_id = connection.from_node_id or _ensure_exit_node(
        repo,
        project_id=connection.project_id,
        subworld_id=connection.from_subworld_id,
        target_subworld_id=connection.to_subworld_id,
        side="from",
    ).id
    to_node_id = connection.to_node_id or _ensure_exit_node(
        repo,
        project_id=connection.project_id,
        subworld_id=connection.to_subworld_id,
        target_subworld_id=connection.from_subworld_id,
        side="to",
    ).id
    multiplier = float(connection.cost_multiplier or 1.0)
    edge = MapEdge(
        id=_stable_map_id(
            "inter_edge",
            connection.project_id,
            connection.from_subworld_id,
            connection.to_subworld_id,
            from_node_id,
            to_node_id,
            connection.edge_type,
        ),
        project_id=connection.project_id,
        subworld_id=connection.from_subworld_id,
        from_node_id=from_node_id,
        to_node_id=to_node_id,
        edge_type=connection.edge_type,
        bidirectional=connection.bidirectional,
        distance=round(connection.distance * multiplier, 2),
        travel_time=round(connection.travel_time * multiplier, 2),
        travel_cost=round(connection.travel_cost * multiplier, 2),
        risk_level=round(connection.risk_level * multiplier, 2),
        narrative_cost=round(connection.narrative_cost * multiplier, 2),
        access_rule_id=connection.access_rule_id,
        status="hidden" if connection.hidden else "open",
        discovered_by_default=not connection.hidden,
        visibility_default="hidden" if connection.hidden else "visible",
        metadata={
            **connection.metadata,
            "inter_subworld_edge": True,
            "source_subworld_id": connection.from_subworld_id,
            "target_subworld_id": connection.to_subworld_id,
        },
    )
    repo.upsert_map_edge(edge)
    return edge


def _ensure_exit_node(
    repo: MapRepository,
    *,
    project_id: str,
    subworld_id: str,
    target_subworld_id: str,
    side: str,
) -> MapNode:
    node_id = _stable_map_id("exit", project_id, subworld_id, target_subworld_id, side)
    existing = next((node for node in repo.list_map_nodes(project_id, subworld_id) if node.id == node_id), None)
    if existing is not None:
        _ensure_exit_connector(repo, existing)
        return existing
    regions = repo.list_regions(project_id, subworld_id)
    if not regions:
        raise ValueError(f"cannot create inter-subworld exit without regions for {subworld_id}")
    region = regions[0]
    node = MapNode(
        id=node_id,
        project_id=project_id,
        subworld_id=subworld_id,
        region_id=region.id,
        node_type="waypoint",
        name=f"{region.name}界门",
        description=f"连接 {target_subworld_id} 的跨 subworld 出入口。",
        hierarchy_path=f"{subworld_id}/{region.id}",
        scale_level="gateway",
        terrain=region.terrain,
        culture_tag=region.culture_tag,
        default_danger_level=region.danger_level,
        access_level="restricted",
        metadata={
            "node_role": "exit_node",
            "target_subworld_id": target_subworld_id,
            "scheme": "方案 C：Graph-based Weighted Map Generation",
        },
    )
    repo.upsert_map_node(node)
    _ensure_exit_connector(repo, node)
    return node


def _ensure_exit_connector(repo: MapRepository, exit_node: MapNode) -> None:
    local_nodes = [
        node
        for node in repo.list_map_nodes(exit_node.project_id, exit_node.subworld_id)
        if node.region_id == exit_node.region_id and node.id != exit_node.id
    ]
    if not local_nodes:
        return
    hub = next((node for node in local_nodes if node.metadata.get("node_role") == "hub"), local_nodes[0])
    edge = MapEdge(
        id=_stable_map_id("exit_connector", exit_node.project_id, exit_node.id, hub.id),
        project_id=exit_node.project_id,
        subworld_id=exit_node.subworld_id,
        from_node_id=hub.id,
        to_node_id=exit_node.id,
        edge_type="road",
        bidirectional=True,
        distance=10.0,
        travel_time=1.0,
        travel_cost=1.0,
        risk_level=max(0.0, float(exit_node.default_danger_level or 0.0)),
        narrative_cost=1.0,
        status="open",
        discovered_by_default=True,
        visibility_default="visible",
        metadata={
            "exit_connector": True,
            "target_subworld_id": exit_node.metadata.get("target_subworld_id", ""),
        },
    )
    repo.upsert_map_edge(edge)


def _stable_map_id(prefix: str, *parts: str) -> str:
    return f"{prefix}_{uuid5(NAMESPACE_URL, '|'.join(str(part) for part in parts)).hex[:16]}"


def _latest_overlay(
    session: Session,
    project_id: str,
    observer: tuple[str, str],
) -> CognitionOverlay | None:
    row = session.execute(
        select(CognitionOverlayRow)
        .where(
            CognitionOverlayRow.project_id == project_id,
            CognitionOverlayRow.observer_type == observer[0],
            CognitionOverlayRow.observer_id == observer[1],
        )
        .order_by(CognitionOverlayRow.as_of_chapter.desc(), CognitionOverlayRow.created_at.desc())
    ).scalars().first()
    if row is None:
        return None
    false_nodes = {
        key: WorldNode.model_validate(value)
        for key, value in _loads(row.false_nodes_json, {}).items()
    }
    false_edges: dict[str, WorldEdge | MapEdge] = {}
    for key, value in _loads(row.false_edges_json, {}).items():
        if not isinstance(value, dict):
            continue
        if "from_node_id" in value and "to_node_id" in value:
            false_edges[key] = MapEdge.model_validate(value)
        elif "source_id" in value and "target_id" in value:
            false_edges[key] = WorldEdge.model_validate(value)
    false_facts = {
        key: FactNode.model_validate(value)
        for key, value in _loads(row.false_facts_json, {}).items()
    }
    return CognitionOverlay(
        id=row.id,
        project_id=row.project_id,
        observer_type=row.observer_type,
        observer_id=row.observer_id,
        as_of_chapter=row.as_of_chapter,
        as_of_story_time=row.as_of_story_time,
        visible_refs=_loads(row.visible_refs_json, []),
        hidden_refs=_loads(row.hidden_refs_json, []),
        suspected_refs=_loads(row.suspected_refs_json, []),
        confirmed_refs=_loads(row.confirmed_refs_json, []),
        field_overrides=_loads(row.field_overrides_json, {}),
        false_nodes=false_nodes,
        false_edges=false_edges,
        false_facts=false_facts,
        evidence_by_ref=_loads(row.evidence_by_ref_json, {}),
        metadata=_loads(row.metadata_json, {}),
    )


def _loads(text: str | None, default: Any) -> Any:
    try:
        value = json.loads(text or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return value if value is not None else default
