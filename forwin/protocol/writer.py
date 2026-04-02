from __future__ import annotations
from pydantic import BaseModel, Field
from .scene import SceneOutput
from .state_change import StateChangeCandidate, EventCandidate, ThreadBeatCandidate, TimeAdvance


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
    generation_meta: dict[str, object] = Field(default_factory=dict)
