from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field
from pydantic.json_schema import SkipJsonSchema

from forwin.planning.checkpoints import NextBandSummary
from forwin.planning.constraints import NarrativeConstraintInfo
from forwin.planning.contracts import PlanTaskItem
from forwin.planning.world_contracts import ChapterWorldDeltaIntent, RevealLadderStep

from .experience import (
    ArcPayoffMap,
    BandDelightSchedule,
    ChapterExperiencePlan,
    ReaderPromise,
)
from .scene import ScenePlan
from .subworld import ChapterEntryTarget, SubWorldSummary
from .world_model import WorldContextPack


class CognitionSource(BaseModel):
    """An accepted change event, not an inferred knowledge-acquisition date."""

    delta_id: str
    chapter_number: int
    field_path: str
    op: str
    evidence_refs: list[str] = Field(default_factory=list)


class AcceptedCognitionSnapshot(BaseModel):
    """Sparse observer beliefs at the shared accepted read baseline.

    Knowing a node never grants knowledge of all its objective fields.
    Missing refs and absent evidence timing remain unknown.
    """

    observer_type: str
    observer_id: str
    as_of_chapter: int
    ref_states: dict[str, Literal["confirmed", "suspected", "known", "hidden", "unknown"]] = Field(default_factory=dict)
    field_overrides: dict[str, Any] = Field(default_factory=dict)
    false_nodes: dict[str, Any] = Field(default_factory=dict)
    false_edges: dict[str, Any] = Field(default_factory=dict)
    false_facts: dict[str, Any] = Field(default_factory=dict)
    evidence_by_ref: dict[str, list[str]] = Field(default_factory=dict)
    sources_by_ref: dict[str, list[CognitionSource]] = Field(default_factory=dict)


COGNITION_CONTINUATION_RULE = (
    "缺记录为 unknown；可见性默认值、知道对象存在均不代表知道其所有字段。"
    "作者秘密、阶段目标和预期变化不是已接纳认知。"
    "仅在本章已写正文明确发生获知事件之后，相关角色才可条件性承接新知识；"
    "前场草稿仍待审，不能把计划、角色说法或其他角色获知当作人人知情。"
    "as_of_chapter 是读取基线；sources_by_ref 是来源变化事件，缺来源时点不得推定获知时间。"
)


class EntitySnapshot(BaseModel):
    """Snapshot of an entity's current state for context."""

    entity_id: str
    kind: str
    name: str
    importance: int = 0
    aliases: list[str] = Field(default_factory=list)
    description: str
    status: str = ""
    is_active: bool = True
    current_state: dict  # Deserialized state_json


class RelationSnapshot(BaseModel):
    """Snapshot of a relationship for context."""

    relation_id: str = ""
    source_id: str = ""
    target_id: str = ""
    current_state: dict[str, Any] = Field(default_factory=dict)
    status: str = ""
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

    project_id: str = ""
    canon_commit_id: str = ""
    candidate_id: str = ""
    draft_id: str = ""
    body_hash: str = ""
    embedding_input_hash: str = ""
    embedding_identity: str = ""
    chapter_number: int
    title: str
    summary: str = ""
    excerpt: str = ""
    score: float = 0.0


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


class AudienceHintItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    category: Literal["pacing", "clarity", "character_heat", "risk", "prediction"]
    text: str = Field(min_length=1, max_length=500)


class AudienceHintView(BaseModel):
    """Canonical action items; category lists are derived read views only."""

    items: list[AudienceHintItem] = Field(default_factory=list)

    def clipped(self, per_category: int = 3) -> AudienceHintView:
        counts: dict[str, int] = {}
        kept: list[AudienceHintItem] = []
        seen: set[str] = set()
        for item in self.items:
            if item.action_id in seen or counts.get(item.category, 0) >= per_category:
                continue
            seen.add(item.action_id)
            counts[item.category] = counts.get(item.category, 0) + 1
            kept.append(item)
        return AudienceHintView(items=kept)

    def _texts(self, category: str) -> list[str]:
        return [item.text for item in self.items if item.category == category]

    @computed_field
    @property
    def pacing_hints(self) -> list[str]:
        return self._texts("pacing")

    @computed_field
    @property
    def clarity_hints(self) -> list[str]:
        return self._texts("clarity")

    @computed_field
    @property
    def character_heat_changes(self) -> list[str]:
        return self._texts("character_heat")

    @computed_field
    @property
    def risk_flags(self) -> list[str]:
        return self._texts("risk")

    @computed_field
    @property
    def prediction_hints(self) -> list[str]:
        return self._texts("prediction")


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


class GenesisReferenceFact(BaseModel):
    """Verbatim writing-time source evidence, not a Canon or knowledge update."""

    source_path: str
    category: Literal["world_history", "world_rule", "character_secret"]
    subject: str = ""
    text: str


class RepairContract(BaseModel):
    """Current rewrite requirements shared by Writer and repair verification."""

    must_fix: list[str] = Field(default_factory=list)
    must_preserve: list[str] = Field(default_factory=list)
    must_not_reveal: list[str] = Field(default_factory=list)


class ChapterContextPack(BaseModel):
    """Everything a Writer needs to write one chapter."""

    # Retain the immutable in-process read fence for repair rehydration.
    canon_read_baseline: Any | None = Field(default=None, exclude=True)
    required_context_hydrator: SkipJsonSchema[Callable[["ChapterContextPack", list[ScenePlan]], "ChapterContextPack"] | None] = Field(default=None, exclude=True)
    # Complete accepted name candidates, preserved through trimming/transport.
    # Snapshot identity only: this never grants authority to read new state.
    entity_name_candidates: dict[str, list[str]] = Field(default_factory=dict)
    required_entity_ids: list[str] = Field(default_factory=list)
    required_relation_ids: list[str] = Field(default_factory=list)
    required_selection_sources: dict[str, list[str]] = Field(default_factory=dict)
    required_facts: dict[str, Any] = Field(default_factory=dict)
    context_budget_summary: dict[str, Any] = Field(default_factory=dict)

    project_id: str = ""
    project_title: str
    premise: str
    genre: str
    setting_summary: str
    project_target_total_chapters: int = 0
    genesis_context_refs: dict[str, str] = Field(default_factory=dict)
    genesis_world_overview: str = ""
    genesis_map_overview: str = ""
    genesis_story_engine_summary: str = ""
    genesis_reference_facts: list[GenesisReferenceFact] = Field(default_factory=list)
    genesis_reference_omitted_count: int = 0

    # Current chapter info
    chapter_number: int
    chapter_plan_title: str
    chapter_plan_one_line: str
    chapter_goals: list[str]
    repair_contract: RepairContract | None = None

    # History
    previous_chapter_summaries: list[str] = Field(
        default_factory=list
    )  # Last 1-3 chapters

    # World state
    active_entities: list[EntitySnapshot] = Field(default_factory=list)
    active_relations: list[RelationSnapshot] = Field(default_factory=list)
    active_threads: list[PlotThreadSnapshot] = Field(default_factory=list)
    timeline: TimelineSnapshot | None = None
    retrieved_memories: list[MemorySnippet] = Field(default_factory=list)
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
    active_future_constraints: list[NarrativeConstraintInfo] = Field(
        default_factory=list
    )
    next_band_summary: NextBandSummary | None = None
    world_context: WorldContextPack = Field(default_factory=WorldContextPack)
    knowledge_system_context: dict[str, Any] = Field(default_factory=dict)
    map_context: dict[str, Any] = Field(default_factory=dict)
    active_world_lines: list[str] = Field(default_factory=list)
    visible_world_lines: list[str] = Field(default_factory=list)
    hidden_world_lines: list[str] = Field(default_factory=list)
    recent_world_deltas: list[str] = Field(default_factory=list)
    recent_offscreen_deltas: list[str] = Field(default_factory=list)
    active_knowledge_gaps: list[str] = Field(default_factory=list)
    planned_reveal_ladder: list[RevealLadderStep] = Field(default_factory=list)
    reader_cognition_state: str = ""
    accepted_cognition: list[AcceptedCognitionSnapshot] = Field(default_factory=list)
    planned_reader_cognition_state: str = ""
    character_cognition_states: dict[str, Any] = Field(default_factory=dict)
    observer_visibility_states: dict[str, str] = Field(default_factory=dict)
    promise_debts: list[str] = Field(default_factory=list)
    recent_reader_experience_deltas: list[str] = Field(default_factory=list)
    must_not_reveal: list[str] = Field(default_factory=list)
    fair_misdirection_requirements: list[str] = Field(default_factory=list)
    chapter_world_delta_intent: ChapterWorldDeltaIntent | None = None
    active_personality_contexts: list[dict[str, Any]] = Field(default_factory=list)
    personality_integrity_issues: list[dict[str, Any]] = Field(default_factory=list)
    canon_quality_context: dict[str, Any] = Field(default_factory=dict)
    active_narrative_obligations: list[dict[str, Any]] = Field(default_factory=list)
    future_plan_audit_summary: dict[str, Any] = Field(default_factory=dict)


class WorldModelRetrievalPack(BaseModel):
    """Role-scoped v4 world-model context.

    These packs are read-side views. They must not be treated as canon writes.
    """

    pack_kind: str
    project_id: str
    as_of_chapter: int = 0
    active_world_lines: list[str] = Field(default_factory=list)
    visible_world_lines: list[str] = Field(default_factory=list)
    hidden_world_lines: list[str] = Field(default_factory=list)
    recent_world_deltas: list[str] = Field(default_factory=list)
    recent_offscreen_deltas: list[str] = Field(default_factory=list)
    active_knowledge_gaps: list[str] = Field(default_factory=list)
    hidden_objective_truths: list[str] = Field(default_factory=list)
    planned_reveal_ladder: list[RevealLadderStep] = Field(default_factory=list)
    reader_cognition_state: dict[str, Any] = Field(default_factory=dict)
    accepted_cognition: list[AcceptedCognitionSnapshot] = Field(default_factory=list)
    planned_reader_cognition_state: str = ""
    character_cognition_states: dict[str, Any] = Field(default_factory=dict)
    observer_visibility_states: dict[str, str] = Field(default_factory=dict)
    promise_debts: list[str] = Field(default_factory=list)
    recent_reader_experience_deltas: list[str] = Field(default_factory=list)
    must_not_reveal: list[str] = Field(default_factory=list)
    fair_misdirection_requirements: list[str] = Field(default_factory=list)
    accepted_delta_ids: list[str] = Field(default_factory=list)
    rejected_delta_ids: list[str] = Field(default_factory=list)
    book_state_snapshot: dict[str, Any] = Field(default_factory=dict)
    book_state_nodes: list[dict[str, Any]] = Field(default_factory=list)
    book_state_edges: list[dict[str, Any]] = Field(default_factory=list)
    book_state_facts: list[dict[str, Any]] = Field(default_factory=list)
    book_state_map: dict[str, Any] = Field(default_factory=dict)
    obsidian_pages: list[dict[str, Any]] = Field(default_factory=list)
    llm_kb_context: dict[str, Any] = Field(default_factory=dict)
    review_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    active_personality_contexts: list[dict[str, Any]] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    source_digest: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class PlanningPack(WorldModelRetrievalPack):
    pack_kind: Literal["planning"] = "planning"


class WritingPack(WorldModelRetrievalPack):
    pack_kind: Literal["writing"] = "writing"


class ReviewPack(WorldModelRetrievalPack):
    pack_kind: Literal["review"] = "review"


class CompilerPack(WorldModelRetrievalPack):
    pack_kind: Literal["compiler"] = "compiler"


class ReaderExperiencePack(WorldModelRetrievalPack):
    pack_kind: Literal["reader_experience"] = "reader_experience"


class CognitionPack(WorldModelRetrievalPack):
    pack_kind: Literal["cognition"] = "cognition"


class RevealPack(WorldModelRetrievalPack):
    pack_kind: Literal["reveal"] = "reveal"


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
    genesis_reference_facts: list[GenesisReferenceFact] = Field(default_factory=list)
    genesis_reference_omitted_count: int = 0
    must_not_reveal: list[str] = Field(default_factory=list)
    planned_reveal_ladder: list[RevealLadderStep] = Field(default_factory=list)
    accepted_cognition: list[AcceptedCognitionSnapshot] = Field(default_factory=list)
    planned_reader_cognition_state: str = ""
    character_cognition_states: dict[str, Any] = Field(default_factory=dict)
    observer_visibility_states: dict[str, str] = Field(default_factory=dict)
    fair_misdirection_requirements: list[str] = Field(default_factory=list)
    chapter_world_delta_intent: ChapterWorldDeltaIntent | None = None
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
    active_future_constraints: list[NarrativeConstraintInfo] = Field(
        default_factory=list
    )
    next_band_summary: NextBandSummary | None = None
    world_context: WorldContextPack = Field(default_factory=WorldContextPack)
    map_context: dict[str, Any] = Field(default_factory=dict)
    recent_canon_events: list[CanonEventEvidence] = Field(default_factory=list)
    recent_rule_events: list[CanonEventEvidence] = Field(default_factory=list)
    recent_review_notes: list[ReviewNote] = Field(default_factory=list)
    lint_signals: list[LintSignal] = Field(default_factory=list)
    active_personality_contexts: list[dict[str, Any]] = Field(default_factory=list)
    deterministic_quality_report: dict[str, Any] = Field(default_factory=dict)
    canon_invariants: list[dict[str, Any]] = Field(default_factory=list)
    active_narrative_obligations: list[dict[str, Any]] = Field(default_factory=list)
    future_plan_audit_summary: dict[str, Any] = Field(default_factory=dict)
