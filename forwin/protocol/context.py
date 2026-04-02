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


class MemorySnippet(BaseModel):
    """Retrieved memory snippet selected for the current chapter."""
    chapter_number: int
    title: str
    summary: str = ""
    excerpt: str = ""
    score: float = 0.0


class NPCIntentView(BaseModel):
    entity_name: str
    intent_kind: str
    objective: str
    tactic: str = ""
    urgency: int = 1
    notes: str = ""


class WorldPressureView(BaseModel):
    pressure_level: str
    pressure_summary: str
    notable_shifts: list[str] = Field(default_factory=list)


class ArcEnvelopeView(BaseModel):
    source_policy_tier: str = ""
    base_target_size: int = 0
    base_soft_min: int = 0
    base_soft_max: int = 0
    resolved_target_size: int = 0
    resolved_soft_min: int = 0
    resolved_soft_max: int = 0
    detailed_band_size: int = 0
    frozen_zone_size: int = 0
    current_projected_size: int = 0
    current_confidence: float = 0.0


class ReaderCommentView(BaseModel):
    platform_id: str = ""
    author_name: str = ""
    body_text: str
    chapter_title: str = ""
    remote_created_at: str = ""


class ReaderFeedbackView(BaseModel):
    comment_count: int = 0
    dominant_sentiment: str = "neutral"
    feedback_summary: str = ""
    recent_highlights: list[ReaderCommentView] = Field(default_factory=list)
    highlighted_topics: list[str] = Field(default_factory=list)


class ChapterContextPack(BaseModel):
    """Everything a Writer needs to write one chapter."""
    project_id: str = ""
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
    retrieved_memories: list[MemorySnippet] = Field(default_factory=list)
    npc_intents: list[NPCIntentView] = Field(default_factory=list)
    world_pressure: WorldPressureView | None = None
    reader_feedback: ReaderFeedbackView | None = None
    current_arc_envelope: ArcEnvelopeView | None = None
