from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forwin.protocol.book_state import MapEdge, MapNode, PathMetric, PathResult


SCHEME_C_NAME = "方案 C：Graph-based Weighted Map Generation"


class _MapProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class SubWorldType(str, Enum):
    CONTINENT = "continent"
    PLANET = "planet"
    PLANE = "plane"
    REALM = "realm"
    STAR_SECTOR = "star_sector"
    UNDERGROUND = "underground"
    DIVINE_DOMAIN = "divine_domain"
    DEMON_REALM = "demon_realm"
    VIRTUAL_WORLD = "virtual_world"


class RegionType(str, Enum):
    KINGDOM = "kingdom"
    SECT_DOMAIN = "sect_domain"
    PROVINCE = "province"
    MOUNTAIN = "mountain"
    FOREST = "forest"
    WASTELAND = "wasteland"
    SEA = "sea"
    CITY_CLUSTER = "city_cluster"
    BORDERLAND = "borderland"
    RUIN_ZONE = "ruin_zone"
    BATTLEFRONT = "battlefront"
    RESOURCE_ZONE = "resource_zone"
    FORBIDDEN_ZONE = "forbidden_zone"


class MapAnchorNodeSpec(_MapProtocolModel):
    name: str
    node_type: str
    region_role: str
    narrative_function: str
    required: bool = True
    tags: list[str] = Field(default_factory=list)
    source_node_id: str = ""
    source_region_id: str = ""
    source_subworld_id: str = ""


class SubWorldMapSpec(_MapProtocolModel):
    project_id: str
    subworld_id: str
    name: str
    subworld_type: str
    culture_tags: list[str] = Field(default_factory=list)
    terrain_tags: list[str] = Field(default_factory=list)
    scale_level: str = "world"
    target_region_count: int = Field(default=6, ge=1)
    target_node_count: int = Field(default=36, ge=1)
    target_edge_density: float = Field(default=1.6, ge=1.0)
    required_region_roles: list[str] = Field(default_factory=list)
    required_anchor_nodes: list[MapAnchorNodeSpec] = Field(default_factory=list)
    required_connection_roles: list[str] = Field(default_factory=list)
    danger_profile: dict[str, Any] = Field(default_factory=dict)
    resource_profile: dict[str, Any] = Field(default_factory=dict)
    faction_profile: dict[str, Any] = Field(default_factory=dict)
    narrative_functions: list[str] = Field(default_factory=list)
    generation_seed: int = 0

    @model_validator(mode="after")
    def _target_counts_cover_required_baseline(self) -> "SubWorldMapSpec":
        minimum_nodes = len(self.required_anchor_nodes) + self.target_region_count * 3
        if self.target_node_count < minimum_nodes:
            raise ValueError(
                "target_node_count must cover required anchors plus three baseline nodes per region"
            )
        return self


class SubWorldNode(_MapProtocolModel):
    id: str
    project_id: str
    name: str = ""
    subworld_type: str = ""
    scale_level: str = "world"
    culture_profile: dict[str, Any] = Field(default_factory=dict)
    terrain_profile: dict[str, Any] = Field(default_factory=dict)
    danger_profile: dict[str, Any] = Field(default_factory=dict)
    generation_seed: int = 0
    map_status: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class RegionNode(_MapProtocolModel):
    id: str
    project_id: str
    subworld_id: str
    region_type: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    description: str = ""
    terrain: str = ""
    culture_tag: str = ""
    controlling_faction_id: str = ""
    danger_level: float = Field(default=0.0, ge=0.0)
    node_ids: list[str] = Field(default_factory=list)
    boundary_node_ids: list[str] = Field(default_factory=list)
    entry_node_ids: list[str] = Field(default_factory=list)
    status: str = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)


class RegionEdge(_MapProtocolModel):
    id: str
    project_id: str
    subworld_id: str
    from_region_id: str
    to_region_id: str
    edge_type: str = "adjacent"
    bidirectional: bool = True
    distance: float = Field(default=0.0, ge=0.0)
    travel_time: float = Field(default=0.0, ge=0.0)
    risk_level: float = Field(default=0.0, ge=0.0)
    status: str = "open"
    metadata: dict[str, Any] = Field(default_factory=dict)


class InterSubWorldEdge(_MapProtocolModel):
    id: str
    project_id: str
    from_subworld_id: str
    to_subworld_id: str
    map_edge_id: str
    edge_type: str = "world_gate"
    metadata: dict[str, Any] = Field(default_factory=dict)


class InterSubWorldConnectionSpec(_MapProtocolModel):
    project_id: str
    from_subworld_id: str
    to_subworld_id: str
    from_node_id: str = ""
    to_node_id: str = ""
    edge_type: str = "world_gate"
    bidirectional: bool = True
    hidden: bool = False
    access_rule_id: str = ""
    distance: float = Field(default=1000.0, ge=0.0)
    travel_time: float = Field(default=120.0, ge=0.0)
    travel_cost: float = Field(default=500.0, ge=0.0)
    risk_level: float = Field(default=5.0, ge=0.0)
    narrative_cost: float = Field(default=10.0, ge=0.0)
    cost_multiplier: float = Field(default=1.0, ge=0.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MapValidationReport(_MapProtocolModel):
    valid: bool = True
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)

    @field_validator("errors", "warnings", mode="before")
    @classmethod
    def _default_list(cls, value: Any) -> list[str]:
        return list(value or [])


class MapGenerationResult(_MapProtocolModel):
    project_id: str
    subworld_id: str
    generation_seed: int
    algorithm: str = "anchor_graph_v1"
    regions: list[RegionNode] = Field(default_factory=list)
    region_edges: list[RegionEdge] = Field(default_factory=list)
    map_nodes: list[MapNode] = Field(default_factory=list)
    map_edges: list[MapEdge] = Field(default_factory=list)
    validation_report: MapValidationReport = Field(default_factory=MapValidationReport)
    summary: dict[str, Any] = Field(default_factory=dict)


class BookMapGenerationResult(_MapProtocolModel):
    project_id: str
    scheme: str = SCHEME_C_NAME
    subworld_results: list[MapGenerationResult] = Field(default_factory=list)
    inter_subworld_edges: list[MapEdge] = Field(default_factory=list)
    validation_report: MapValidationReport = Field(default_factory=MapValidationReport)
    summary: dict[str, Any] = Field(default_factory=dict)


class BookMapRuntime(_MapProtocolModel):
    project_id: str
    subworlds_by_id: dict[str, SubWorldNode] = Field(default_factory=dict)
    inter_subworld_edges_by_id: dict[str, InterSubWorldEdge] = Field(default_factory=dict)
    regions_by_id: dict[str, RegionNode] = Field(default_factory=dict)
    region_edges_by_id: dict[str, RegionEdge] = Field(default_factory=dict)
    map_nodes_by_id: dict[str, MapNode] = Field(default_factory=dict)
    map_edges_by_id: dict[str, MapEdge] = Field(default_factory=dict)
    outgoing_edges: dict[str, list[str]] = Field(default_factory=dict)
    incoming_edges: dict[str, list[str]] = Field(default_factory=dict)
    regions_by_subworld: dict[str, list[str]] = Field(default_factory=dict)
    nodes_by_region: dict[str, list[str]] = Field(default_factory=dict)
    path_cache: dict[tuple[str, str, str, str], PathResult] = Field(default_factory=dict)


__all__ = [
    "BookMapRuntime",
    "BookMapGenerationResult",
    "InterSubWorldEdge",
    "InterSubWorldConnectionSpec",
    "MapAnchorNodeSpec",
    "MapEdge",
    "MapGenerationResult",
    "MapNode",
    "MapValidationReport",
    "PathMetric",
    "PathResult",
    "RegionEdge",
    "RegionNode",
    "RegionType",
    "SCHEME_C_NAME",
    "SubWorldMapSpec",
    "SubWorldNode",
    "SubWorldType",
]
