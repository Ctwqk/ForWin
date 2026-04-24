from __future__ import annotations
from typing import Literal

from pydantic import BaseModel, Field

from .experience import (
    ArcPayoffMap,
    BandDelightSchedule,
    ChapterExperiencePlan,
    ReaderPromise,
)
from forwin.governance import NarrativeConstraintInfo, NextBandSummary, PlanTaskItem
from .subworld import ChapterEntryTarget, SubWorldSummary
from .world_model import WorldContextPack


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


class SignalSummaryView(BaseModel):
    signal_key: str = ""
    signal_type: str = ""
    target_name: str = ""
    level: str = "noise"
    hit_count: int = 0
    max_severity: int = 0


class ReaderFeedbackView(BaseModel):
    comment_count: int = 0
    dominant_sentiment: str = "neutral"
    feedback_summary: str = ""
    recent_highlights: list[ReaderCommentView] = Field(default_factory=list)
    highlighted_topics: list[str] = Field(default_factory=list)
    confirmed_signals: list[SignalSummaryView] = Field(default_factory=list)
    reader_tier: int = 0


class AudienceHintView(BaseModel):
    pacing_hints: list[str] = Field(default_factory=list)
    clarity_hints: list[str] = Field(default_factory=list)
    character_heat_changes: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)


class AudienceTrendView(BaseModel):
    signal_key: str = ""
    signal_type: str = ""
    target_name: str = ""
    window_type: str = "long"
    current_level: str = "noise"
    previous_score: float = 0.0
    current_score: float = 0.0
    delta: float = 0.0
    scale_confidence: float = 0.0
    estimation_method: str = ""
    trend_type: Literal["rising", "falling", "stable"] = "stable"


class CanonEventEvidence(BaseModel):
    event_id: str = ""
    chapter_number: int = 0
    summary: str = ""
    significance: str = ""
    involved_entity_names: list[str] = Field(default_factory=list)
    evidence_id: str = ""


class ReviewNote(BaseModel):
    chapter_number: int = 0
    verdict: str = ""
    summary: str = ""
    issue_types: list[str] = Field(default_factory=list)
    planned_reward_tags: list[str] = Field(default_factory=list)
    delivered_reward_tags: list[str] = Field(default_factory=list)
    review_notes: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class LintSignal(BaseModel):
    tool: str
    code: str = ""
    severity: Literal["error", "warning", "info"] = "warning"
    message: str
    line: int = 0
    column: int = 0
    evidence_refs: list[str] = Field(default_factory=list)


class ChapterContextPack(BaseModel):
    """Everything a Writer needs to write one chapter."""
    project_id: str = ""
    project_title: str
    premise: str
    genre: str
    setting_summary: str
    genesis_context_refs: dict[str, str] = Field(default_factory=dict)
    genesis_world_overview: str = ""
    genesis_map_overview: str = ""
    genesis_story_engine_summary: str = ""

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
    audience_hints: AudienceHintView | None = None
    reader_promise: ReaderPromise | None = None
    arc_payoff_map: ArcPayoffMap | None = None
    band_delight_schedule: BandDelightSchedule | None = None
    band_task_contract: list[PlanTaskItem] = Field(default_factory=list)
    chapter_experience_plan: ChapterExperiencePlan | None = None
    active_subworlds: list[SubWorldSummary] = Field(default_factory=list)
    allowed_entities: list[str] = Field(default_factory=list)
    chapter_entry_targets: list[ChapterEntryTarget] = Field(default_factory=list)
    entity_admission_rule: str = ""
    chapter_task_contract: list[PlanTaskItem] = Field(default_factory=list)
    active_future_constraints: list[NarrativeConstraintInfo] = Field(default_factory=list)
    next_band_summary: NextBandSummary | None = None
    world_context: WorldContextPack = Field(default_factory=WorldContextPack)


class ReviewContextPack(BaseModel):
    project_id: str = ""
    project_title: str
    chapter_number: int
    chapter_plan_title: str
    chapter_plan_one_line: str
    chapter_goals: list[str] = Field(default_factory=list)
    previous_chapter_summaries: list[str] = Field(default_factory=list)
    genesis_context_refs: dict[str, str] = Field(default_factory=dict)
    genesis_world_overview: str = ""
    genesis_map_overview: str = ""
    genesis_story_engine_summary: str = ""
    active_entities: list[EntitySnapshot] = Field(default_factory=list)
    active_rules: list[EntitySnapshot] = Field(default_factory=list)
    active_threads: list[PlotThreadSnapshot] = Field(default_factory=list)
    timeline: TimelineSnapshot | None = None
    world_pressure: WorldPressureView | None = None
    reader_feedback: ReaderFeedbackView | None = None
    audience_hints: AudienceHintView | None = None
    reader_promise: ReaderPromise | None = None
    arc_payoff_map: ArcPayoffMap | None = None
    band_delight_schedule: BandDelightSchedule | None = None
    band_task_contract: list[PlanTaskItem] = Field(default_factory=list)
    chapter_experience_plan: ChapterExperiencePlan | None = None
    chapter_task_contract: list[PlanTaskItem] = Field(default_factory=list)
    active_future_constraints: list[NarrativeConstraintInfo] = Field(default_factory=list)
    next_band_summary: NextBandSummary | None = None
    world_context: WorldContextPack = Field(default_factory=WorldContextPack)
    recent_canon_events: list[CanonEventEvidence] = Field(default_factory=list)
    recent_rule_events: list[CanonEventEvidence] = Field(default_factory=list)
    recent_review_notes: list[ReviewNote] = Field(default_factory=list)
    lint_signals: list[LintSignal] = Field(default_factory=list)
