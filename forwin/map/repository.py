from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from forwin.book_state.cognition import CognitionView
from forwin.models.subworld import SubWorld
from forwin.protocol.book_state import CognitionOverlay, MapEdge, MapNode

from .models import (
    MapEdgeRow,
    MapGenerationRunRow,
    MapNodeRow,
    MapRegionEdgeRow,
    MapRegionRow,
)
from .protocol import (
    BookMapRuntime,
    InterSubWorldEdge,
    MapGenerationResult,
    RegionEdge,
    RegionNode,
    SubWorldMapSpec,
    SubWorldNode,
)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(text: str | None, default: Any) -> Any:
    try:
        value = json.loads(text or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return value if value is not None else default


class MapRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def ensure_subworld_map_metadata(self, spec: SubWorldMapSpec) -> SubWorld:
        row = self.session.get(SubWorld, spec.subworld_id)
        if row is None:
            row = SubWorld(
                id=spec.subworld_id,
                project_id=spec.project_id,
                name=spec.name,
                purpose="大尺度地图容器",
                scope="arc_local",
                status="active",
            )
            self.session.add(row)
        row.name = spec.name or row.name
        row.subworld_type = spec.subworld_type
        row.scale_level = spec.scale_level
        row.culture_profile_json = _dump({"tags": list(spec.culture_tags)})
        row.terrain_profile_json = _dump({"tags": list(spec.terrain_tags)})
        row.danger_profile_json = _dump(spec.danger_profile)
        row.generation_seed = int(spec.generation_seed or 0)
        row.map_status = "generating"
        meta = _loads(row.metadata_json, {})
        if not isinstance(meta, dict):
            meta = {}
        meta.update(
            {
                "subworld_type": spec.subworld_type,
                "scale_level": spec.scale_level,
                "culture_profile": {"tags": list(spec.culture_tags)},
                "terrain_profile": {"tags": list(spec.terrain_tags)},
                "danger_profile": spec.danger_profile,
                "generation_seed": int(spec.generation_seed or 0),
                "map_status": "generating",
            }
        )
        row.metadata_json = _dump(meta)
        self.session.add(row)
        self.session.flush()
        return row

    def persist_generation_result(
        self,
        *,
        spec: SubWorldMapSpec,
        result: MapGenerationResult,
    ) -> MapGenerationRunRow:
        if not result.validation_report.valid:
            raise ValueError("cannot persist invalid map generation result")

        self.session.execute(delete(MapRegionEdgeRow).where(MapRegionEdgeRow.subworld_id == spec.subworld_id))
        edge_rows = self.session.execute(
            select(MapEdgeRow).where(MapEdgeRow.subworld_id == spec.subworld_id)
        ).scalars().all()
        for row in edge_rows:
            metadata = _loads(row.metadata_json, {})
            if isinstance(metadata, dict) and (
                metadata.get("inter_subworld_edge") is True or metadata.get("exit_connector") is True
            ):
                continue
            self.session.delete(row)
        node_rows = self.session.execute(
            select(MapNodeRow).where(MapNodeRow.subworld_id == spec.subworld_id)
        ).scalars().all()
        for row in node_rows:
            metadata = _loads(row.metadata_json, {})
            if isinstance(metadata, dict) and metadata.get("node_role") == "exit_node":
                continue
            self.session.delete(row)
        self.session.execute(delete(MapRegionRow).where(MapRegionRow.subworld_id == spec.subworld_id))

        for region in result.regions:
            self.upsert_region(region)
        for node in result.map_nodes:
            self.upsert_map_node(node)
        for region_edge in result.region_edges:
            self.upsert_region_edge(region_edge)
        for edge in result.map_edges:
            self.upsert_map_edge(edge)
        self._delete_orphan_inter_subworld_edges(spec.project_id)

        subworld = self.session.get(SubWorld, spec.subworld_id)
        if subworld is not None:
            subworld.map_status = "generated"
            subworld.generation_seed = int(spec.generation_seed or 0)
            meta = _loads(subworld.metadata_json, {})
            if not isinstance(meta, dict):
                meta = {}
            meta["map_status"] = "generated"
            meta["generation_seed"] = int(spec.generation_seed or 0)
            subworld.metadata_json = _dump(meta)
            self.session.add(subworld)

        run = MapGenerationRunRow(
            project_id=spec.project_id,
            subworld_id=spec.subworld_id,
            generation_seed=int(spec.generation_seed or 0),
            algorithm=result.algorithm,
            input_spec_json=spec.model_dump_json(),
            result_summary_json=_dump(result.summary),
            validation_report_json=result.validation_report.model_dump_json(),
        )
        self.session.add(run)
        self.session.flush()
        return run

    def upsert_region(self, region: RegionNode) -> MapRegionRow:
        row = self.session.get(MapRegionRow, region.id)
        if row is None:
            row = MapRegionRow(
                id=region.id,
                project_id=region.project_id,
                subworld_id=region.subworld_id,
                region_type=region.region_type,
            )
            self.session.add(row)
        row.region_type = region.region_type
        row.name = region.name
        row.aliases_json = _dump(region.aliases)
        row.description = region.description
        row.terrain = region.terrain
        row.culture_tag = region.culture_tag
        row.controlling_faction_id = region.controlling_faction_id
        row.danger_level = float(region.danger_level)
        row.node_ids_json = _dump(region.node_ids)
        row.boundary_node_ids_json = _dump(region.boundary_node_ids)
        row.entry_node_ids_json = _dump(region.entry_node_ids)
        row.status = region.status
        row.metadata_json = _dump(region.metadata)
        self.session.flush()
        return row

    def upsert_region_edge(self, edge: RegionEdge) -> MapRegionEdgeRow:
        row = self.session.get(MapRegionEdgeRow, edge.id)
        if row is None:
            row = MapRegionEdgeRow(
                id=edge.id,
                project_id=edge.project_id,
                subworld_id=edge.subworld_id,
                from_region_id=edge.from_region_id,
                to_region_id=edge.to_region_id,
            )
            self.session.add(row)
        row.from_region_id = edge.from_region_id
        row.to_region_id = edge.to_region_id
        row.edge_type = edge.edge_type
        row.bidirectional = bool(edge.bidirectional)
        row.distance = float(edge.distance)
        row.travel_time = float(edge.travel_time)
        row.risk_level = float(edge.risk_level)
        row.status = edge.status
        row.metadata_json = _dump(edge.metadata)
        self.session.flush()
        return row

    def upsert_map_node(self, node: MapNode) -> MapNodeRow:
        row = self.session.get(MapNodeRow, node.id)
        if row is None:
            row = MapNodeRow(
                id=node.id,
                project_id=node.project_id,
                subworld_id=node.subworld_id,
                region_id=node.region_id,
                node_type=str(node.node_type),
            )
            self.session.add(row)
        row.subworld_id = node.subworld_id
        row.region_id = node.region_id
        row.node_type = str(node.node_type)
        row.name = node.name
        row.aliases_json = _dump(node.aliases)
        row.description = node.description
        row.parent_id = node.parent_id
        row.hierarchy_path = node.hierarchy_path
        row.scale_level = node.scale_level
        row.coordinates_json = _dump(node.coordinates or {})
        row.shape_ref = node.shape_ref
        row.terrain = node.terrain
        row.climate = node.climate
        row.culture_tag = node.culture_tag
        row.default_danger_level = float(node.default_danger_level)
        row.access_level = node.access_level
        row.status = node.status
        metadata = dict(node.metadata)
        row.created_at_chapter = _created_at_chapter(metadata)
        metadata.setdefault("created_at_chapter", row.created_at_chapter)
        row.metadata_json = _dump(metadata)
        self.session.flush()
        return row

    def upsert_map_edge(self, edge: MapEdge) -> MapEdgeRow:
        _ensure_non_negative_edge(edge)
        row = self.session.get(MapEdgeRow, edge.id)
        if row is None:
            row = MapEdgeRow(id=edge.id, project_id=edge.project_id, subworld_id=edge.subworld_id)
            self.session.add(row)
        row.subworld_id = edge.subworld_id
        row.from_node_id = edge.from_node_id
        row.to_node_id = edge.to_node_id
        row.edge_type = str(edge.edge_type)
        row.bidirectional = bool(edge.bidirectional)
        row.distance = float(edge.distance)
        row.travel_time = float(edge.travel_time)
        row.travel_cost = float(edge.travel_cost)
        row.risk_level = float(edge.risk_level)
        row.narrative_cost = float(edge.narrative_cost)
        row.access_rule_id = edge.access_rule_id
        row.status = edge.status
        row.discovered_by_default = bool(edge.discovered_by_default)
        row.visibility_default = edge.visibility_default
        metadata = dict(edge.metadata)
        row.created_at_chapter = _created_at_chapter(metadata)
        metadata.setdefault("created_at_chapter", row.created_at_chapter)
        row.metadata_json = _dump(metadata)
        self.session.flush()
        return row

    def list_regions(self, project_id: str, subworld_id: str | None = None) -> list[RegionNode]:
        stmt = select(MapRegionRow).where(MapRegionRow.project_id == project_id)
        if subworld_id:
            stmt = stmt.where(MapRegionRow.subworld_id == subworld_id)
        rows = self.session.execute(stmt.order_by(MapRegionRow.id.asc())).scalars().all()
        return [_region_from_row(row) for row in rows]

    def list_region_edges(self, project_id: str, subworld_id: str | None = None) -> list[RegionEdge]:
        stmt = select(MapRegionEdgeRow).where(MapRegionEdgeRow.project_id == project_id)
        if subworld_id:
            stmt = stmt.where(MapRegionEdgeRow.subworld_id == subworld_id)
        rows = self.session.execute(stmt.order_by(MapRegionEdgeRow.id.asc())).scalars().all()
        return [_region_edge_from_row(row) for row in rows]

    def list_map_nodes(
        self,
        project_id: str,
        subworld_id: str | None = None,
        *,
        as_of_chapter: int | None = None,
    ) -> list[MapNode]:
        stmt = select(MapNodeRow).where(MapNodeRow.project_id == project_id)
        if subworld_id:
            stmt = stmt.where(MapNodeRow.subworld_id == subworld_id)
        if as_of_chapter is not None:
            stmt = stmt.where(MapNodeRow.created_at_chapter <= int(as_of_chapter))
        rows = self.session.execute(stmt.order_by(MapNodeRow.id.asc())).scalars().all()
        return [_map_node_from_row(row) for row in rows]

    def list_map_edges(
        self,
        project_id: str,
        subworld_id: str | None = None,
        *,
        as_of_chapter: int | None = None,
    ) -> list[MapEdge]:
        stmt = select(MapEdgeRow).where(MapEdgeRow.project_id == project_id)
        if subworld_id:
            stmt = stmt.where(MapEdgeRow.subworld_id == subworld_id)
        if as_of_chapter is not None:
            stmt = stmt.where(MapEdgeRow.created_at_chapter <= int(as_of_chapter))
        rows = self.session.execute(stmt.order_by(MapEdgeRow.id.asc())).scalars().all()
        return [_map_edge_from_row(row) for row in rows]

    def get_book_map_runtime(
        self,
        project_id: str,
        *,
        cognition_overlays: list[CognitionOverlay] | None = None,
    ) -> BookMapRuntime:
        subworld_rows = self.session.execute(
            select(SubWorld).where(SubWorld.project_id == project_id).order_by(SubWorld.id.asc())
        ).scalars().all()
        regions = self.list_regions(project_id)
        region_edges = self.list_region_edges(project_id)
        nodes = self.list_map_nodes(project_id)
        edges = self.list_map_edges(project_id)
        runtime = BookMapRuntime(
            project_id=project_id,
            subworlds_by_id={row.id: _subworld_from_row(row) for row in subworld_rows},
            regions_by_id={region.id: region for region in regions},
            region_edges_by_id={edge.id: edge for edge in region_edges},
            map_nodes_by_id={node.id: node for node in nodes},
            map_edges_by_id={edge.id: edge for edge in edges},
        )
        outgoing: dict[str, list[str]] = defaultdict(list)
        incoming: dict[str, list[str]] = defaultdict(list)
        regions_by_subworld: dict[str, list[str]] = defaultdict(list)
        nodes_by_region: dict[str, list[str]] = defaultdict(list)
        inter_edges: dict[str, InterSubWorldEdge] = {}
        node_subworlds = {node.id: node.subworld_id for node in nodes}
        for region in regions:
            regions_by_subworld[region.subworld_id].append(region.id)
        for node in nodes:
            nodes_by_region[node.region_id].append(node.id)
        for edge in edges:
            outgoing[edge.from_node_id].append(edge.id)
            incoming[edge.to_node_id].append(edge.id)
            from_subworld = node_subworlds.get(edge.from_node_id, edge.subworld_id)
            to_subworld = node_subworlds.get(edge.to_node_id, edge.metadata.get("target_subworld_id", ""))
            if from_subworld and to_subworld and from_subworld != to_subworld:
                inter_edges[edge.id] = InterSubWorldEdge(
                    id=f"inter_{edge.id}",
                    project_id=edge.project_id,
                    from_subworld_id=from_subworld,
                    to_subworld_id=to_subworld,
                    map_edge_id=edge.id,
                    edge_type=str(edge.edge_type),
                    metadata=dict(edge.metadata),
                )
        runtime.outgoing_edges = dict(outgoing)
        runtime.incoming_edges = dict(incoming)
        runtime.regions_by_subworld = dict(regions_by_subworld)
        runtime.nodes_by_region = dict(nodes_by_region)
        runtime.inter_subworld_edges_by_id = inter_edges
        if cognition_overlays:
            runtime.path_cache = {}
        return runtime

    def _delete_orphan_inter_subworld_edges(self, project_id: str) -> None:
        node_ids = {
            row[0]
            for row in self.session.execute(
                select(MapNodeRow.id).where(MapNodeRow.project_id == project_id)
            ).all()
        }
        rows = self.session.execute(
            select(MapEdgeRow).where(MapEdgeRow.project_id == project_id)
        ).scalars().all()
        for row in rows:
            if row.from_node_id not in node_ids or row.to_node_id not in node_ids:
                self.session.delete(row)
                continue
            metadata = _loads(row.metadata_json, {})
            if not (isinstance(metadata, dict) and metadata.get("inter_subworld_edge") is True):
                continue


def _subworld_from_row(row: SubWorld) -> SubWorldNode:
    return SubWorldNode(
        id=row.id,
        project_id=row.project_id,
        name=row.name,
        subworld_type=getattr(row, "subworld_type", "") or _loads(row.metadata_json, {}).get("subworld_type", ""),
        scale_level=getattr(row, "scale_level", "") or _loads(row.metadata_json, {}).get("scale_level", "world"),
        culture_profile=_loads(getattr(row, "culture_profile_json", "{}"), {}),
        terrain_profile=_loads(getattr(row, "terrain_profile_json", "{}"), {}),
        danger_profile=_loads(getattr(row, "danger_profile_json", "{}"), {}),
        generation_seed=int(getattr(row, "generation_seed", 0) or 0),
        map_status=getattr(row, "map_status", "") or _loads(row.metadata_json, {}).get("map_status", ""),
        metadata=_loads(row.metadata_json, {}),
    )


def _region_from_row(row: MapRegionRow) -> RegionNode:
    return RegionNode(
        id=row.id,
        project_id=row.project_id,
        subworld_id=row.subworld_id,
        region_type=row.region_type,
        name=row.name,
        aliases=_loads(row.aliases_json, []),
        description=row.description,
        terrain=row.terrain,
        culture_tag=row.culture_tag,
        controlling_faction_id=row.controlling_faction_id,
        danger_level=float(row.danger_level or 0.0),
        node_ids=_loads(row.node_ids_json, []),
        boundary_node_ids=_loads(row.boundary_node_ids_json, []),
        entry_node_ids=_loads(row.entry_node_ids_json, []),
        status=row.status,
        metadata=_loads(row.metadata_json, {}),
    )


def _region_edge_from_row(row: MapRegionEdgeRow) -> RegionEdge:
    return RegionEdge(
        id=row.id,
        project_id=row.project_id,
        subworld_id=row.subworld_id,
        from_region_id=row.from_region_id,
        to_region_id=row.to_region_id,
        edge_type=row.edge_type,
        bidirectional=bool(row.bidirectional),
        distance=float(row.distance or 0.0),
        travel_time=float(row.travel_time or 0.0),
        risk_level=float(row.risk_level or 0.0),
        status=row.status,
        metadata=_loads(row.metadata_json, {}),
    )


def _map_node_from_row(row: MapNodeRow) -> MapNode:
    coordinates = _loads(row.coordinates_json, {})
    metadata = _loads(row.metadata_json, {})
    if isinstance(metadata, dict):
        metadata.setdefault("created_at_chapter", int(getattr(row, "created_at_chapter", 0) or 0))
    else:
        metadata = {"created_at_chapter": int(getattr(row, "created_at_chapter", 0) or 0)}
    return MapNode(
        id=row.id,
        project_id=row.project_id,
        subworld_id=getattr(row, "subworld_id", "") or "",
        region_id=getattr(row, "region_id", "") or "",
        node_type=row.node_type,
        name=row.name,
        aliases=_loads(row.aliases_json, []),
        description=getattr(row, "description", "") or "",
        parent_id=row.parent_id,
        hierarchy_path=row.hierarchy_path,
        scale_level=row.scale_level,
        coordinates=coordinates or None,
        shape_ref=row.shape_ref,
        terrain=row.terrain,
        climate=row.climate,
        culture_tag=row.culture_tag,
        default_danger_level=float(row.default_danger_level or 0.0),
        access_level=row.access_level,
        status=row.status,
        metadata=metadata,
    )


def _map_edge_from_row(row: MapEdgeRow) -> MapEdge:
    metadata = _loads(row.metadata_json, {})
    if isinstance(metadata, dict):
        metadata.setdefault("created_at_chapter", int(getattr(row, "created_at_chapter", 0) or 0))
    else:
        metadata = {"created_at_chapter": int(getattr(row, "created_at_chapter", 0) or 0)}
    return MapEdge(
        id=row.id,
        project_id=row.project_id,
        subworld_id=getattr(row, "subworld_id", "") or "",
        from_node_id=row.from_node_id,
        to_node_id=row.to_node_id,
        edge_type=row.edge_type,
        bidirectional=bool(row.bidirectional),
        distance=row.distance,
        travel_time=row.travel_time,
        travel_cost=row.travel_cost,
        risk_level=row.risk_level,
        narrative_cost=row.narrative_cost,
        access_rule_id=row.access_rule_id,
        status=row.status,
        discovered_by_default=bool(row.discovered_by_default),
        visibility_default=row.visibility_default,
        metadata=metadata,
    )


def _ensure_non_negative_edge(edge: MapEdge) -> None:
    for field_name in ("distance", "travel_time", "travel_cost", "risk_level", "narrative_cost"):
        if float(getattr(edge, field_name) or 0.0) < 0:
            raise ValueError(f"MapEdge.{field_name} must be non-negative")


def _created_at_chapter(metadata: dict[str, Any]) -> int:
    try:
        return max(0, int(metadata.get("created_at_chapter") or 0))
    except (TypeError, ValueError):
        return 0


def cognition_views_from_overlays(overlays: list[CognitionOverlay]) -> dict[tuple[str, str], CognitionView]:
    return {
        (str(overlay.observer_type), overlay.observer_id): CognitionView(overlay)
        for overlay in overlays
    }
