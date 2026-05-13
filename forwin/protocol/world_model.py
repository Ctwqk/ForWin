from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class EvidenceRef(BaseModel):
    source_type: str
    source_id: str = ""
    chapter_number: int = 0
    summary: str = ""


class WorldModelPage(BaseModel):
    id: str = ""
    page_key: str
    page_type: str = "overview"
    title: str
    vault_path: str = ""
    markdown: str = ""
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    content_hash: str = ""
    revision: int = 1
    status: str = "canon_live"
    as_of_chapter: int = 0
    logical_identity_key: str = ""
    canonical_source_type: str = ""
    canonical_source_id: str = ""
    supersedes_page_id: str = ""
    canonical_rank: int = 0


class WorldModelLink(BaseModel):
    id: str = ""
    source_page_id: str = ""
    target_page_id: str = ""
    relation_type: str = "related"
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)


class WorldModelConflict(BaseModel):
    id: str = ""
    conflict_type: str = ""
    severity: Literal["info", "warning", "error"] = "warning"
    subject_key: str = ""
    description: str = ""
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    status: str = "open"


class WorldEditProposal(BaseModel):
    id: str = ""
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
    review_reason: str = ""
    graph_delta_id: str = ""


class WorldModelSnapshot(BaseModel):
    id: str = ""
    project_id: str
    as_of_chapter: int = 0
    version: int = 1
    status: str = "live"
    snapshot: dict[str, Any] = Field(default_factory=dict)
    source_digest: str = ""
    source_refs: list[EvidenceRef] = Field(default_factory=list)


class WorldModelDelta(BaseModel):
    project_id: str
    as_of_chapter: int = 0
    source_refs: list[EvidenceRef] = Field(default_factory=list)
    changes: dict[str, Any] = Field(default_factory=dict)


class WorldContextPack(BaseModel):
    snapshot_id: str = ""
    as_of_chapter: int = 0
    world_model_digest: str = ""
    world_model_refs: dict[str, str] = Field(default_factory=dict)
    relevant_world_pages: list[WorldModelPage] = Field(default_factory=list)
    active_world_conflicts: list[WorldModelConflict] = Field(default_factory=list)
    active_secrets: list[WorldModelPage] = Field(default_factory=list)
    active_promises: list[WorldModelPage] = Field(default_factory=list)
    active_resource_constraints: list[WorldModelPage] = Field(default_factory=list)
    active_institution_rules: list[WorldModelPage] = Field(default_factory=list)


class WorldModelExportResult(BaseModel):
    ok: bool = True
    project_id: str = ""
    vault_root: str = ""
    exported_count: int = 0
    message: str = ""


class WorldModelImportResult(BaseModel):
    ok: bool = True
    project_id: str = ""
    vault_root: str = ""
    proposal_count: int = 0
    changed_paths: list[str] = Field(default_factory=list)
    message: str = ""
