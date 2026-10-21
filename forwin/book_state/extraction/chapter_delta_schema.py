from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from forwin.protocol.book_state import WORLD_EDGE_TYPES


EntityKind = Literal["character", "faction", "organization", "group", "item", "location", "other"]


class _ExtractionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Duration(_ExtractionModel):
    amount: float
    unit: str


class ChapterTime(_ExtractionModel):
    elapsed: Duration | None = None
    evidence: str


class NewEntity(_ExtractionModel):
    name: str
    kind: EntityKind
    aliases: list[str] = Field(default_factory=list)


class EntityChange(_ExtractionModel):
    ref: str = ""
    new_entity: NewEntity | None = None
    field: str
    new_value: Any
    evidence: str
    flashback: bool = False


class RelationChange(_ExtractionModel):
    source_ref: str
    target_ref: str
    edge_type: str
    op: Literal["create", "end", "set"]
    field: str = ""
    new_value: Any = None
    appellation: str = ""
    evidence: str

    @field_validator("edge_type")
    @classmethod
    def _valid_edge_type(cls, value: str) -> str:
        if value not in WORLD_EDGE_TYPES:
            raise ValueError(f"unknown world edge type: {value}")
        return value


class NewFact(_ExtractionModel):
    proposition: str
    truth_value: bool
    sensitivity: Literal["public", "hidden", "secret"]


class KnowledgeEventItem(_ExtractionModel):
    observer_ref: str
    fact_ref: str = ""
    new_fact: NewFact | None = None
    state: str
    channel: str
    source_ref: str = ""
    touches_secret_ref: str = ""
    contradicts_ref: str = ""
    evidence: str
    flashback: bool = False

    @field_validator("state")
    @classmethod
    def _valid_state(cls, value: str) -> str:
        from forwin.book_state.knowledge import KNOWLEDGE_STATES

        if value not in KNOWLEDGE_STATES:
            raise ValueError(f"unknown knowledge state: {value}")
        return value

    @field_validator("channel")
    @classmethod
    def _valid_channel(cls, value: str) -> str:
        from forwin.book_state.knowledge import KNOWLEDGE_CHANNELS

        if value not in KNOWLEDGE_CHANNELS:
            raise ValueError(f"unknown knowledge channel: {value}")
        return value


class Movement(_ExtractionModel):
    entity_ref: str
    from_ref: str
    to_ref: str
    depart_offset: Duration | None = None
    arrive_offset: Duration | None = None
    travel_mode_id: str = ""
    evidence: str


class NewSite(_ExtractionModel):
    name: str
    near_ref: str = ""
    region_ref: str = ""
    kind: str = "site"
    evidence: str


class ItemTransfer(_ExtractionModel):
    item_ref: str = ""
    new_item: NewEntity | None = None
    from_ref: str
    to_ref: str
    evidence: str


class EventItem(_ExtractionModel):
    summary: str
    participant_refs: list[str] = Field(default_factory=list)
    location_ref: str = ""
    significance: Literal["minor", "moderate", "major"]
    visibility: Literal["public", "secret"]
    evidence: str
    flashback: bool = False


class ThreadBeat(_ExtractionModel):
    thread_ref: str = ""
    new_thread_title: str = ""
    beat_type: Literal["setup", "advance", "complication", "resolution"]
    evidence: str


class IntentInteraction(_ExtractionModel):
    intent_ref: str
    effect: Literal["disrupted", "advanced", "fulfilled"]
    evidence: str


class PlanFulfillment(_ExtractionModel):
    target_id: str
    status: Literal["done", "partial", "missing"]
    evidence: str = ""


class EntityMention(_ExtractionModel):
    name: str
    kind: str
    is_named: bool = True
    is_on_stage: bool = True
    evidence: str


class ChapterDeltaExtraction(_ExtractionModel):
    schema_version: Literal["v1"] = "v1"
    body_sha256: str
    time: ChapterTime
    entity_changes: list[EntityChange] = Field(default_factory=list)
    relation_changes: list[RelationChange] = Field(default_factory=list)
    knowledge_events: list[KnowledgeEventItem] = Field(default_factory=list)
    movements: list[Movement] = Field(default_factory=list)
    new_sites: list[NewSite] = Field(default_factory=list)
    item_transfers: list[ItemTransfer] = Field(default_factory=list)
    events: list[EventItem] = Field(default_factory=list)
    thread_beats: list[ThreadBeat] = Field(default_factory=list)
    intent_interactions: list[IntentInteraction] = Field(default_factory=list)
    plan_fulfillment: list[PlanFulfillment] = Field(default_factory=list)
    entity_mentions: list[EntityMention] = Field(default_factory=list)


class ExtractionIssue(_ExtractionModel):
    severity: Literal["error", "warning"]
    code: str
    path: str
    message: str
