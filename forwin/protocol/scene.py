from __future__ import annotations

from collections.abc import Collection

from pydantic import BaseModel, Field, field_validator

from forwin.book_state.extraction.chapter_delta_schema import NEW_REF_PREFIX
from forwin.protocol.experience import RewardTag


def _normalize_reward_tag(value: object) -> str:
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


class SceneContinuation(BaseModel):
    scene_no: int = 0
    continuity_anchor: str = ""
    unresolved_micro_hook: str = ""
    next_scene_bridge: str = ""
    time_continuity: str = ""
    location_continuity: str = ""
    character_focus: list[str] = Field(default_factory=list)


class ScenePlan(BaseModel):
    scene_no: int
    objective: str
    must_progress_points: list[str] = Field(default_factory=list)
    time_hint: str = ""
    location_hint: str = ""
    involved_entities: list[str] = Field(default_factory=list)
    micro_hook: str = ""
    target_chars: int = 800
    reward_beat_tag: RewardTag = "mystery"
    immersion_anchor: str = ""
    progress_marker: str = ""

    @field_validator("reward_beat_tag", mode="before")
    @classmethod
    def _coerce_reward_beat_tag(cls, value: object) -> str:
        return _normalize_reward_tag(value)


def is_scene_location_ref(value: str, location_ids: Collection[str]) -> bool:
    """A location id from the world view's location table, or ``new:<name>`` for a place it lacks."""
    text = value.strip()
    if text.startswith(NEW_REF_PREFIX):
        return bool(text.removeprefix(NEW_REF_PREFIX).strip())
    return text in location_ids


class SceneOutput(BaseModel):
    scene_no: int
    scene_objective: str
    scene_time_point: str = ""
    # What the writer declared: a view location id or new:<name>; any other text is kept as written.
    scene_location_id: str = ""
    involved_entities: list[str] = Field(default_factory=list)
    text: str
    text_blob_path: str = ""
    micro_summary: str = ""
    reward_beat_tag: RewardTag = "mystery"
    immersion_anchor: str = ""
    progress_marker: str = ""
    continuation: SceneContinuation = Field(default_factory=SceneContinuation)

    @field_validator("reward_beat_tag", mode="before")
    @classmethod
    def _coerce_reward_beat_tag(cls, value: object) -> str:
        return _normalize_reward_tag(value)
