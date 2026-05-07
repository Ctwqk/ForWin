from __future__ import annotations

from pydantic import BaseModel, Field

from forwin.planning.world_contracts import ChapterWorldDeltaIntent
from forwin.protocol.book_state import ApprovedGraphDeltaSet
from forwin.protocol.writer import WriterOutput
from forwin.protocol.world_v4 import ExtractedWorldChangeSet
from forwin.world_v4_review_gate.types import V4ReviewGateVerdict


class BookStateExtractionRequest(BaseModel):
    project_id: str
    chapter_number: int
    writer_output: WriterOutput
    chapter_intent: ChapterWorldDeltaIntent | None = None
    review_verdict_id: str = ""
    forced_accept_reason: str = ""


class BookStateExtractionIssue(BaseModel):
    severity: str = "error"
    code: str
    message: str
    evidence_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)


class BookStateExtractionResult(BaseModel):
    project_id: str
    chapter_number: int
    accepted: bool
    changes: ApprovedGraphDeltaSet | None = None
    issues: list[BookStateExtractionIssue] = Field(default_factory=list)
    compatibility_extracted: ExtractedWorldChangeSet | None = None
    compatibility_gate_verdict: V4ReviewGateVerdict | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


__all__ = [
    "BookStateExtractionIssue",
    "BookStateExtractionRequest",
    "BookStateExtractionResult",
]
