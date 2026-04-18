from __future__ import annotations

from pydantic import BaseModel, Field

from forwin.protocol.experience import RewardTag


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


class SceneOutput(BaseModel):
    scene_no: int
    scene_objective: str
    scene_time_point: str = ""
    scene_location_id: str = ""
    involved_entities: list[str] = Field(default_factory=list)
    text: str
    text_blob_path: str = ""
    micro_summary: str = ""
    reward_beat_tag: RewardTag = "mystery"
    immersion_anchor: str = ""
    progress_marker: str = ""
    continuation: SceneContinuation = Field(default_factory=SceneContinuation)
