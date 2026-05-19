from __future__ import annotations

from forwin.review_engine.issue_taxonomy import classify_primary_issue
from forwin.review_engine.types import Decision, DecisionInput


def decide_commit_with_obligation(input: DecisionInput) -> Decision:
    primary = classify_primary_issue(review=input.review, signals=input.signals)
    if primary.scope not in {"chapter_plan", "band_plan"}:
        return Decision(
            outcome="manual_review",
            reason=(
                f"{primary.kind} scope {primary.scope} is not eligible for "
                "commit_with_obligation"
            ),
            rule_id="commit_with_obligation_wrong_scope",
            missing_evidence=["eligible_scope"],
            routed_from="AutoDecisionEngine",
            sub_action={"issue_kind": primary.kind, "scope": primary.scope},
        )
    if input.budget is not None and input.budget.over_budget:
        return Decision(
            outcome="system_block",
            reason="obligation budget exceeded",
            rule_id="commit_with_obligation_over_budget",
            missing_evidence=[],
            routed_from="AutoDecisionEngine",
            sub_action={
                "issue_kind": primary.kind,
                "scope": primary.scope,
                "budget_reasons": list(input.budget.reasons),
            },
        )
    patch_count = (
        input.plan_layer_health.active_chapter_patch_count
        if primary.scope == "chapter_plan"
        else input.plan_layer_health.active_band_patch_count
    )
    if int(patch_count or 0) <= 0:
        return Decision(
            outcome="manual_review",
            reason=f"missing plan patch for {primary.scope}",
            rule_id="commit_with_obligation_missing_patch",
            missing_evidence=["plan_patch"],
            routed_from="AutoDecisionEngine",
            sub_action={"issue_kind": primary.kind, "scope": primary.scope},
        )
    return Decision(
        outcome="commit_with_obligation",
        reason=f"{primary.kind} can commit with {primary.scope} obligation",
        rule_id="commit_with_obligation_eligible",
        missing_evidence=[],
        routed_from="AutoDecisionEngine",
        sub_action={"issue_kind": primary.kind, "scope": primary.scope},
    )
