from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any, get_origin

from .chapter_delta_schema import ChapterDeltaExtraction, ExtractionIssue


_ITEM_FIELDS = tuple(
    name for name, field in ChapterDeltaExtraction.model_fields.items()
    if get_origin(field.annotation) is list
)
# The lists that say what happened in the chapter; the hard floor counts them as events.
FACT_FIELDS = (
    "events", "entity_changes", "relation_changes", "knowledge_events",
    "movements", "item_transfers", "thread_beats",
)


def normalize_for_evidence(text: str) -> str:
    return unicodedata.normalize("NFKC", "".join(text.split()))


def _quotable(normalized: str) -> bool:
    """A quote needs a letter or digit; punctuation alone matches almost any body."""
    return any(char.isalnum() for char in normalized)


def evidence_in_body(evidence: str, body: str) -> bool:
    normalized = normalize_for_evidence(evidence)
    return _quotable(normalized) and normalized in normalize_for_evidence(body)


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
            elif _quotable(evidence) and evidence in normalized_body:
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
        elif not _quotable(time_evidence) or time_evidence not in normalized_body:
            result.time.elapsed = None
            issues.append(ExtractionIssue(
                severity="warning", code="evidence_not_in_body", path="time",
                message="Time evidence is not a passage from the chapter body.",
            ))
    return result, issues


def has_facts(extraction: ChapterDeltaExtraction) -> bool:
    return any(getattr(extraction, field) for field in FACT_FIELDS)


def every_fact_dropped(
    extraction: ChapterDeltaExtraction, issues: Iterable[Mapping[str, Any]],
) -> bool:
    """The extractor reported facts and evidence validation dropped every one of them.

    ``issues`` are the extraction's issues as stored in ``chapter_delta_issues``.
    """
    return not has_facts(extraction) and any(
        isinstance(issue, Mapping)
        and issue.get("code") == "evidence_not_in_body"
        and str(issue.get("path") or "").partition("[")[0] in FACT_FIELDS
        for issue in issues
    )


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
