from __future__ import annotations

from forwin.review_engine.issue_taxonomy import classify_primary_issue
from forwin.review_engine.types import Decision, DecisionInput


def decide_structural_patch(
    *,
    input: DecisionInput,
    arc_patcher_enabled: bool,
    book_patcher_enabled: bool,
) -> Decision:
    primary = classify_primary_issue(review=input.review, signals=input.signals)
    if primary.scope == "arc_plan":
        if not arc_patcher_enabled:
            return Decision(
                outcome="manual_review",
                reason="arc patcher disabled",
                rule_id="arc_patcher_disabled",
                missing_evidence=["arc_patcher_enabled"],
                routed_from="AutoDecisionEngine",
                sub_action={"issue_kind": primary.kind, "scope": primary.scope},
            )
        return Decision(
            outcome="arc_patch",
            reason=f"{primary.kind} requires arc patch",
            rule_id="arc_patch_enabled",
            missing_evidence=[],
            routed_from="AutoDecisionEngine",
            sub_action={"patch_type": "arc_defer_acceptance", "issue_kind": primary.kind},
        )
    if primary.scope == "book_plan":
        if not book_patcher_enabled:
            return Decision(
                outcome="manual_review",
                reason="book patcher disabled",
                rule_id="book_patcher_disabled",
                missing_evidence=["book_patcher_enabled"],
                routed_from="AutoDecisionEngine",
                sub_action={"issue_kind": primary.kind, "scope": primary.scope},
            )
        return Decision(
            outcome="book_patch",
            reason=f"{primary.kind} requires book patch",
            rule_id="book_patch_enabled",
            missing_evidence=[],
            routed_from="AutoDecisionEngine",
            sub_action={"patch_type": "book_defer_acceptance", "issue_kind": primary.kind},
        )
    return Decision(
        outcome="manual_review",
        reason="not a structural patch issue",
        rule_id="not_structural_patch",
        missing_evidence=["structural_issue"],
        routed_from="AutoDecisionEngine",
        sub_action={"issue_kind": primary.kind, "scope": primary.scope},
    )
