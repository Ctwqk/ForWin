from __future__ import annotations
from pydantic import BaseModel, Field, field_validator
from typing import Literal

from .experience import RewardTag

EntityKind = Literal["character", "location", "faction", "item", "rule"]


class StateChangeCandidate(BaseModel):
    """A proposed state change for an entity."""

    entity_name: str  # Name-based, resolved to ID by updater
    entity_kind: EntityKind
    field: str  # Which field changed
    old_value: str  # Previous value (may be empty for new fields)
    new_value: str  # New value
    reason: str  # Why this changed (in Chinese)


class EventCandidate(BaseModel):
    """A proposed canon event."""

    summary: str  # Event description in Chinese
    significance: Literal["major", "minor", "background"] = "minor"
    involved_entity_names: list[str] = Field(default_factory=list)
    roles: list[str] = Field(
        default_factory=list
    )  # Parallel to involved_entity_names: protagonist/antagonist/witness/mentioned


class DeliveredPayoffCandidate(BaseModel):
    """A realized protagonist benefit backed by a verbatim body quote."""

    entity_name: str = Field(min_length=1)
    category: RewardTag
    direction: Literal["gain", "relief", "reversal"]
    before_state: str = Field(min_length=1)
    after_state: str = Field(min_length=1)
    evidence_quote: str = Field(min_length=4, max_length=240)

    @field_validator(
        "entity_name",
        "before_state",
        "after_state",
        "evidence_quote",
        mode="before",
    )
    @classmethod
    def _strip_required_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ThreadBeatCandidate(BaseModel):
    """A proposed plot thread beat."""

    thread_name: str  # Resolved to ID by updater
    beat_type: Literal["setup", "escalation", "twist", "climax", "resolution"] = (
        "escalation"
    )
    description: str  # What happened to this thread


class TimeAdvance(BaseModel):
    """Time progression information."""

    new_time_label: str  # e.g. "天元历1023年三月初五"
    duration_description: str  # e.g. "三天后" or "半月之后"
