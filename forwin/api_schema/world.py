from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from forwin.config import DEFAULT_MINIMAX_BASE_URL, DEFAULT_MINIMAX_MODEL
from forwin.governance import (
    BandCheckpointDetail,
    BlockingReasonInfo,
    DecisionEventInfo,
    NarrativeConstraintInfo,
    PlanTaskItem,
    ProjectGovernanceSettings,
)
from forwin.protocol.subworld import SubWorldSummary


class WorldModelV4DebugResponse(BaseModel):
    project_id: str
    active_world_lines: list[str] = Field(default_factory=list)
    visible_world_lines: list[str] = Field(default_factory=list)
    hidden_world_lines: list[str] = Field(default_factory=list)
    open_gaps: list[str] = Field(default_factory=list)
    planned_reveals: list[dict[str, Any]] = Field(default_factory=list)
    accepted_delta_ids: list[str] = Field(default_factory=list)
    rejected_delta_ids: list[str] = Field(default_factory=list)
    reader_cognition: dict[str, Any] = Field(default_factory=dict)
    protagonist_beliefs: list[str] = Field(default_factory=list)
    promise_debts: list[str] = Field(default_factory=list)


class WorldModelV4LineInfo(BaseModel):
    world_line_id: str
    line_type: str = ""
    title: str = ""
    objective_state_summary: str = ""
    is_visible_onstage: bool = False
    planned_reveal_chapter: int | None = None
    long_term_promise: str = ""
    source_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorldModelV4GapInfo(BaseModel):
    gap_id: str
    status: str = ""
    objective_truth: str = ""
    related_world_line_id: str = ""
    happened_at_story_time: str = ""
    observer_states: dict[str, Any] = Field(default_factory=dict)
    planned_closure: str = ""
    fairness_requirements: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorldModelV4RevealInfo(BaseModel):
    source: str
    gap_id: str = ""
    reveal_event_id: str = ""
    chapter_hint: int | None = None
    from_state: str = ""
    to_state: str = ""
    method: str = ""
    reveal_to_reader: bool = False
    reveal_to_characters: list[str] = Field(default_factory=list)
    fairness_evidence: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorldModelV4ExportResponse(BaseModel):
    project_id: str
    lines: list[WorldModelV4LineInfo] = Field(default_factory=list)
    gaps: list[WorldModelV4GapInfo] = Field(default_factory=list)
    reveals: list[WorldModelV4RevealInfo] = Field(default_factory=list)
    debug: WorldModelV4DebugResponse


class BookStateRuntimeResponse(BaseModel):
    schema_version: str = "book_state.runtime.v1"
    project_id: str
    as_of_chapter: int = 0
    world_node_count: int = 0
    world_edge_count: int = 0
    fact_count: int = 0
    map_node_count: int = 0
    map_edge_count: int = 0
    observer_count: int = 0
    narrative_node_count: int = 0
    narrative_edge_count: int = 0
    active_world_line_ids: list[str] = Field(default_factory=list)
    open_gap_ids: list[str] = Field(default_factory=list)


class BookStatePathResponse(BaseModel):
    schema_version: str = "book_state.path.v1"
    project_id: str
    as_of_chapter: int = 0
    reachable: bool = False
    from_node_id: str = ""
    to_node_id: str = ""
    metric: str = "travel_time"
    total_distance: float = 0.0
    total_travel_time: float = 0.0
    total_travel_cost: float = 0.0
    total_risk: float = 0.0
    total_narrative_cost: float = 0.0
    path_node_ids: list[str] = Field(default_factory=list)
    path_edge_ids: list[str] = Field(default_factory=list)
    blocked_reason: str = ""
    explanation: str = ""


class PersonalityLoadoutUpdateRequest(BaseModel):
    personality_loadout: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class CharacterCreateRequest(BaseModel):
    source: str = "api_manual"
    source_ref: str = ""
    character_id: str = ""
    legacy_entity_id: str = ""
    roster_item_id: str = ""
    name: str
    aliases: list[str] = Field(default_factory=list)
    description: str = ""
    summary: str = ""
    importance: int = 5
    created_at_chapter: int = 0
    profile: dict[str, Any] = Field(default_factory=dict)
    state: dict[str, Any] = Field(default_factory=dict)
    personality_loadout: dict[str, Any] | None = None
    personality_tags: list[str] = Field(default_factory=list)
    personality_policy: str = "auto"
    existing_resolution: str = "get_or_create"
    generic_character_policy: str = "reject_or_group"
    audit_reason: str = ""


class CharacterPersonalityPreviewRequest(BaseModel):
    name: str = ""
    description: str = ""
    summary: str = ""
    profile: dict[str, Any] = Field(default_factory=dict)
    state: dict[str, Any] = Field(default_factory=dict)
    personality_tags: list[str] = Field(default_factory=list)
    source: str = "api_manual"
    source_ref: str = ""


class CharacterPersonalityReassignRequest(BaseModel):
    mode: str = "auto_rule"
    respect_manual_override: bool = True
    force: bool = False
    reason: str = ""


class CharacterPersonalityActiveContextPreviewRequest(BaseModel):
    character_id: str = ""
    character_name: str = ""
    personality_loadout: dict[str, Any] = Field(default_factory=dict)
    scene_flags: list[str] = Field(default_factory=list)
    pressure_triggers: list[str] = Field(default_factory=list)
    relationship_targets: list[str] = Field(default_factory=list)


class MapRuntimeResponse(BaseModel):
    schema_version: str = "map.runtime.v1"
    project_id: str
    subworld_count: int = 0
    region_count: int = 0
    map_node_count: int = 0
    map_edge_count: int = 0
    inter_subworld_edge_count: int = 0
    subworld_ids: list[str] = Field(default_factory=list)


class MapPathResponse(BaseModel):
    schema_version: str = "map.path.v1"
    project_id: str
    reachable: bool = False
    from_node_id: str = ""
    to_node_id: str = ""
    metric: str = "travel_time"
    total_distance: float = 0.0
    total_travel_time: float = 0.0
    total_travel_cost: float = 0.0
    total_risk: float = 0.0
    total_narrative_cost: float = 0.0
    path_node_ids: list[str] = Field(default_factory=list)
    path_edge_ids: list[str] = Field(default_factory=list)
    blocked_reason: str = ""
    explanation: str = ""


class MapEnsureResponse(BaseModel):
    schema_version: str = "map.ensure.v1"
    project_id: str
    summary: dict[str, Any] = Field(default_factory=dict)
    validation_report: dict[str, Any] = Field(default_factory=dict)


class WorldModelSnapshotInfo(BaseModel):
    id: str
    project_id: str
    as_of_chapter: int = 0
    version: int = 1
    status: str = "live"
    source_digest: str = ""
    snapshot: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


class WorldModelPageInfo(BaseModel):
    id: str
    project_id: str
    page_key: str
    page_type: str = "overview"
    title: str
    vault_path: str = ""
    markdown: str = ""
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    projection_kind: str = "world_studio"
    projection_version: str = ""
    source_digest: str = ""
    section_digest: dict[str, str] = Field(default_factory=dict)
    observer_type: str = ""
    observer_id: str = ""
    role_scope: str = ""
    visibility_scope: str = ""
    canon_status: str = "canon_projection"
    content_hash: str = ""
    revision: int = 1
    status: str = "canon_live"
    as_of_chapter: int = 0
    logical_identity_key: str = ""
    canonical_source_type: str = ""
    canonical_source_id: str = ""
    supersedes_page_id: str = ""
    canonical_rank: int = 0
    updated_at: str = ""


class WorldModelConflictInfo(BaseModel):
    id: str
    project_id: str
    conflict_type: str
    severity: str = "warning"
    subject_key: str = ""
    description: str = ""
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    status: str = "open"
    created_at: str = ""
    resolved_at: str = ""


class WorldEditProposalInfo(BaseModel):
    id: str
    project_id: str
    source: str = "obsidian"
    target_page_key: str = ""
    target_node_id: str = ""
    target_field: str = ""
    proposal_type: str = ""
    proposed_patch: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    human_notes: str = ""
    status: str = "pending"
    created_by: str = ""
    created_at: str = ""
    reviewed_at: str = ""
    review_reason: str = ""
    graph_delta_id: str = ""
    projection_refresh: dict[str, Any] = Field(default_factory=dict)


class WorldEditProposalCreateRequest(BaseModel):
    source: str = "world_studio"
    target_page_key: str = ""
    target_node_id: str = ""
    target_field: str = ""
    proposal_type: str = "CanonCorrectionProposal"
    proposed_patch: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    human_notes: str = ""
    created_by: str = "world_studio"


class WorldModelExportRequest(BaseModel):
    vault_root: str = ""


class WorldModelExportResponse(BaseModel):
    ok: bool = True
    project_id: str = ""
    vault_root: str = ""
    exported_count: int = 0
    message: str = ""


class WorldModelImportRequest(BaseModel):
    vault_root: str = ""


class WorldModelImportResponse(BaseModel):
    ok: bool = True
    project_id: str = ""
    vault_root: str = ""
    proposal_count: int = 0
    changed_paths: list[str] = Field(default_factory=list)
    message: str = ""


class WorldEditProposalReviewRequest(BaseModel):
    status: str
    reason: str = ""
    forced_accept_reason: str = ""


__all__ = [
    'WorldModelV4DebugResponse',
    'WorldModelV4LineInfo',
    'WorldModelV4GapInfo',
    'WorldModelV4RevealInfo',
    'WorldModelV4ExportResponse',
    'BookStateRuntimeResponse',
    'BookStatePathResponse',
    'PersonalityLoadoutUpdateRequest',
    'CharacterCreateRequest',
    'CharacterPersonalityPreviewRequest',
    'CharacterPersonalityReassignRequest',
    'CharacterPersonalityActiveContextPreviewRequest',
    'MapRuntimeResponse',
    'MapPathResponse',
    'MapEnsureResponse',
    'WorldModelSnapshotInfo',
    'WorldModelPageInfo',
    'WorldModelConflictInfo',
    'WorldEditProposalInfo',
    'WorldEditProposalCreateRequest',
    'WorldModelExportRequest',
    'WorldModelExportResponse',
    'WorldModelImportRequest',
    'WorldModelImportResponse',
    'WorldEditProposalReviewRequest',
]
