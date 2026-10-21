from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .signals import CanonQualitySignal


class CanonQualityAnalysisResult(BaseModel):
    project_id: str
    chapter_number: int
    draft_id: str = ""
    signals: list[CanonQualitySignal] = Field(default_factory=list)
    deterministic_quality_report: dict[str, Any] = Field(default_factory=dict)
    mode: str = "chapter_review_form"
    summary: str = ""
    review_issues: list[dict[str, Any]] = Field(default_factory=list)
    raw_analyzer_results: list[dict[str, Any]] = Field(default_factory=list)
    blocking: bool = False
    confidence: float = 0.0


class QualityAnalysisCachePayload(BaseModel):
    analysis: CanonQualityAnalysisResult


__all__ = ["CanonQualityAnalysisResult", "QualityAnalysisCachePayload"]
