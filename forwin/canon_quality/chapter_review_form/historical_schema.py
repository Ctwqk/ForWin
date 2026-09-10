"""Evidence contract for historical-only coverage in the existing chapter form."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from .form_schema import ChapterReviewAnswers
from .service import ChapterReviewFormResult

HISTORICAL_DIMENSIONS = (
    "possession",
    "knowledge",
    "life_state",
    "time",
    "place",
    "obligations",
)
HistoricalDimension = Literal[
    "possession", "knowledge", "life_state", "time", "place", "obligations"
]
CoverageStatus = Literal["pass", "fail", "unknown"]


class HistoricalBodyEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: StrictInt = Field(ge=0)
    end: StrictInt = Field(gt=0)
    quote: str = Field(min_length=1)


class HistoricalDimensionAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension: HistoricalDimension
    status: CoverageStatus
    explanation: str
    body_evidence: list[HistoricalBodyEvidence]
    prefix_refs: list[str]


class HistoricalFormAnswers(BaseModel):
    """One model response includes both existing form answers and coverage."""

    model_config = ConfigDict(extra="forbid")

    answers: ChapterReviewAnswers
    body_sha256: str
    prefix_sha256: str
    coverage_complete: StrictBool
    coverage: list[HistoricalDimensionAnswer]


class HistoricalPrefixContext(BaseModel):
    """Caller-owned, freshly rebuilt state immediately before the reviewed chapter.

    `complete` attests to replica provenance, not merely successful serialization.
    The owner must provide all six fact collections and unpruned form inputs.
    This module never populates missing collections from live/old projections.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str
    through_chapter: StrictInt = Field(ge=0)
    complete: StrictBool
    facts: dict[str, Any]
    character_rows: list[dict[str, Any]]
    countdown_rows: list[dict[str, Any]]
    open_signal_rows: list[dict[str, Any]]
    obligations: list[dict[str, Any]]


class HistoricalCoverageCheck(BaseModel):
    """Duck-compatible with Canon's RevisionCheck without importing admission."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dimension: HistoricalDimension
    status: CoverageStatus
    evidence_refs: tuple[str, ...] = ()
    explanation: str


class HistoricalChapterReviewResult(BaseModel):
    review: ChapterReviewFormResult
    checks: tuple[HistoricalCoverageCheck, ...]
