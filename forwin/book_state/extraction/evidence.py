from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from typing import get_origin

from .chapter_delta_schema import ChapterDeltaExtraction, ExtractionIssue


_ITEM_FIELDS = tuple(
    name for name, field in ChapterDeltaExtraction.model_fields.items()
    if get_origin(field.annotation) is list
)


def normalize_for_evidence(text: str) -> str:
    return unicodedata.normalize("NFKC", "".join(text.split()))


def evidence_in_body(evidence: str, body: str) -> bool:
    normalized = normalize_for_evidence(evidence)
    return bool(normalized) and normalized in normalize_for_evidence(body)


def validate_evidence(
    extraction: ChapterDeltaExtraction, body: str,
) -> tuple[ChapterDeltaExtraction, list[ExtractionIssue]]:
    result = extraction.model_copy(deep=True)
    issues: list[ExtractionIssue] = []
    normalized_body = normalize_for_evidence(body)
    for field in _ITEM_FIELDS:
        kept = []
        for index, item in enumerate(getattr(result, field)):
            evidence = normalize_for_evidence(item.evidence)
            if field == "plan_fulfillment" and item.status == "missing" and not evidence:
                kept.append(item)
            elif evidence and evidence in normalized_body:
                kept.append(item)
            else:
                issues.append(ExtractionIssue(
                    severity="warning", code="evidence_not_in_body", path=f"{field}[{index}]",
                    message="Evidence is not a passage from the chapter body.",
                ))
        setattr(result, field, kept)

    if result.time.elapsed is not None:
        time_evidence = normalize_for_evidence(result.time.evidence)
        if not time_evidence:
            result.time.elapsed = None
            issues.append(ExtractionIssue(
                severity="warning", code="elapsed_without_evidence", path="time",
                message="Elapsed time has no supporting evidence.",
            ))
        elif time_evidence not in normalized_body:
            result.time.elapsed = None
            issues.append(ExtractionIssue(
                severity="warning", code="evidence_not_in_body", path="time",
                message="Time evidence is not a passage from the chapter body.",
            ))
    return result, issues


def dropped_ratio(
    issues: Iterable[ExtractionIssue], extraction_before: ChapterDeltaExtraction,
) -> float:
    """Fraction of list entries dropped; clearing elapsed time is not a dropped entry."""
    paths = {
        f"{field}[{index}]"
        for field in _ITEM_FIELDS
        for index in range(len(getattr(extraction_before, field)))
    }
    if not paths:
        return 0.0
    dropped = {issue.path for issue in issues if issue.code == "evidence_not_in_body"}
    return len(dropped & paths) / len(paths)
