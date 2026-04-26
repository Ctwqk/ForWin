from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class WorldNodeType(_StrEnum):
    CHARACTER = "character"
    FACTION = "faction"
    GROUP = "group"
    ITEM = "item"
    RESOURCE = "resource"
    ABILITY = "ability"
    RULE = "rule"
    ACTIVITY = "activity"
    SITE_STATE = "site_state"
    EVENT = "event"
    FACT = "fact"
    OBJECTIVE = "objective"


class WorldEdgeFamily(_StrEnum):
    ORGANIZATION = "organization"
    POSSESSION = "possession"
    CONTROL_CONFLICT = "control_conflict"
    SOCIAL = "social"
    CAPABILITY_RULE = "capability_rule"
    EVENT_CAUSAL = "event_causal"
    FACT_EVIDENCE = "fact_evidence"
    ACTIVITY_OBJECTIVE = "activity_objective"


WORLD_EDGE_TYPES_BY_FAMILY: dict[str, set[str]] = {
    "organization": {"member_of", "leader_of", "subordinate_to", "branch_of", "part_of"},
    "possession": {"owns", "possesses", "equipped_with", "bound_to", "created_by", "grants_ability"},
    "control_conflict": {"controls", "governs", "claims", "occupies", "protects", "besieges", "blockades"},
    "social": {"family_of", "ally_of", "enemy_of", "mentor_of", "romantic_with", "trusts", "distrusts", "owes_debt_to"},
    "capability_rule": {"has_ability", "uses", "requires", "counters", "weak_against", "forbidden_by"},
    "event_causal": {"participates_in", "witnesses", "causes", "prevents", "enables", "results_in", "damages"},
    "fact_evidence": {"supports", "contradicts", "evidence_for", "proves", "disproves"},
    "activity_objective": {"organizes", "hosts", "competes_in", "rewards", "targets", "advances_objective"},
}
WORLD_EDGE_TYPES: set[str] = {
    edge_type
    for edge_types in WORLD_EDGE_TYPES_BY_FAMILY.values()
    for edge_type in edge_types
}


class Directionality(_StrEnum):
    DIRECTED = "directed"
    UNDIRECTED = "undirected"
    SYMMETRIC = "symmetric"


class MapNodeType(_StrEnum):
    WORLD_AREA = "world_area"
    REGION = "region"
    SETTLEMENT = "settlement"
    SITE = "site"
    BUILDING = "building"
    ROOM = "room"
    ZONE = "zone"
    WAYPOINT = "waypoint"
    LANDMARK = "landmark"
    CAMP = "camp"
    DUNGEON_ROOM = "dungeon_room"


class MapEdgeType(_StrEnum):
    ROAD = "road"
    PATH = "path"
    RIVER = "river"
    SEA_ROUTE = "sea_route"
    FLIGHT_ROUTE = "flight_route"
    TUNNEL = "tunnel"
    PORTAL = "portal"
    RAIL = "rail"
    BORDER_CROSSING = "border_crossing"
    HIDDEN_ROUTE = "hidden_route"
    MOUNTAIN_PASS = "mountain_pass"
    SPACE_ROUTE = "space_route"
    WORLD_GATE = "world_gate"


class ObserverType(_StrEnum):
    READER = "reader"
    CHARACTER = "character"
    FACTION = "faction"
    GROUP = "group"
    SYSTEM = "system"


class CognitionState(_StrEnum):
    HIDDEN = "hidden"
    UNKNOWN = "unknown"
    HINTED = "hinted"
    SUSPECTED = "suspected"
    PARTIALLY_KNOWN = "partially_known"
    KNOWN = "known"
    CONFIRMED = "confirmed"
    MISLED = "misled"
    FALSE = "false"
    STALE = "stale"


class PatchOp(_StrEnum):
    CREATE = "create"
    SET = "set"
    MERGE = "merge"
    APPEND = "append"
    REMOVE = "remove"
    REPLACE = "replace"
    DEACTIVATE = "deactivate"


class GraphDeltaType(_StrEnum):
    WORLD_STATE = "world_state"
    MAP_STATE = "map_state"
    COGNITION = "cognition"
    NARRATIVE_CONTROL = "narrative_control"
    REPAIR = "repair"
    RETCON_BLOCK = "retcon_block"


class PathMetric(_StrEnum):
    PHYSICAL_DISTANCE = "physical_distance"
    DISTANCE = "distance"
    TRAVEL_TIME = "travel_time"
    TRAVEL_COST = "travel_cost"
    RISK = "risk"
    RISK_COST = "risk_cost"
    NARRATIVE_COST = "narrative_cost"
    COMPOSITE = "composite"
    COMPOSITE_COST = "composite_cost"
    KNOWN_DISTANCE = "known_distance"


class _BookStateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class WorldNode(_BookStateModel):
    id: str
    project_id: str
    node_type: WorldNodeType | str
    name: str = ""
    aliases: list[str] = Field(default_factory=list)
    description: str = ""
    importance: int = Field(default=5, ge=1, le=10)
    created_at_chapter: int = 0
    retired_at_chapter: int | None = None
    is_active: bool = True
    profile: dict[str, Any] = Field(default_factory=dict)
    state: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("node_type")
    @classmethod
    def _valid_node_type(cls, value: WorldNodeType | str) -> str:
        text = str(getattr(value, "value", value) or "")
        if text not in {item.value for item in WorldNodeType}:
            raise ValueError(f"unknown world node type: {text}")
        return text


class FactNode(_BookStateModel):
    id: str
    project_id: str
    proposition: str
    fact_type: str = ""
    truth_value: str = "true"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    related_node_refs: list[str] = Field(default_factory=list)
    related_edge_refs: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    created_at_chapter: int = 0
    happened_at_story_time: str = ""
    contradiction_refs: list[str] = Field(default_factory=list)
    sensitivity_level: str = ""
    narrative_function: str = ""
    state: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorldEdge(_BookStateModel):
    id: str
    project_id: str
    source_id: str
    target_id: str
    edge_type: str
    edge_family: WorldEdgeFamily | str
    directionality: Directionality | str = Directionality.DIRECTED
    weight: float = 1.0
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    established_at_chapter: int = 0
    ended_at_chapter: int | None = None
    is_active: bool = True
    visibility_default: str = "visible"
    state: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _valid_edge_type(self) -> "WorldEdge":
        family = str(getattr(self.edge_family, "value", self.edge_family) or "")
        if family not in WORLD_EDGE_TYPES_BY_FAMILY:
            raise ValueError(f"unknown world edge family: {family}")
        if self.edge_type not in WORLD_EDGE_TYPES_BY_FAMILY[family]:
            raise ValueError(f"edge_type {self.edge_type!r} does not belong to {family!r}")
        return self


class MapNode(_BookStateModel):
    id: str
    project_id: str
    subworld_id: str = ""
    region_id: str = ""
    node_type: MapNodeType | str
    name: str = ""
    aliases: list[str] = Field(default_factory=list)
    description: str = ""
    parent_id: str = ""
    hierarchy_path: str = ""
    scale_level: str = ""
    coordinates: dict[str, Any] | None = None
    shape_ref: str = ""
    terrain: str = ""
    climate: str = ""
    culture_tag: str = ""
    default_danger_level: float = 0.0
    access_level: str = "open"
    status: str = "normal"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("node_type")
    @classmethod
    def _valid_map_node_type(cls, value: MapNodeType | str) -> str:
        text = str(getattr(value, "value", value) or "")
        if text not in {item.value for item in MapNodeType}:
            raise ValueError(f"unknown map node type: {text}")
        return text


class MapEdge(_BookStateModel):
    id: str
    project_id: str
    subworld_id: str = ""
    from_node_id: str
    to_node_id: str
    edge_type: MapEdgeType | str
    bidirectional: bool = False
    distance: float = Field(default=0.0, ge=0.0)
    travel_time: float = Field(default=0.0, ge=0.0)
    travel_cost: float = Field(default=0.0, ge=0.0)
    risk_level: float = Field(default=0.0, ge=0.0)
    narrative_cost: float = Field(default=0.0, ge=0.0)
    access_rule_id: str = ""
    status: str = "open"
    discovered_by_default: bool = True
    visibility_default: str = "visible"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("edge_type")
    @classmethod
    def _valid_map_edge_type(cls, value: MapEdgeType | str) -> str:
        text = str(getattr(value, "value", value) or "")
        if text not in {item.value for item in MapEdgeType}:
            raise ValueError(f"unknown map edge type: {text}")
        return text

    @field_validator("distance", "travel_time", "travel_cost", "risk_level", "narrative_cost", mode="before")
    @classmethod
    def _default_non_negative_weight(cls, value: Any) -> float:
        if value is None or value == "":
            return 0.0
        return float(value)


class PathResult(_BookStateModel):
    reachable: bool
    from_node_id: str
    to_node_id: str
    metric: str = PathMetric.TRAVEL_TIME
    total_distance: float = 0.0
    total_travel_time: float = 0.0
    total_travel_cost: float = 0.0
    total_risk: float = 0.0
    total_narrative_cost: float = 0.0
    path_node_ids: list[str] = Field(default_factory=list)
    path_edge_ids: list[str] = Field(default_factory=list)
    blocked_reason: str = ""
    explanation: str = ""


class NodePatch(_BookStateModel):
    node_id: str
    node_type: WorldNodeType | str
    op: PatchOp | str
    field_path: str = ""
    old_value: Any = None
    new_value: Any = None
    reason: str = ""
    visibility_default: str = "visible"


class EdgePatch(_BookStateModel):
    edge_id: str
    op: PatchOp | str
    source_id: str = ""
    target_id: str = ""
    edge_type: str = ""
    edge_family: WorldEdgeFamily | str = ""
    field_path: str = ""
    old_value: Any = None
    new_value: Any = None
    reason: str = ""


class FactPatch(_BookStateModel):
    fact_id: str
    op: PatchOp | str
    proposition: str = ""
    truth_value: str = ""
    related_refs: list[str] = Field(default_factory=list)
    old_value: Any = None
    new_value: Any = None
    reason: str = ""
    sensitivity_level: str = ""


class MapPatch(_BookStateModel):
    target_type: str
    target_id: str
    op: PatchOp | str
    field_path: str = ""
    old_value: Any = None
    new_value: Any = None
    reason: str = ""
    discovered_by_default: bool | None = None
    access_change: str = ""
    affected_path_cache_keys: list[str] = Field(default_factory=list)
    visibility_default: str = "visible"


class CognitionPatch(_BookStateModel):
    observer_type: ObserverType | str
    observer_id: str
    op: PatchOp | str
    field_path: str
    old_value: Any = None
    new_value: Any = None
    reason: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class NarrativePatch(_BookStateModel):
    target_ref: str
    op: PatchOp | str
    field_path: str = ""
    old_value: Any = None
    new_value: Any = None
    reason: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class GraphDelta(_BookStateModel):
    id: str
    project_id: str
    chapter_number: int = 0
    story_time: str = ""
    delta_type: GraphDeltaType | str = GraphDeltaType.WORLD_STATE
    source_type: str = ""
    source_id: str = ""
    world_line_id: str = ""
    summary: str = ""
    node_patches: list[NodePatch] = Field(default_factory=list)
    edge_patches: list[EdgePatch] = Field(default_factory=list)
    fact_patches: list[FactPatch] = Field(default_factory=list)
    map_patches: list[MapPatch] = Field(default_factory=list)
    cognition_patches: list[CognitionPatch] = Field(default_factory=list)
    narrative_patches: list[NarrativePatch] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApprovedGraphDeltaSet(_BookStateModel):
    project_id: str
    chapter_number: int
    graph_deltas: list[GraphDelta] = Field(default_factory=list)
    approved_by: list[str] = Field(default_factory=list)
    review_verdict_id: str = ""
    forced_accept_reason: str = ""


class BookStateCompileResult(_BookStateModel):
    project_id: str
    chapter_number: int
    compiler_run_id: str = ""
    committed: bool = False
    graph_delta_ids: list[str] = Field(default_factory=list)
    world_snapshot_id: str = ""
    map_snapshot_id: str = ""
    cognition_snapshot_ids: list[str] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)
    forced_accept_reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class CognitionOverlay(_BookStateModel):
    id: str
    project_id: str
    observer_type: ObserverType | str
    observer_id: str
    as_of_chapter: int = 0
    as_of_story_time: str = ""
    visible_refs: list[str] = Field(default_factory=list)
    hidden_refs: list[str] = Field(default_factory=list)
    suspected_refs: list[str] = Field(default_factory=list)
    confirmed_refs: list[str] = Field(default_factory=list)
    field_overrides: dict[str, Any] = Field(default_factory=dict)
    false_nodes: dict[str, WorldNode] = Field(default_factory=dict)
    false_edges: dict[str, WorldEdge | MapEdge] = Field(default_factory=dict)
    false_facts: dict[str, FactNode] = Field(default_factory=dict)
    evidence_by_ref: dict[str, list[str]] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class NarrativeNode(_BookStateModel):
    id: str
    project_id: str
    node_type: str
    title: str = ""
    status: str = "active"
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class NarrativeEdge(_BookStateModel):
    id: str
    project_id: str
    source_id: str
    target_id: str
    edge_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorldSnapshot(_BookStateModel):
    id: str
    project_id: str
    as_of_chapter: int = 0
    as_of_story_time: str = ""
    base_snapshot_id: str = ""
    world_node_state_index: dict[str, dict[str, Any]] = Field(default_factory=dict)
    active_edge_ids: list[str] = Field(default_factory=list)
    active_fact_ids: list[str] = Field(default_factory=list)
    active_world_line_ids: list[str] = Field(default_factory=list)
    open_gap_ids: list[str] = Field(default_factory=list)
    source_delta_ids: list[str] = Field(default_factory=list)
    built_at: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class MapSnapshot(_BookStateModel):
    id: str
    project_id: str
    as_of_chapter: int = 0
    map_node_index: dict[str, MapNode] = Field(default_factory=dict)
    map_edge_index: dict[str, MapEdge] = Field(default_factory=dict)
    blocked_edge_ids: list[str] = Field(default_factory=list)
    hidden_edge_ids: list[str] = Field(default_factory=list)
    built_at: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class CognitionSnapshot(_BookStateModel):
    id: str
    project_id: str
    observer_type: ObserverType | str
    observer_id: str
    as_of_chapter: int = 0
    overlay_id: str = ""
    visible_refs: list[str] = Field(default_factory=list)
    suspected_refs: list[str] = Field(default_factory=list)
    confirmed_refs: list[str] = Field(default_factory=list)
    built_at: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
