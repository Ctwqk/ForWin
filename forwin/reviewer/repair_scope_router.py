from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from forwin.canon_quality.signals import CanonQualitySignal, SignalKind
from forwin.protocol.review import ContinuityIssue, ReviewVerdict


class RepairScopeKind(StrEnum):
    OPERATOR = "operator"
    ACTIVE_RULES = "active_rules"
    SUBWORLD = "subworld"
    BAND_PLAN = "band_plan"
    CHAPTER_PLAN = "chapter_plan"
    DRAFT = "draft"


SIGNAL_KIND_TO_SCOPE: dict[str, RepairScopeKind] = {
    SignalKind.form_schema_invalid.value: RepairScopeKind.OPERATOR,
    SignalKind.form_llm_unavailable.value: RepairScopeKind.OPERATOR,
    SignalKind.form_budget_exceeded.value: RepairScopeKind.OPERATOR,
    SignalKind.form_answer_rejected.value: RepairScopeKind.OPERATOR,
    SignalKind.writer_prompt_assembly_error.value: RepairScopeKind.OPERATOR,
    SignalKind.form_countdown_inconsistency.value: RepairScopeKind.ACTIVE_RULES,
    SignalKind.countdown_non_monotonic.value: RepairScopeKind.ACTIVE_RULES,
    SignalKind.active_rule_missing.value: RepairScopeKind.ACTIVE_RULES,
    SignalKind.subworld_admission_missing_canon_entity.value: RepairScopeKind.SUBWORLD,
    SignalKind.subworld_admission_unauthorized_new_entity.value: RepairScopeKind.DRAFT,
    SignalKind.form_open_signal_persisting.value: RepairScopeKind.DRAFT,
    SignalKind.personality_drift.value: RepairScopeKind.DRAFT,
    SignalKind.placeholder_leakage.value: RepairScopeKind.DRAFT,
    SignalKind.bare_role_placeholder_leakage.value: RepairScopeKind.DRAFT,
    SignalKind.protagonist_placeholder_leakage.value: RepairScopeKind.DRAFT,
    SignalKind.internal_state_key_leakage.value: RepairScopeKind.DRAFT,
    SignalKind.body_duplicate_span.value: RepairScopeKind.DRAFT,
    SignalKind.style_repetition.value: RepairScopeKind.DRAFT,
    SignalKind.form_obligation_unresolved.value: RepairScopeKind.CHAPTER_PLAN,
    SignalKind.form_final_chapter_unresolved.value: RepairScopeKind.CHAPTER_PLAN,
    SignalKind.terminal_state_active_conflict.value: RepairScopeKind.CHAPTER_PLAN,
    SignalKind.artifact_count_conflict.value: RepairScopeKind.CHAPTER_PLAN,
    SignalKind.repeated_reveal_as_new.value: RepairScopeKind.CHAPTER_PLAN,
}

_SCOPE_PRIORITY = {
    RepairScopeKind.OPERATOR: 0,
    RepairScopeKind.ACTIVE_RULES: 1,
    RepairScopeKind.SUBWORLD: 2,
    RepairScopeKind.BAND_PLAN: 3,
    RepairScopeKind.CHAPTER_PLAN: 4,
    RepairScopeKind.DRAFT: 5,
}


@dataclass(frozen=True)
class RoutedSignal:
    kind: str
    severity: str = "warning"
    subject_key: str = ""
    description: str = ""
    source_signal_id: str = ""
    source: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RepairScopeDispatch:
    scope: RepairScopeKind
    signals: list[RoutedSignal]


def route_signal_kind(kind: str) -> RepairScopeKind:
    return SIGNAL_KIND_TO_SCOPE.get(str(kind or "").strip(), RepairScopeKind.OPERATOR)


def route_review_repair_scopes(
    review: ReviewVerdict,
    *,
    signals: list[CanonQualitySignal] | None = None,
) -> list[RepairScopeDispatch]:
    grouped: dict[RepairScopeKind, list[RoutedSignal]] = {}
    for routed in _routed_from_review(review):
        grouped.setdefault(route_signal_kind(routed.kind), []).append(routed)
    for signal in signals or []:
        routed = RoutedSignal(
            kind=str(signal.signal_type or ""),
            severity=str(signal.severity or "warning"),
            subject_key=str(signal.subject_key or ""),
            description=str(signal.description or ""),
            source_signal_id=str(signal.signal_id or ""),
            source="canon_quality_signal",
            payload=dict(signal.payload or {}),
        )
        grouped.setdefault(route_signal_kind(routed.kind), []).append(routed)
    return [
        RepairScopeDispatch(scope=scope, signals=grouped[scope])
        for scope in sorted(grouped, key=lambda item: _SCOPE_PRIORITY.get(item, 99))
    ]


def _routed_from_review(review: ReviewVerdict) -> list[RoutedSignal]:
    routed: list[RoutedSignal] = []
    for issue in review.issues:
        if not _issue_is_blocking(issue):
            continue
        kind = _issue_kind(issue)
        if not kind:
            continue
        routed.append(
            RoutedSignal(
                kind=kind,
                severity=str(issue.severity or "warning"),
                subject_key=str(issue.rule_name or ""),
                description=str(issue.description or ""),
                source="review_issue",
                payload={"reviewer": issue.reviewer, "source_analyzer": issue.source_analyzer},
            )
        )
    return routed


def _issue_is_blocking(issue: ContinuityIssue) -> bool:
    return bool(issue.blocking) or str(issue.severity or "") == "error"


def _issue_kind(issue: ContinuityIssue) -> str:
    return str(issue.issue_type or issue.rule_name or "").strip()


__all__ = [
    "RepairScopeDispatch",
    "RepairScopeKind",
    "RoutedSignal",
    "SIGNAL_KIND_TO_SCOPE",
    "route_review_repair_scopes",
    "route_signal_kind",
]
