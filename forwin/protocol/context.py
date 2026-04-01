from __future__ import annotations
from pydantic import BaseModel, Field


class EntitySnapshot(BaseModel):
    """Snapshot of an entity's current state for context."""
    entity_id: str
    kind: str
    name: str
    importance: int = 0
    aliases: list[str] = Field(default_factory=list)
    description: str
    current_state: dict       # Deserialized state_json


class RelationSnapshot(BaseModel):
    """Snapshot of a relationship for context."""
    source_name: str
    target_name: str
    relation_type: str
    description: str


class PlotThreadSnapshot(BaseModel):
    """Snapshot of a plot thread for context."""
    thread_id: str
    name: str
    description: str
    status: str
    priority: int
    recent_beats: list[str] = Field(default_factory=list)  # Last 2-3 beat descriptions


class TimelineSnapshot(BaseModel):
    """Current story time."""
    current_time_label: str
    ordinal: int


class ChapterContextPack(BaseModel):
    """Everything a Writer needs to write one chapter."""
    project_title: str
    premise: str
    genre: str
    setting_summary: str

    # Current chapter info
    chapter_number: int
    chapter_plan_title: str
    chapter_plan_one_line: str
    chapter_goals: list[str]

    # History
    previous_chapter_summaries: list[str] = Field(default_factory=list)  # Last 1-3 chapters

    # World state
    active_entities: list[EntitySnapshot] = Field(default_factory=list)
    active_relations: list[RelationSnapshot] = Field(default_factory=list)
    active_threads: list[PlotThreadSnapshot] = Field(default_factory=list)
    timeline: TimelineSnapshot | None = None
