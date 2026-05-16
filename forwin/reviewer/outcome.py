from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from forwin.canon_quality.signals import CanonQualitySignal
from forwin.narrative_obligations.types import NarrativeObligation, ReviewOutcome
from forwin.protocol.review import ReviewVerdict


_LOCAL_REWRITE_TYPES = {
    "placeholder_leakage",
    "body_incomplete",
    "body_truncated",
    "json_leakage",
    "empty_body",
    "duplicate_paragraph",
    "name_typo",
}
_CHAPTER_DEFER_TYPES = {
    "motivation_gap",
    "transition_bridge_needed",
    "foreshadowing_payoff",
}
_BAND_DEFER_TYPES = {
    "reader_promise_payoff",
    "reveal_escalation_needed",
    "style_repetition_pressure",
}
_ARC_DEFER_TYPES = {
    "identity_ambiguity",
    "countdown_explanation",
    "artifact_count_explanation",
    "world_rule_explanation",
    "relationship_reconciliation",
}
_BOOK_TYPES = {
    "final_hook_closure",
    "final_hook_unresolved",
    "final_resolution_missing",
    "final_resolution_pending",
}
_STRUCTURAL_ARC_ERRORS = {
    "identity_ambiguity",
    "countdown_explanation",
    "artifact_count_explanation",
    "terminal_state_active_conflict",
    "character_state_conflict",
}
_SCOPE_RANK = {
    "draft": 0,
    "scene": 0,
    "chapter": 1,
    "chapter_plan": 1,
    "band": 2,
    "band_plan": 2,
    "arc": 3,
    "book": 4,
    "manual": 5,
}


@dataclass(frozen=True)
class _IssueFact:
    issue_class: str
    severity: str
    signal_id: str = ""
    target_scope: str = ""


class ReviewOutcomeRouter:
    def route(
        self,
        *,
        review: ReviewVerdict,
        signals: list[CanonQualitySignal] | None = None,
        open_obligations: list[NarrativeObligation] | None = None,
        attempt_history: list[dict[str, Any]] | None = None,
        current_chapter: int = 0,
        target_total_chapters: int = 0,
    ) -> ReviewOutcome:
        del attempt_history
        facts = _facts_from_review(review) + _facts_from_signals(signals or [])
        obligations = list(open_obligations or [])
        is_final = bool(target_total_chapters and int(current_chapter or 0) >= int(target_total_chapters or 0))
        if not facts and not obligations and review.verdict == "pass":
            return ReviewOutcome(
                action="commit_clean",
                reason="review passed with no deterministic quality signals",
                minimum_scope="draft",
            )

        primary = _primary_issue(facts)
        issue_class = primary.issue_class if primary is not None else ""
        blocking_signal_ids = [
            fact.signal_id
            for fact in facts
            if fact.signal_id and (fact.severity == "error" or fact.issue_class in _BOOK_TYPES)
        ]

        if is_final and (issue_class in _BOOK_TYPES or any(item.priority in {"P0", "P1"} for item in obligations)):
            return ReviewOutcome(
                action="block",
                reason="final chapter cannot carry mainline obligations",
                primary_issue_class=issue_class or "final_obligation",
                minimum_scope="book",
                blocking_signal_ids=blocking_signal_ids,
            )

        if primary is not None and issue_class in _LOCAL_REWRITE_TYPES:
            return ReviewOutcome(
                action="local_rewrite",
                reason=f"{issue_class} must be fixed in the current draft",
                primary_issue_class=issue_class,
                minimum_scope="draft",
                blocking_signal_ids=blocking_signal_ids,
            )

        if primary is not None and review.verdict == "fail":
            if issue_class in _STRUCTURAL_ARC_ERRORS or primary.target_scope == "arc":
                return ReviewOutcome(
                    action="arc_replan_then_rewrite",
                    reason=f"{issue_class} requires arc-level repair",
                    primary_issue_class=issue_class,
                    minimum_scope="arc",
                    blocking_signal_ids=blocking_signal_ids,
                )
            if issue_class in _BAND_DEFER_TYPES or primary.target_scope == "band":
                return ReviewOutcome(
                    action="band_replan_then_rewrite",
                    reason=f"{issue_class} requires band-level repair",
                    primary_issue_class=issue_class,
                    minimum_scope="band",
                    blocking_signal_ids=blocking_signal_ids,
                )
            if issue_class in _CHAPTER_DEFER_TYPES or primary.target_scope in {"chapter", "chapter_plan"}:
                return ReviewOutcome(
                    action="chapter_replan_then_rewrite",
                    reason=f"{issue_class} requires chapter-plan repair",
                    primary_issue_class=issue_class,
                    minimum_scope="chapter_plan",
                    blocking_signal_ids=blocking_signal_ids,
                )
            return ReviewOutcome(
                action="local_rewrite",
                reason=f"{issue_class or 'review_failure'} requires current draft repair",
                primary_issue_class=issue_class,
                minimum_scope="draft",
                blocking_signal_ids=blocking_signal_ids,
            )

        if primary is not None and issue_class in _ARC_DEFER_TYPES:
            return ReviewOutcome(
                action="defer_with_arc_plan_patch",
                reason=f"{issue_class} can be deferred only with an arc plan patch",
                primary_issue_class=issue_class,
                minimum_scope="arc",
                blocking_signal_ids=[],
            )
        if primary is not None and issue_class in _BAND_DEFER_TYPES:
            return ReviewOutcome(
                action="defer_with_band_plan_patch",
                reason=f"{issue_class} can be deferred only with a band plan patch",
                primary_issue_class=issue_class,
                minimum_scope="band",
                blocking_signal_ids=[],
            )
        if primary is not None and issue_class in _CHAPTER_DEFER_TYPES:
            return ReviewOutcome(
                action="defer_with_chapter_plan_patch",
                reason=f"{issue_class} can be deferred only with a future chapter plan patch",
                primary_issue_class=issue_class,
                minimum_scope="chapter_plan",
                blocking_signal_ids=[],
            )
        if facts:
            return ReviewOutcome(
                action="block" if any(fact.severity == "error" for fact in facts) else "manual_review_required",
                reason=f"{issue_class or 'unknown_issue'} has no automatic route",
                primary_issue_class=issue_class,
                minimum_scope="manual" if any(fact.severity == "error" for fact in facts) else "draft",
                blocking_signal_ids=blocking_signal_ids,
            )
        return ReviewOutcome(
            action="commit_clean",
            reason="review passed",
            minimum_scope="draft",
        )


def repair_scope_for_outcome(outcome: ReviewOutcome, *, attempt_no: int) -> str:
    del attempt_no
    if outcome.action == "local_rewrite":
        return "draft"
    return _scope_to_repair_scope(outcome.minimum_scope)


def merge_repair_scope(
    *,
    deterministic_scope: str,
    requested_scope: str,
    allow_arc: bool,
) -> tuple[str, str]:
    deterministic = _scope_to_repair_scope(deterministic_scope)
    requested = _scope_to_repair_scope(requested_scope)
    if requested == "arc" and not allow_arc and _SCOPE_RANK.get(deterministic, 0) < _SCOPE_RANK["arc"]:
        floor = "band" if _SCOPE_RANK.get(deterministic, 0) < _SCOPE_RANK["band"] else deterministic
        return floor, "arc requested but no structural trigger"
    return _max_scope(deterministic, requested), ""


def _facts_from_review(review: ReviewVerdict) -> list[_IssueFact]:
    facts: list[_IssueFact] = []
    for issue in review.issues:
        issue_class = str(getattr(issue, "issue_type", "") or getattr(issue, "rule_name", "") or "").strip()
        if not issue_class:
            continue
        facts.append(
            _IssueFact(
                issue_class=issue_class,
                severity=str(getattr(issue, "severity", "") or "warning"),
                target_scope=str(getattr(issue, "target_scope", "") or ""),
            )
        )
    return facts


def _facts_from_signals(signals: list[CanonQualitySignal]) -> list[_IssueFact]:
    return [
        _IssueFact(
            issue_class=str(signal.signal_type or ""),
            severity=str(signal.severity or "warning"),
            signal_id=str(signal.signal_id or ""),
            target_scope=str(signal.target_scope or ""),
        )
        for signal in signals
        if str(signal.signal_type or "").strip()
    ]


def _primary_issue(facts: list[_IssueFact]) -> _IssueFact | None:
    if not facts:
        return None
    return sorted(
        facts,
        key=lambda item: (
            0 if item.severity == "error" else 1,
            -_SCOPE_RANK.get(item.target_scope, 0),
            item.issue_class,
        ),
    )[0]


def _scope_to_repair_scope(scope: str) -> str:
    normalized = str(scope or "draft").strip().lower()
    if normalized == "book":
        return "arc"
    if normalized == "manual":
        return "draft"
    if normalized == "chapter":
        return "chapter_plan"
    if normalized == "band_plan":
        return "band"
    if normalized in {"draft", "chapter_plan", "band", "arc"}:
        return normalized
    if normalized == "scene":
        return "draft"
    return "draft"


def _max_scope(left: str, right: str) -> str:
    return left if _SCOPE_RANK.get(left, 0) >= _SCOPE_RANK.get(right, 0) else right
