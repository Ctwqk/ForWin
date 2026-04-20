from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, BaseModel, Field

from .subworld import ChapterEntryTarget


RewardTag = Literal["power", "social", "justice", "mystery", "emotion"]
AmbiguityMode = Literal["stable", "managed", "high"]
AmbiguityPayoffType = Literal["confirmation", "reversal", "constraint"]
ProgressChannel = Literal["event", "state", "thread", "time", "status", "relationship", "rule"]


class ReaderPromise(BaseModel):
    genre_promise: str = ""
    pleasure_promise: str = ""
    core_pleasures: list[str] = Field(default_factory=list)
    acceptable_drag_level: str = ""
    acceptable_exposition_density: str = ""
    cliffhanger_aggressiveness: str = ""
    ambiguity_mode: AmbiguityMode = "stable"
    world_legibility_target: str = ""


class MacroPayoff(BaseModel):
    payoff_id: str
    category: RewardTag
    template_id: str = ""
    target_chapter_hint: str = ""
    setup_requirement: str = ""
    success_signal: str = ""


class RevelationLayer(BaseModel):
    layer_id: str = ""
    layer_type: str = ""
    summary: str = Field(
        default="",
        validation_alias=AliasChoices("summary", "description"),
    )
    chapter_window: str = Field(
        default="",
        validation_alias=AliasChoices("chapter_window", "reveal_window"),
    )


class ArcPayoffMap(BaseModel):
    macro_payoffs: list[MacroPayoff] = Field(default_factory=list)
    awe_kit: list[str] = Field(default_factory=list)
    revelation_layers: list[RevelationLayer] = Field(default_factory=list)
    ambiguity_constraints: list[str] = Field(default_factory=list)


class BandRewardItem(BaseModel):
    chapter_hint: int = 0
    category: RewardTag
    template_id: str = ""
    intent: str = ""


class CuriosityBeat(BaseModel):
    chapter_hint: int = 0
    question_open: str = Field(
        default="",
        validation_alias=AliasChoices("question_open", "opened_question"),
    )
    question_resolve: str = Field(
        default="",
        validation_alias=AliasChoices("question_resolve", "resolved_question"),
    )
    escalated_question: str = ""


class AmbiguityPayoff(BaseModel):
    chapter_hint: int = 0
    payoff_type: AmbiguityPayoffType = "confirmation"
    summary: str = ""
    constraint_ref: str = Field(
        default="",
        validation_alias=AliasChoices("constraint_ref", "constraint"),
    )


class BandDelightSchedule(BaseModel):
    band_id: str
    chapter_start: int
    chapter_end: int
    scheduled_rewards: list[BandRewardItem] = Field(default_factory=list)
    immersion_anchor_scene_goal: str = ""
    stall_guard_max_gap: int = 0
    curiosity_beats: list[CuriosityBeat] = Field(default_factory=list)
    ambiguity_payoffs: list[AmbiguityPayoff] = Field(default_factory=list)
    active_subworld_ids: list[str] = Field(default_factory=list)
    chapter_entry_targets: list[ChapterEntryTarget] = Field(default_factory=list)


class ChapterExperiencePlan(BaseModel):
    planned_reward_tags: list[RewardTag] = Field(default_factory=list)
    selected_template_ids: list[str] = Field(default_factory=list)
    hook_type: str = ""
    question_hook: str = ""
    question_resolution: str = ""
    immersion_anchors: list[str] = Field(default_factory=list)
    progress_markers: list[str] = Field(default_factory=list)
    rule_anchors: list[str] = Field(default_factory=list)
    relationship_or_status_shift: str = ""
    minimum_progress_channels: list[ProgressChannel] = Field(default_factory=list)
    active_subworld_ids: list[str] = Field(default_factory=list)
    chapter_entry_targets: list[ChapterEntryTarget] = Field(default_factory=list)
    entity_admission_rule: str = ""
