from __future__ import annotations
from pydantic import BaseModel, Field
from .scene import SceneContinuation, SceneOutput
from .state_change import StateChangeCandidate, EventCandidate, ThreadBeatCandidate, TimeAdvance
from .subworld import EntityMention
from .world_v4 import (
    Belief,
    KnowledgeGap,
    ReaderExperienceDelta,
    RevealEvent,
    WorldDelta,
)


class LoreCandidate(BaseModel):
    subject_name: str = ""
    subject_type: str = ""
    description: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: float = 0.5


class TimelineHint(BaseModel):
    current_time_label: str = ""
    projected_time_label: str = ""
    duration_hint: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: float = 0.5


class WriterNote(BaseModel):
    note_type: str = ""
    target_name: str = ""
    note: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class WriterOutput(BaseModel):
    """Structured output from the Chapter Writer."""
    project_id: str = ""
    chapter_number: int
    title: str
    body: str                                         # The actual Chinese chapter text
    char_count: int = 0                               # len(body), computed after parse
    end_of_chapter_summary: str                       # 1-2 sentence recap in Chinese
    draft_blob_path: str = ""
    scene_outputs: list[SceneOutput] = Field(default_factory=list)
    state_changes: list[StateChangeCandidate] = Field(default_factory=list)
    new_events: list[EventCandidate] = Field(default_factory=list)
    thread_beats: list[ThreadBeatCandidate] = Field(default_factory=list)
    time_advance: TimeAdvance | None = None
    scene_continuation: list[SceneContinuation] = Field(default_factory=list)
    lore_candidates: list[LoreCandidate] = Field(default_factory=list)
    timeline_hints: list[TimelineHint] = Field(default_factory=list)
    writer_notes: list[WriterNote] = Field(default_factory=list)
    entity_mentions: list[EntityMention] = Field(default_factory=list)
    generation_meta: dict[str, object] = Field(default_factory=dict)
    world_deltas: list[WorldDelta] = Field(default_factory=list)
    belief_updates: list[Belief] = Field(default_factory=list)
    knowledge_gap_updates: list[KnowledgeGap] = Field(default_factory=list)
    reveal_events: list[RevealEvent] = Field(default_factory=list)
    reader_experience_deltas: list[ReaderExperienceDelta] = Field(default_factory=list)
    observer_visibility_updates: dict[str, str] = Field(default_factory=dict)
    must_preserve_facts: list[str] = Field(default_factory=list)
    must_not_reveal_violations: list[str] = Field(default_factory=list)
