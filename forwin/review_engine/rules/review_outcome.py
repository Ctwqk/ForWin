from __future__ import annotations

from dataclasses import dataclass

from forwin.canon_quality.signals import CanonQualitySignal
from forwin.narrative_obligations.types import NarrativeObligation, ReviewOutcome
from forwin.protocol.review import ReviewVerdict

from ..types import Decision, DecisionInput, DecisionRule

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


def build_review_outcome_rules(_: object | None = None) -> list[DecisionRule]:
    return [
        DecisionRule(
            rule_id="review_outcome_policy",
            source_dispatcher="review_engine",
            priority=100,
            matches=lambda _input: True,
            decide=decide_review_outcome,
        )
    ]


def decide_review_outcome(input: DecisionInput) -> Decision:
    facts = _facts_from_review(input.review) + _facts_from_signals(input.signals)
    obligations = list(input.open_obligations)
    is_final = bool(
        input.target_total_chapters
        and int(input.chapter_number or 0) >= int(input.target_total_chapters or 0)
    )
    if not facts and not obligations and input.review.verdict == "pass":
        return _decision(
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

    if is_final and (
        issue_class in _BOOK_TYPES
        or any(item.priority in {"P0", "P1"} for item in obligations)
    ):
        return _decision(
            action="block",
            reason="final chapter cannot carry mainline obligations",
            primary_issue_class=issue_class or "final_obligation",
            minimum_scope="book",
            blocking_signal_ids=blocking_signal_ids,
        )

    if primary is not None and issue_class in _LOCAL_REWRITE_TYPES:
        return _decision(
            action="local_rewrite",
            reason=f"{issue_class} must be fixed in the current draft",
            primary_issue_class=issue_class,
            minimum_scope="draft",
            blocking_signal_ids=blocking_signal_ids,
        )

    if primary is not None and input.review.verdict == "fail":
        if issue_class in _STRUCTURAL_ARC_ERRORS or primary.target_scope == "arc":
            return _decision(
                action="arc_replan_then_rewrite",
                reason=f"{issue_class} requires arc-level repair",
                primary_issue_class=issue_class,
                minimum_scope="arc",
                blocking_signal_ids=blocking_signal_ids,
            )
        if issue_class in _BAND_DEFER_TYPES or primary.target_scope == "band":
            return _decision(
                action="band_replan_then_rewrite",
                reason=f"{issue_class} requires band-level repair",
                primary_issue_class=issue_class,
                minimum_scope="band",
                blocking_signal_ids=blocking_signal_ids,
            )
        if issue_class in _CHAPTER_DEFER_TYPES or primary.target_scope in {"chapter", "chapter_plan"}:
            return _decision(
                action="chapter_replan_then_rewrite",
                reason=f"{issue_class} requires chapter-plan repair",
                primary_issue_class=issue_class,
                minimum_scope="chapter_plan",
                blocking_signal_ids=blocking_signal_ids,
            )
        return _decision(
            action="local_rewrite",
            reason=f"{issue_class or 'review_failure'} requires current draft repair",
            primary_issue_class=issue_class,
            minimum_scope="draft",
            blocking_signal_ids=blocking_signal_ids,
        )

    if primary is not None and issue_class in _ARC_DEFER_TYPES:
        return _decision(
            action="defer_with_arc_plan_patch",
            reason=f"{issue_class} can be deferred only with an arc plan patch",
            primary_issue_class=issue_class,
            minimum_scope="arc",
        )
    if primary is not None and issue_class in _BAND_DEFER_TYPES:
        return _decision(
            action="defer_with_band_plan_patch",
            reason=f"{issue_class} can be deferred only with a band plan patch",
            primary_issue_class=issue_class,
            minimum_scope="band",
        )
    if primary is not None and issue_class in _CHAPTER_DEFER_TYPES:
        return _decision(
            action="defer_with_chapter_plan_patch",
            reason=f"{issue_class} can be deferred only with a future chapter plan patch",
            primary_issue_class=issue_class,
            minimum_scope="chapter_plan",
        )
    if facts:
        has_error = any(fact.severity == "error" for fact in facts)
        return _decision(
            action="block" if has_error else "manual_review_required",
            reason=f"{issue_class or 'unknown_issue'} has no automatic route",
            primary_issue_class=issue_class,
            minimum_scope="manual" if has_error else "draft",
            blocking_signal_ids=blocking_signal_ids,
        )
    return _decision(
        action="commit_clean",
        reason="review passed",
        minimum_scope="draft",
    )


def decision_from_review_outcome(outcome: ReviewOutcome) -> Decision:
    return _decision(
        action=outcome.action,
        reason=outcome.reason,
        primary_issue_class=outcome.primary_issue_class,
        minimum_scope=outcome.minimum_scope,
        blocking_signal_ids=list(outcome.blocking_signal_ids),
        obligation_ids=list(outcome.obligation_ids),
        plan_patch_ids=list(outcome.plan_patch_ids),
        deadline_chapter=outcome.deadline_chapter,
        payoff_test=outcome.payoff_test,
    )


def review_action_from_decision(decision: Decision, fallback_action: str = "") -> str:
    review_action = str(decision.sub_action.get("review_action") or "").strip()
    if review_action:
        return review_action
    return _review_action_for_outcome(str(decision.outcome or "").strip(), fallback_action)


def _decision(
    *,
    action: str,
    reason: str,
    minimum_scope: str,
    primary_issue_class: str = "",
    blocking_signal_ids: list[str] | None = None,
    obligation_ids: list[str] | None = None,
    plan_patch_ids: list[str] | None = None,
    deadline_chapter: int = 0,
    payoff_test: str = "",
) -> Decision:
    return Decision(
        outcome=_decision_outcome_for_review_action(action),
        reason=reason,
        rule_id="review_outcome_policy",
        missing_evidence=[],
        routed_from="review_engine",
        sub_action={
            "review_action": action,
            "minimum_scope": minimum_scope,
            "primary_issue_class": primary_issue_class,
            "blocking_signal_ids": list(blocking_signal_ids or []),
            "obligation_ids": list(obligation_ids or []),
            "plan_patch_ids": list(plan_patch_ids or []),
            "deadline_chapter": int(deadline_chapter or 0),
            "payoff_test": payoff_test,
        },
    )


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


def _decision_outcome_for_review_action(action: str) -> str:
    normalized = str(action or "").strip()
    if normalized == "commit_clean":
        return "auto_approve"
    if normalized == "commit_with_obligation":
        return "commit_with_obligation"
    if normalized == "local_rewrite":
        return "local_repair"
    if normalized == "chapter_replan_then_rewrite":
        return "chapter_patch"
    if normalized == "band_replan_then_rewrite":
        return "band_patch"
    if normalized == "arc_replan_then_rewrite":
        return "arc_patch"
    if normalized == "defer_with_chapter_plan_patch":
        return "chapter_patch"
    if normalized == "defer_with_band_plan_patch":
        return "band_patch"
    if normalized == "defer_with_arc_plan_patch":
        return "arc_patch"
    if normalized in {"block", "book_replan_required"}:
        return "system_block"
    return "manual_review"


def _review_action_for_outcome(outcome: str, fallback_action: str = "") -> str:
    if outcome == "auto_approve":
        return "commit_clean"
    if outcome == "local_repair":
        return "local_rewrite"
    if outcome == "chapter_patch":
        return "defer_with_chapter_plan_patch"
    if outcome == "band_patch":
        return "defer_with_band_plan_patch"
    if outcome == "arc_patch":
        return "defer_with_arc_plan_patch"
    if outcome == "book_patch":
        return "book_replan_required"
    if outcome == "commit_with_obligation":
        return "commit_with_obligation"
    if outcome == "manual_review":
        return "manual_review"
    if outcome == "system_block":
        return "block"
    return str(fallback_action or "").strip()
