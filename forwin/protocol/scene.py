from __future__ import annotations

from pydantic import BaseModel, Field


class ScenePlan(BaseModel):
    scene_no: int
    objective: str
    must_progress_points: list[str] = Field(default_factory=list)
    time_hint: str = ""
    location_hint: str = ""
    involved_entities: list[str] = Field(default_factory=list)
    micro_hook: str = ""
    target_chars: int = 800


class SceneOutput(BaseModel):
    scene_no: int
    scene_objective: str
    scene_time_point: str = ""
    scene_location_id: str = ""
    involved_entities: list[str] = Field(default_factory=list)
    text: str
    text_blob_path: str = ""
    micro_summary: str = ""
