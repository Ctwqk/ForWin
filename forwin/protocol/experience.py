from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator

from .subworld import ChapterEntryTarget


RewardTag = Literal["power", "social", "justice", "mystery", "emotion"]
AmbiguityMode = Literal["stable", "managed", "high"]
AmbiguityPayoffType = Literal["confirmation", "reversal", "constraint"]
ProgressChannel = Literal["event", "state", "thread", "time", "status", "relationship", "rule"]


def _stringify_llm_item(value: object) -> str:
    if isinstance(value, dict):
        primary = (
            value.get("awe_type")
            or value.get("aspect")
            or value.get("name")
            or value.get("type")
            or value.get("summary")
            or value.get("description")
            or ""
        )
        detail = (
            value.get("summary")
            or value.get("rule")
            or value.get("constraint")
            or value.get("description")
            or value.get("note")
            or ""
        )
        if primary and detail and str(primary) != str(detail):
            return f"{primary}：{detail}"
        return str(primary or detail or value).strip()
    return str(value or "").strip()


def _stringify_window(value: object) -> str:
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
        if len(items) == 2:
            return f"{items[0]}-{items[1]}"
        return ", ".join(items)
    return _stringify_llm_item(value)


class ReaderPromise(BaseModel):
    genre_promise: str = ""
    pleasure_promise: str = ""
    core_pleasures: list[str] = Field(default_factory=list)
    acceptable_drag_level: str = ""
    acceptable_exposition_density: str = ""
    cliffhanger_aggressiveness: str = ""
    ambiguity_mode: AmbiguityMode = "stable"
    world_legibility_target: str = ""

    @field_validator("ambiguity_mode", mode="before")
    @classmethod
    def _normalize_ambiguity_mode(cls, value: object) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"stable", "managed", "high"}:
            return normalized
        if any(token in normalized for token in ("high", "opaque", "uncertain", "mystery")):
            return "managed"
        return "stable"


class MacroPayoff(BaseModel):
    payoff_id: str
    category: RewardTag
    template_id: str = ""
    target_chapter_hint: str = ""
    setup_requirement: str = ""
    success_signal: str = ""

    @field_validator("category", mode="before")
    @classmethod
    def _normalize_category(cls, value: object) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"power", "social", "justice", "mystery", "emotion"}:
            return normalized
        if any(token in normalized for token in ("item", "acquisition", "ability", "resource", "breakthrough", "power")):
            return "power"
        if any(token in normalized for token in ("social", "relationship", "alliance", "status")):
            return "social"
        if any(token in normalized for token in ("justice", "revenge", "punishment", "fair")):
            return "justice"
        if any(token in normalized for token in ("emotion", "bond", "heart", "affection")):
            return "emotion"
        return "mystery"

    @field_validator("target_chapter_hint", "template_id", "setup_requirement", "success_signal", mode="before")
    @classmethod
    def _coerce_text_fields(cls, value: object) -> str:
        return _stringify_llm_item(value)


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

    @field_validator("layer_id", "layer_type", "summary", mode="before")
    @classmethod
    def _coerce_text_fields(cls, value: object) -> str:
        return _stringify_llm_item(value)

    @field_validator("chapter_window", mode="before")
    @classmethod
    def _coerce_chapter_window(cls, value: object) -> str:
        return _stringify_window(value)


class ArcPayoffMap(BaseModel):
    macro_payoffs: list[MacroPayoff] = Field(default_factory=list)
    awe_kit: list[str] = Field(default_factory=list)
    revelation_layers: list[RevelationLayer] = Field(default_factory=list)
    ambiguity_constraints: list[str] = Field(default_factory=list)

    @field_validator("awe_kit", "ambiguity_constraints", mode="before")
    @classmethod
    def _normalize_string_list(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [
            text
            for text in (_stringify_llm_item(item) for item in value)
            if text
        ]


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
