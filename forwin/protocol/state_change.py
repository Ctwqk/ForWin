from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Literal

EntityKind = Literal["character", "location", "faction", "item", "rule"]


class StateChangeCandidate(BaseModel):
    """A proposed state change for an entity."""
    entity_name: str          # Name-based, resolved to ID by updater
    entity_kind: EntityKind
    field: str                # Which field changed
    old_value: str            # Previous value (may be empty for new fields)
    new_value: str            # New value
    reason: str               # Why this changed (in Chinese)


class EventCandidate(BaseModel):
    """A proposed canon event."""
    summary: str              # Event description in Chinese
    significance: Literal["major", "minor", "background"] = "minor"
    involved_entity_names: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)    # Parallel to involved_entity_names: protagonist/antagonist/witness/mentioned


class ThreadBeatCandidate(BaseModel):
    """A proposed plot thread beat."""
    thread_name: str          # Resolved to ID by updater
    beat_type: Literal["setup", "escalation", "twist", "climax", "resolution"] = "escalation"
    description: str          # What happened to this thread


class TimeAdvance(BaseModel):
    """Time progression information."""
    new_time_label: str       # e.g. "天元历1023年三月初五"
    duration_description: str # e.g. "三天后" or "半月之后"
