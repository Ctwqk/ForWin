from __future__ import annotations

from forwin.narrative_obligations.types import ReviewOutcome
from forwin.reviewer.outcome import ReviewOutcomeRouter

from ..types import Decision, DecisionInput, DecisionRule


def build_review_outcome_rules(router: ReviewOutcomeRouter | None = None) -> list[DecisionRule]:
    resolved_router = router or ReviewOutcomeRouter()
    return [
        DecisionRule(
            rule_id="legacy_review_outcome_router",
            source_dispatcher="ReviewOutcomeRouter",
            priority=100,
            matches=lambda _input: True,
            decide=lambda input: _decision_from_review_outcome(resolved_router, input),
        )
    ]


def _decision_from_review_outcome(router: ReviewOutcomeRouter, input: DecisionInput) -> Decision:
    outcome = router.route(
        review=input.review,
        signals=input.signals,
        open_obligations=input.open_obligations,
        attempt_history=[{"scope": scope} for scope in input.prior_scope_history],
        current_chapter=input.chapter_number,
        target_total_chapters=input.target_total_chapters,
    )
    return decision_from_review_outcome(outcome)


def decision_from_review_outcome(outcome: ReviewOutcome) -> Decision:
    return Decision(
        outcome=_decision_outcome_for_legacy_action(outcome.action),
        reason=outcome.reason,
        rule_id="legacy_review_outcome_router",
        missing_evidence=[],
        routed_from="ReviewOutcomeRouter",
        sub_action={
            "legacy_action": outcome.action,
            "minimum_scope": outcome.minimum_scope,
            "primary_issue_class": outcome.primary_issue_class,
            "blocking_signal_ids": list(outcome.blocking_signal_ids),
            "obligation_ids": list(outcome.obligation_ids),
            "plan_patch_ids": list(outcome.plan_patch_ids),
            "deadline_chapter": outcome.deadline_chapter,
            "payoff_test": outcome.payoff_test,
        },
    )


def _decision_outcome_for_legacy_action(action: str) -> str:
    normalized = str(action or "").strip()
    if normalized == "commit_clean":
        return "auto_approve"
    if normalized == "commit_with_obligation":
        return "commit_with_obligation"
    if normalized in {
        "local_rewrite",
        "chapter_replan_then_rewrite",
        "band_replan_then_rewrite",
        "arc_replan_then_rewrite",
    }:
        return "local_repair"
    if normalized == "defer_with_chapter_plan_patch":
        return "chapter_patch"
    if normalized == "defer_with_band_plan_patch":
        return "band_patch"
    if normalized == "defer_with_arc_plan_patch":
        return "arc_patch"
    if normalized in {"block", "book_replan_required"}:
        return "system_block"
    return "manual_review"
