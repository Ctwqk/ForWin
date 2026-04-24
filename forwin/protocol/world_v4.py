from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class _StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class ObserverType(_StrEnum):
    READER = "reader"
    CHARACTER = "character"
    FACTION = "faction"
    GROUP = "group"
    SYSTEM = "system"


class DeltaKind(_StrEnum):
    VISIBLE = "visible"
    OFFSCREEN = "offscreen"
    HINT = "hint"
    KNOWLEDGE = "knowledge"
    REVEAL = "reveal"
    FALSE_BELIEF = "false_belief"


class DeltaSourceType(_StrEnum):
    CHARACTER_ACTION = "character_action"
    FACTION_ACTION = "faction_action"
    ENVIRONMENTAL_EVENT = "environmental_event"
    INSTITUTIONAL_PROCESS = "institutional_process"
    ECONOMIC_PRESSURE = "economic_pressure"
    TECHNOLOGICAL_OR_MAGIC_PROCESS = "technological_or_magic_process"
    INFORMATION_SPREAD = "information_spread"
    SCHEDULED_CYCLE = "scheduled_cycle"
    ACCIDENT_OR_RANDOMNESS = "accident_or_randomness"


class TruthRelation(_StrEnum):
    TRUE = "true"
    FALSE = "false"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class BeliefStatus(_StrEnum):
    ACTIVE = "active"
    OUTDATED = "outdated"
    MANIPULATED = "manipulated"
    SUSPECTED = "suspected"
    DISPUTED = "disputed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class VisibilityState(_StrEnum):
    HIDDEN = "hidden"
    UNKNOWN = "unknown"
    HINTED = "hinted"
    SUSPECTED = "suspected"
    PARTIALLY_REVEALED = "partially_revealed"
    PARTIALLY_KNOWN = "partially_known"
    KNOWN = "known"
    CONFIRMED = "confirmed"
    MISLED = "misled"


class GapStatus(_StrEnum):
    OPEN = "open"
    HINTED = "hinted"
    PARTIALLY_CLOSED = "partially_closed"
    CLOSED = "closed"
    ABANDONED = "abandoned"


class KnowledgeUpdateType(_StrEnum):
    HINT = "hint"
    REVEAL = "reveal"
    CONFIRM = "confirm"
    DISCONFIRM = "disconfirm"
    MISLEAD = "mislead"
    DECEIVE = "deceive"
    CORRECT = "correct"
    RETCON_BLOCK = "retcon_block"
    DRAMATIC_IRONY = "dramatic_irony"


class SourceRef(BaseModel):
    source_type: str = ""
    source_id: str = ""
    chapter_number: int | None = None
    label: str = ""
    quote: str = ""


class DeltaSource(BaseModel):
    source_type: DeltaSourceType
    actor_id: str = ""
    mechanism: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class ObserverDeltaState(BaseModel):
    observer_type: ObserverType
    observer_id: str
    visibility: VisibilityState = VisibilityState.UNKNOWN
    cognition_state: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    last_updated_chapter: int | None = None
    last_updated_story_time: str = ""


class WorldLine(BaseModel):
    world_line_id: str
    project_id: str
    line_type: str
    title: str = ""
    participants: list[str] = Field(default_factory=list)
    objective_state_summary: str = ""
    is_visible_onstage: bool = False
    planned_reveal_chapter: int | None = None
    long_term_promise: str = ""
    source_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorldDelta(BaseModel):
    delta_id: str
    project_id: str
    world_line_id: str
    delta_kind: DeltaKind
    summary: str
    objective_story_time: str = ""
    narrative_chapter: int | None = None
    source: DeltaSource
    affected_entities: list[str] = Field(default_factory=list)
    affected_factions: list[str] = Field(default_factory=list)
    affected_locations: list[str] = Field(default_factory=list)
    affected_resources: list[str] = Field(default_factory=list)
    affected_rules: list[str] = Field(default_factory=list)
    observer_states: dict[str, ObserverDeltaState] = Field(default_factory=dict)
    allowed_for_canon: bool = True
    source_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Belief(BaseModel):
    belief_id: str
    holder_type: ObserverType
    holder_id: str
    proposition: str
    truth_relation: TruthRelation
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    belief_status: BeliefStatus = BeliefStatus.ACTIVE
    evidence_sources: list[str] = Field(default_factory=list)
    created_at_chapter: int | None = None
    created_at_story_time: str = ""
    contradicted_by: list[str] = Field(default_factory=list)
    last_updated_at_chapter: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CognitionState(BaseModel):
    cognition_state_id: str = ""
    project_id: str
    observer_type: ObserverType
    observer_id: str
    as_of_chapter: int | None = None
    as_of_story_time: str = ""
    beliefs: list[Belief] = Field(default_factory=list)
    known_delta_ids: list[str] = Field(default_factory=list)
    suspected_gap_ids: list[str] = Field(default_factory=list)
    visibility_by_delta: dict[str, VisibilityState] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class GapObserverState(BaseModel):
    observer_type: ObserverType
    observer_id: str
    visibility: VisibilityState = VisibilityState.UNKNOWN
    cognition_state: str = ""
    belief_ids: list[str] = Field(default_factory=list)
    first_relevant_chapter: int | None = None
    last_updated_chapter: int | None = None
    last_updated_story_time: str = ""


class KnowledgeGap(BaseModel):
    gap_id: str
    project_id: str
    objective_truth: str
    happened_at_story_time: str = ""
    related_world_line_id: str = ""
    observer_states: dict[str, GapObserverState] = Field(default_factory=dict)
    narrative_function: str = ""
    planned_closure: str = ""
    maximum_safe_delay: int | None = None
    fairness_requirements: list[str] = Field(default_factory=list)
    status: GapStatus = GapStatus.OPEN
    source_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RevealEvent(BaseModel):
    reveal_event_id: str
    project_id: str
    reveals_fact_id: str = ""
    reveals_delta_id: str = ""
    related_gap_id: str = ""
    reveal_to_reader: bool = False
    reveal_to_characters: list[str] = Field(default_factory=list)
    reveal_method: str = ""
    from_state: VisibilityState = VisibilityState.UNKNOWN
    to_state: VisibilityState = VisibilityState.UNKNOWN
    emotional_effect: str = ""
    narrative_function: str = ""
    fairness_evidence: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeUpdateEvent(BaseModel):
    update_event_id: str
    project_id: str
    update_type: KnowledgeUpdateType
    observer_type: ObserverType
    observer_id: str
    related_gap_id: str = ""
    related_delta_id: str = ""
    from_state: VisibilityState = VisibilityState.UNKNOWN
    to_state: VisibilityState = VisibilityState.UNKNOWN
    evidence_refs: list[str] = Field(default_factory=list)
    chapter_number: int | None = None
    story_time: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReaderExperienceDelta(BaseModel):
    reader_experience_delta_id: str
    project_id: str
    chapter_number: int
    reader_state_before: str = ""
    reader_state_after: str = ""
    cognition_transition: str = ""
    payoff_type: str = ""
    reward_tags: list[str] = Field(default_factory=list)
    emotional_effect: str = ""
    promise_debt_change: int = 0
    next_desire: str = ""
    fairness_evidence: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorldModelSnapshot(BaseModel):
    snapshot_id: str
    project_id: str
    as_of_chapter: int | None = None
    as_of_story_time: str = ""
    active_world_line_ids: list[str] = Field(default_factory=list)
    open_gap_ids: list[str] = Field(default_factory=list)
    reader_cognition_state: CognitionState | None = None
    character_cognition_states: dict[str, CognitionState] = Field(default_factory=dict)
    objective_state_summary: str = ""
    source_delta_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExtractedWorldChangeSet(BaseModel):
    project_id: str
    chapter_number: int
    world_deltas: list[WorldDelta] = Field(default_factory=list)
    belief_updates: list[Belief] = Field(default_factory=list)
    knowledge_gap_updates: list[KnowledgeGap] = Field(default_factory=list)
    reveal_events: list[RevealEvent] = Field(default_factory=list)
    knowledge_update_events: list[KnowledgeUpdateEvent] = Field(default_factory=list)
    reader_experience_deltas: list[ReaderExperienceDelta] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApprovedWorldChangeSet(ExtractedWorldChangeSet):
    approved_by: list[str] = Field(default_factory=list)
    review_verdict_id: str = ""
    forced_accept_reason: str = ""

    @classmethod
    def from_extracted(
        cls,
        extracted: ExtractedWorldChangeSet,
        *,
        approved_by: list[str] | None = None,
        review_verdict_id: str = "",
        forced_accept_reason: str = "",
    ) -> "ApprovedWorldChangeSet":
        data = extracted.model_dump()
        data.update(
            {
                "approved_by": approved_by or [],
                "review_verdict_id": review_verdict_id,
                "forced_accept_reason": forced_accept_reason,
            }
        )
        return cls(**data)


class WorldCompileRequest(BaseModel):
    project_id: str
    chapter_number: int
    approved_changes: ApprovedWorldChangeSet
    review_verdict_id: str = ""
    compiler_run_id: str = ""
    forced_accept_reason: str = ""


class WorldCompileResult(BaseModel):
    project_id: str
    chapter_number: int
    compiler_run_id: str
    committed: bool
    world_delta_ids: list[str] = Field(default_factory=list)
    belief_ids: list[str] = Field(default_factory=list)
    knowledge_gap_ids: list[str] = Field(default_factory=list)
    reveal_event_ids: list[str] = Field(default_factory=list)
    knowledge_update_event_ids: list[str] = Field(default_factory=list)
    reader_experience_delta_ids: list[str] = Field(default_factory=list)
    snapshot_id: str = ""
    derived_canon_event_ids: list[str] = Field(default_factory=list)
    derived_entity_state_ids: list[str] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
