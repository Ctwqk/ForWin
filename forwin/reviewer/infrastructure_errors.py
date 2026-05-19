from __future__ import annotations

import re
from typing import Iterable

from forwin.protocol.review import ContinuityIssue


_INFRASTRUCTURE_ERROR_TYPES = {
    "form_schema_invalid",
    "writer_prompt_assembly_error",
}

_INFRASTRUCTURE_ERROR_PATTERNS = (
    re.compile(r"\bValidationError\b", re.IGNORECASE),
    re.compile(r"\bPydantic\b", re.IGNORECASE),
    re.compile(r"\bInput should be a valid\b", re.IGNORECASE),
    re.compile(r"\btype=string_type\b", re.IGNORECASE),
    re.compile(r"\bChapterReviewAnswers\b", re.IGNORECASE),
    re.compile(r"\bform_schema_invalid\b", re.IGNORECASE),
)


def is_infrastructure_issue(issue: ContinuityIssue) -> bool:
    issue_type = str(issue.issue_type or issue.rule_name or "").strip()
    if issue_type in _INFRASTRUCTURE_ERROR_TYPES:
        return True
    haystack = "\n".join(
        [
            str(issue.description or ""),
            str(issue.suggested_fix or ""),
            str(issue.source_layer or ""),
            str(issue.source_analyzer or ""),
            str(issue.source_mode or ""),
            str(issue.original_result or ""),
        ]
    )
    return any(pattern.search(haystack) for pattern in _INFRASTRUCTURE_ERROR_PATTERNS)


def infrastructure_issues(issues: Iterable[ContinuityIssue]) -> list[ContinuityIssue]:
    return [issue for issue in issues if is_infrastructure_issue(issue)]


def filter_writer_fixable_issues(issues: Iterable[ContinuityIssue]) -> list[ContinuityIssue]:
    return [issue for issue in issues if not is_infrastructure_issue(issue)]


def infrastructure_issue_types(issues: Iterable[ContinuityIssue]) -> list[str]:
    values: list[str] = []
    for issue in infrastructure_issues(issues):
        key = str(issue.issue_type or issue.rule_name or "infrastructure_error").strip()
        values.append(key or "infrastructure_error")
    return list(dict.fromkeys(values))


__all__ = [
    "filter_writer_fixable_issues",
    "infrastructure_issue_types",
    "infrastructure_issues",
    "is_infrastructure_issue",
]
