from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


SignalSeverity = Literal["info", "warning", "error"]
SignalScope = Literal["body", "chapter", "character", "ledger", "book"]


class CanonQualitySignal(BaseModel):
    signal_id: str
    project_id: str
    chapter_number: int
    signal_type: str
    severity: SignalSeverity = "warning"
    target_scope: SignalScope = "chapter"
    subject_key: str = ""
    description: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    span_start: int | None = None
    span_end: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    status: Literal["open", "resolved", "waived"] = "open"


class CanonAdmissionGateResult(BaseModel):
    project_id: str
    chapter_number: int
    draft_id: str = ""
    review_id: str = ""
    commit_allowed: bool
    verdict: Literal["pass", "warn", "fail"]
    blocking_issue_count: int = 0
    warning_issue_count: int = 0
    open_terminal_obligation_count: int = 0
    deterministic_issue_refs: list[str] = Field(default_factory=list)
    llm_issue_refs: list[str] = Field(default_factory=list)
    residual_issue_refs: list[str] = Field(default_factory=list)
    required_repair_scope: Literal["draft", "chapter_plan", "band", "arc", "book"] | None = None
    gate_summary: str = ""


class CharacterStateTransition(BaseModel):
    project_id: str
    character_id: str = ""
    character_name: str
    chapter_number: int
    transition_type: str
    from_state: str = ""
    to_state: str = ""
    terminality: Literal["none", "soft_terminal", "hard_terminal"] = "none"
    can_participate: bool = True
    requires_bridge_from_transition_id: str = ""
    bridge_event_id: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)


class ArtifactLedgerEntry(BaseModel):
    project_id: str
    collection_key: str = "main"
    collection_name: str = "core_artifacts"
    target_total: int = 0
    chapter_number: int
    mentioned_index: int | None = None
    mentioned_remaining: int | None = None
    collected_count_after: int = 0
    new_items: list[str] = Field(default_factory=list)
    consumed_items: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: float = 1.0
    status: Literal["consistent", "warning", "conflict"] = "consistent"
    payload: dict[str, Any] = Field(default_factory=dict)


class CountdownLedgerEntry(BaseModel):
    project_id: str
    countdown_key: str = "main"
    label: str = "main"
    chapter_number: int
    normalized_remaining_minutes: int
    raw_mention: str = ""
    is_reset_event: bool = False
    is_branch_clock: bool = False
    is_resolution_event: bool = False
    previous_remaining_minutes: int | None = None
    status: Literal["consistent", "warning", "conflict", "resolved"] = "consistent"
    evidence_refs: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)


class RevealRegistryEntry(BaseModel):
    project_id: str
    reveal_key: str
    claim_summary: str
    first_revealed_chapter: int
    latest_chapter: int
    repeat_count: int = 0
    status: Literal["new", "escalated", "repeated", "paid_off"] = "new"
    subject_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)


class ChapterBodyMetrics(BaseModel):
    project_id: str
    chapter_number: int
    draft_id: str = ""
    paragraph_hashes: list[str] = Field(default_factory=list)
    dialogue_fingerprints: list[str] = Field(default_factory=list)
    scene_fingerprints: list[str] = Field(default_factory=list)
    duplicate_spans: list[dict[str, int]] = Field(default_factory=list)
    style_motifs: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)


class IdentityRoleFact(BaseModel):
    project_id: str
    character_id: str = ""
    character_name: str
    alias: str = ""
    role_label: str = ""
    relationship_to_protagonist: str = ""
    faction_alignment: str = ""
    temporal_valid_from: int
    temporal_valid_until: int | None = None
    truth_value: Literal["true", "false", "unknown", "in_world_lie"] = "true"
    confidence: float = 1.0
    evidence_refs: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)


class StyleTelemetry(BaseModel):
    project_id: str
    chapter_number: int
    draft_id: str = ""
    style_motifs: list[str] = Field(default_factory=list)
    top_repeated_motifs: list[str] = Field(default_factory=list)
    dialogue_templates: list[str] = Field(default_factory=list)
    rolling_window_density: float = 0.0
    metrics: dict[str, Any] = Field(default_factory=dict)


def make_signal_id(
    project_id: str,
    chapter_number: int,
    signal_type: str,
    subject_key: str,
    index: int = 1,
) -> str:
    safe_subject = str(subject_key or "subject").replace(" ", "_")
    return f"{project_id}:{chapter_number}:{signal_type}:{safe_subject}:{index}"
