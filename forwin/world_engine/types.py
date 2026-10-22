"""Contracts of the world tick: effects, LLM output schemas, tick candidates and news."""

from __future__ import annotations

from collections.abc import Callable, Collection
from hashlib import sha256
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from forwin.book_state.extraction.chapter_delta_schema import Duration
from forwin.book_state.knowledge import FACT_REF_PREFIX, KNOWLEDGE_CHANNELS, KNOWLEDGE_STATES
from forwin.book_state.schema import AGENDA_STATUSES, IMPACT_LEVELS, INTENT_ACTION_TYPES, INTENT_STATUSES
from forwin.protocol.book_state import WORLD_EDGE_TYPES


# The source_type of the tick's GraphDelta.
WORLD_ENGINE_SOURCE_TYPE = "world_engine"
# A knowledge effect's ref to a fact the same effects create: ``fact:new:<key>``.
NEW_FACT_REF_PREFIX = f"{FACT_REF_PREFIX}new:"
# The agenda_ref of an agenda change that creates an agenda.
NEW_AGENDA_REF = "new"
_SEED_MODULUS = 2_000_000_000


def _one_of(label: str, allowed: Collection[str]) -> Callable[[str], str]:
    def check(value: str) -> str:
        if value not in allowed:
            raise ValueError(f"unknown {label}: {value!r}")
        return value

    return check


def _fact_ref(value: str) -> str:
    if not value.startswith(FACT_REF_PREFIX):
        raise ValueError(f"fact ref must start with {FACT_REF_PREFIX!r}: {value!r}")
    return value


ActionType = Annotated[str, AfterValidator(_one_of("action type", INTENT_ACTION_TYPES))]
ImpactLevel = Annotated[str, AfterValidator(_one_of("impact level", IMPACT_LEVELS))]
IntentStatus = Annotated[str, AfterValidator(_one_of("intent status", INTENT_STATUSES))]
KnowledgeState = Annotated[str, AfterValidator(_one_of("knowledge state", KNOWLEDGE_STATES))]
KnowledgeChannel = Annotated[str, AfterValidator(_one_of("knowledge channel", KNOWLEDGE_CHANNELS))]
WorldEdgeType = Annotated[str, AfterValidator(_one_of("world edge type", WORLD_EDGE_TYPES))]
FactRef = Annotated[str, AfterValidator(_fact_ref)]


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# effects


class EffectEntityChange(_ContractModel):
    ref: str
    field: str
    new_value: Any


class EffectRelationChange(_ContractModel):
    source_ref: str
    target_ref: str
    edge_type: WorldEdgeType
    change: Literal["create", "end", "set"]
    field: str = ""
    new_value: Any = None


class EffectMovement(_ContractModel):
    entity_ref: str
    to_location_id: str
    travel_mode_id: str = ""
    depart_at: int
    arrive_at: int

    @model_validator(mode="after")
    def _arrives_after_departing(self) -> EffectMovement:
        if self.arrive_at < self.depart_at:
            raise ValueError(f"arrive_at {self.arrive_at} precedes depart_at {self.depart_at}")
        return self


class EffectItemTransfer(_ContractModel):
    item_ref: str
    from_ref: str
    to_ref: str


class EffectEvent(_ContractModel):
    summary: str
    participant_refs: list[str] = Field(default_factory=list)
    location_id: str
    visibility: Literal["public", "secret"]
    at: int


class EffectFact(_ContractModel):
    key: str
    proposition: str
    truth_value: bool
    sensitivity: Literal["public", "hidden", "secret"]
    known_by: list[str] = Field(default_factory=list)


class EffectKnowledge(_ContractModel):
    observer_ref: str
    # ``fact:<id>``, or ``fact:new:<key>`` for a fact in the same effects' new_facts.
    fact_ref: FactRef
    state: KnowledgeState
    channel: KnowledgeChannel
    source_ref: str = ""


class WorldEffects(_ContractModel):
    entity_changes: list[EffectEntityChange] = Field(default_factory=list)
    relation_changes: list[EffectRelationChange] = Field(default_factory=list)
    movements: list[EffectMovement] = Field(default_factory=list)
    item_transfers: list[EffectItemTransfer] = Field(default_factory=list)
    events: list[EffectEvent] = Field(default_factory=list)
    new_facts: list[EffectFact] = Field(default_factory=list)
    knowledge: list[EffectKnowledge] = Field(default_factory=list)

    @model_validator(mode="after")
    def _new_fact_refs_resolve(self) -> WorldEffects:
        keys = [fact.key for fact in self.new_facts]
        if len(set(keys)) != len(keys):
            raise ValueError(f"new_facts repeat a key: {keys}")
        unresolved = [
            item.fact_ref for item in self.knowledge
            if item.fact_ref.startswith(NEW_FACT_REF_PREFIX)
            and item.fact_ref.removeprefix(NEW_FACT_REF_PREFIX) not in keys
        ]
        if unresolved:
            raise ValueError(f"knowledge names facts missing from new_facts: {unresolved}")
        return self


# LLM outputs


class AdjudicatedOutcome(_ContractModel):
    intent_id: str
    result: Literal["resolved", "disrupted"]
    summary: str
    effects: WorldEffects


class AdjudicationResult(_ContractModel):
    outcomes: list[AdjudicatedOutcome] = Field(default_factory=list)


class ProposedIntent(_ContractModel):
    agenda_ref: str
    action_type: ActionType
    summary: str
    target_refs: list[str] = Field(default_factory=list)
    location_id: str
    scheduled_in: Duration
    requires_onstage: bool
    impact_level: ImpactLevel
    expected_effects: WorldEffects


class AgendaChange(_ContractModel):
    # NEW_AGENDA_REF creates an agenda, which then needs a goal; empty fields leave a field unchanged.
    agenda_ref: str
    status: str = ""
    priority: int | None = None
    goal: str = ""
    motivation: str = ""

    @model_validator(mode="after")
    def _valid_change(self) -> AgendaChange:
        if self.agenda_ref == NEW_AGENDA_REF and not self.goal.strip():
            raise ValueError("a new agenda needs a goal")
        if self.status and self.status not in AGENDA_STATUSES:
            raise ValueError(f"unknown agenda status: {self.status!r}")
        return self


class ActorTurnResult(_ContractModel):
    actor_id: str
    new_intents: list[ProposedIntent] = Field(default_factory=list)
    agenda_changes: list[AgendaChange] = Field(default_factory=list)
    next_turn_in: Duration


# tick internals


class TickContext(_ContractModel):
    project_id: str
    chapter_number: int
    now: int
    window_start: int
    seed: int


class TickCandidate(_ContractModel):
    candidate_id: str
    origin: Literal[
        "clock", "intent_interaction", "adjudication", "onstage", "propagation", "actor_turn",
        "conflict_resolution",
    ]
    actor_id: str = ""
    intent_id: str = ""
    summary: str
    effects: WorldEffects
    new_intents: list[ProposedIntent] = Field(default_factory=list)
    agenda_changes: list[AgendaChange] = Field(default_factory=list)
    intent_status_changes: dict[str, IntentStatus] = Field(default_factory=dict)
    slip_updates: dict[str, int] = Field(default_factory=dict)
    next_turn_at: dict[str, int] = Field(default_factory=dict)
    impact_level: ImpactLevel = "minor"


class NewsItem(_ContractModel):
    fact_ref: FactRef
    observer_ref: str
    arrive_at: int


def tick_seed(project_id: str, chapter_number: int) -> int:
    """The tick's random seed: stable for one chapter of one project."""
    digest = sha256(f"{project_id}|{chapter_number}".encode()).hexdigest()
    return int(digest[:12], 16) % _SEED_MODULUS
