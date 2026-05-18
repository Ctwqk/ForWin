from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .canon_projector import ProjectionResult
from .evidence_validator import ValidationReport
from .form_schema import ChapterReviewAnswers


def summarize_form_run(
    answers: ChapterReviewAnswers,
    validation_report: ValidationReport,
    projection: ProjectionResult,
) -> dict[str, Any]:
    return {
        "validated_count": len(validation_report.validated),
        "rejected_count": len(validation_report.rejected),
        "signals_by_severity": _count_by(signal.severity for signal in projection.signals),
        "blocking_eligible": [
            signal.model_dump(mode="json")
            for signal in projection.signals
            if signal.severity in {"error", "warning"}
        ],
        "top_rejections": [item.model_dump(mode="json") for item in validation_report.rejected[:10]],
    }


def _count_by(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts
