from __future__ import annotations

from forwin.protocol.review import ContinuityIssue

from .signals import CanonQualitySignal, dedupe_signals, make_signal_id


CONTINUITY_ISSUE_TO_SIGNAL_TYPE: dict[str, str] = {
    "dead_character_active": "dead_character_resurrection",
    "thread_already_closed": "closed_thread_reopened",
    "character_teleport": "impossible_location_teleport",
    "location_teleport": "impossible_location_teleport",
    "impossible_location_teleport": "impossible_location_teleport",
}


def signals_from_continuity_issues(
    *,
    project_id: str,
    chapter_number: int,
    issues: list[ContinuityIssue],
    draft_id: str = "",
) -> list[CanonQualitySignal]:
    signals: list[CanonQualitySignal] = []
    for index, issue in enumerate(issues, start=1):
        signal_type = _signal_type_for_issue(issue)
        if not signal_type:
            continue
        subject = _subject_for_issue(issue)
        evidence_refs = [str(ref) for ref in issue.evidence_refs if str(ref).strip()]
        if not evidence_refs:
            evidence_refs = [f"continuity:{issue.rule_name or signal_type}:{index}"]
        signals.append(
            CanonQualitySignal(
                signal_id=make_signal_id(project_id, chapter_number, signal_type, subject, index),
                project_id=project_id,
                chapter_number=chapter_number,
                signal_type=signal_type,
                severity="error",
                target_scope=_target_scope_for_issue(issue),
                subject_key=subject,
                description=str(issue.description or signal_type),
                evidence_refs=evidence_refs,
                payload={
                    "draft_id": draft_id,
                    "source_mode": "deterministic",
                    "blocking_origin": "continuity_adapter",
                    "continuity_rule_name": str(issue.rule_name or ""),
                    "continuity_issue_type": str(issue.issue_type or ""),
                    "confidence": 1.0,
                },
            )
        )
    return dedupe_signals(signals)


def _signal_type_for_issue(issue: ContinuityIssue) -> str:
    for key in (issue.rule_name, issue.issue_type):
        mapped = CONTINUITY_ISSUE_TO_SIGNAL_TYPE.get(str(key or "").strip())
        if mapped:
            return mapped
    return ""


def _subject_for_issue(issue: ContinuityIssue) -> str:
    for name in issue.entity_names or []:
        normalized = str(name or "").strip()
        if normalized:
            return f"entity:{normalized}"
    for ref in issue.evidence_refs or []:
        normalized = str(ref or "").strip()
        if normalized:
            return normalized
    return str(issue.rule_name or issue.issue_type or "continuity")


def _target_scope_for_issue(issue: ContinuityIssue) -> str:
    target_scope = str(issue.target_scope or "").strip()
    if target_scope in {"body", "chapter", "character", "ledger", "book"}:
        return target_scope
    if target_scope == "scene":
        return "chapter"
    return "chapter"


__all__ = ["CONTINUITY_ISSUE_TO_SIGNAL_TYPE", "signals_from_continuity_issues"]
