from __future__ import annotations
from pydantic import BaseModel, Field
from forwin.book_state.extraction.chapter_delta_schema import ChapterDeltaExtraction
from .scene import SceneOutput


class WriterOutput(BaseModel):
    """Structured output from the Chapter Writer."""

    project_id: str = ""
    chapter_number: int
    title: str
    body: str  # The actual Chinese chapter text
    char_count: int = 0  # len(body), computed after parse
    end_of_chapter_summary: str  # 1-2 sentence recap in Chinese
    draft_blob_path: str = ""
    scene_outputs: list[SceneOutput] = Field(default_factory=list)
    chapter_delta: ChapterDeltaExtraction | None = None
    prompt_revision_hash: str = ""
    generation_meta: dict[str, object] = Field(default_factory=dict)
