from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from forwin.protocol.review import ReviewVerdict

IssueScope = Literal[
    "draft",
    "chapter_plan",
    "band_plan",
    "arc_plan",
    "book_plan",
    "subworld",
    "active_rules",
    "operator",
]

ISSUE_TO_SCOPE: dict[str, IssueScope] = {
    "placeholder_leakage": "draft",
    "bare_role_placeholder_leakage": "draft",
    "protagonist_placeholder_leakage": "draft",
    "internal_state_key_leakage": "draft",
    "body_truncated": "draft",
    "body_duplicate_span": "draft",
    "style_repetition": "draft",
    "personality_drift": "draft",
    "subworld_admission_unauthorized_new_entity": "draft",
    "single_chapter_pacing": "chapter_plan",
    "single_chapter_callback": "chapter_plan",
    "form_obligation_unresolved": "chapter_plan",
    "form_final_chapter_unresolved": "chapter_plan",
    "terminal_state_active_conflict": "chapter_plan",
    "artifact_count_conflict": "chapter_plan",
    "repeated_reveal_as_new": "chapter_plan",
    "identity_within_band": "band_plan",
    "foreshadow_band": "band_plan",
    "identity_ambiguity": "arc_plan",
    "countdown_explanation": "arc_plan",
    "artifact_count_explanation": "arc_plan",
    "world_rule_explanation": "arc_plan",
    "relationship_reconciliation": "arc_plan",
    "character_state_conflict": "arc_plan",
    "book_structure_violation": "book_plan",
    "final_hook_closure": "book_plan",
    "final_hook_unresolved": "book_plan",
    "final_resolution_missing": "book_plan",
    "final_resolution_pending": "book_plan",
    "subworld_admission_missing_canon_entity": "subworld",
    "countdown_state_drift": "active_rules",
    "countdown_non_monotonic": "active_rules",
    "form_countdown_inconsistency": "active_rules",
    "active_rule_missing": "active_rules",
    "form_schema_invalid": "operator",
    "form_llm_unavailable": "operator",
    "form_budget_exceeded": "operator",
    "form_answer_rejected": "operator",
    "writer_prompt_assembly_error": "operator",
}

_SCOPE_RANK: dict[IssueScope, int] = {
    "draft": 1,
    "chapter_plan": 2,
    "band_plan": 3,
    "arc_plan": 4,
    "subworld": 4,
    "active_rules": 4,
    "book_plan": 5,
    "operator": 6,
}
_SEVERITY_RANK = {
    "info": 1,
    "warning": 2,
    "warn": 2,
    "error": 3,
    "critical": 4,
    "blocker": 4,
}


@dataclass(frozen=True)
class ClassifiedIssue:
    kind: str
    scope: IssueScope
    severity: str
    source_layer: str = ""
    evidence_refs: tuple[str, ...] = ()


def scope_for_issue_kind(issue_kind: str) -> IssueScope:
    return ISSUE_TO_SCOPE.get(str(issue_kind or "").strip(), "chapter_plan")


def classify_primary_issue(*, review: ReviewVerdict, signals: list[object]) -> ClassifiedIssue:
    candidates: list[ClassifiedIssue] = []
    for issue in review.issues:
        kind = str(issue.issue_type or issue.rule_name or "").strip()
        if not kind:
            continue
        candidates.append(
            ClassifiedIssue(
                kind=kind,
                scope=scope_for_issue_kind(kind),
                severity=str(issue.severity or "warning"),
                source_layer=str(issue.source_layer or ""),
                evidence_refs=tuple(issue.evidence_refs or []),
            )
        )
    for signal in signals:
        kind = str(
            getattr(signal, "signal_type", "")
            or getattr(signal, "kind", "")
            or ""
        ).strip()
        if not kind:
            continue
        candidates.append(
            ClassifiedIssue(
                kind=kind,
                scope=scope_for_issue_kind(kind),
                severity=str(getattr(signal, "severity", "") or "warning"),
                source_layer=str(getattr(signal, "source_layer", "") or ""),
                evidence_refs=tuple(getattr(signal, "evidence_refs", []) or []),
            )
        )
    if not candidates:
        return ClassifiedIssue(
            kind="review_verdict",
            scope="chapter_plan",
            severity=str(review.verdict or "warning"),
        )
    return max(
        candidates,
        key=lambda item: (
            _SEVERITY_RANK.get(item.severity, 0),
            _SCOPE_RANK.get(item.scope, 0),
            len(item.evidence_refs),
        ),
    )
