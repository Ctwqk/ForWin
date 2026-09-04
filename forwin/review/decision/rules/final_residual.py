from __future__ import annotations

from forwin.protocol.review import (
    FinalResidualDecision,
    RepairVerification,
    ReviewVerdict,
)

from ..types import Decision, DecisionInput, DecisionRule


_HARD_ISSUE_TYPES = {
    "continuity",
    "entity_admission_plan_conflict",
    "entity_admission_plan_invalid",
    "future_constraint",
    "future_resource_preservation",
    "intra_band_consistency",
    "next_band_compatibility",
}
_SOFT_ISSUE_TYPES = {
    "stall",
    "hook_failure",
    "payoff_miss",
    "immersion",
    "director_imbalance",
    "lint",
}


class FinalResidualPolicy:
    def evaluate(
        self,
        *,
        review: ReviewVerdict,
        verification: RepairVerification | None,
    ) -> FinalResidualDecision:
        residual_issues = [
            str(issue.description or issue.rule_name or "").strip()
            for issue in review.issues
            if str(issue.severity or "") == "error"
        ]
        if verification is None:
            return FinalResidualDecision(
                decision="manual_review_required",
                forceable=False,
                reason="missing-repair-verification",
                canon_risk="high",
                residual_issues=residual_issues,
                requires_human=True,
            )
        if (
            verification.fixed_all_must_fix is False
            or verification.preserved_all_must_preserve is False
        ):
            return FinalResidualDecision(
                decision="manual_review_required",
                forceable=False,
                reason="repair-verification-failed",
                canon_risk="high",
                residual_issues=residual_issues,
                requires_human=True,
            )

        hard_issue = next(
            (
                issue
                for issue in review.issues
                if str(issue.severity or "") == "error"
                and str(issue.issue_type or "") in _HARD_ISSUE_TYPES
            ),
            None,
        )
        if hard_issue is not None:
            return FinalResidualDecision(
                decision="manual_review_required",
                forceable=False,
                reason=f"hard-residual-issue:{hard_issue.issue_type or hard_issue.rule_name}",
                canon_risk="high",
                residual_issues=residual_issues,
                requires_human=True,
            )

        unknown_issue = next(
            (
                issue
                for issue in review.issues
                if str(issue.severity or "") == "error"
                and str(issue.issue_type or "") not in _SOFT_ISSUE_TYPES
            ),
            None,
        )
        if unknown_issue is not None:
            return FinalResidualDecision(
                decision="manual_review_required",
                forceable=False,
                reason=f"unsupported-residual-issue:{unknown_issue.issue_type or unknown_issue.rule_name}",
                canon_risk="high",
                residual_issues=residual_issues,
                requires_human=True,
            )

        return FinalResidualDecision(
            decision="force_accept",
            forceable=True,
            reason="soft-quality-failure-only",
            canon_risk="low",
            residual_issues=residual_issues,
            requires_human=False,
        )


def build_final_residual_rules(
    policy: FinalResidualPolicy | None = None,
) -> list[DecisionRule]:
    resolved_policy = policy or FinalResidualPolicy()
    return [
        DecisionRule(
            rule_id="final_residual_policy",
            source_dispatcher="FinalResidualPolicy",
            priority=300,
            matches=lambda input: True,
            decide=lambda input: _decision_from_final_residual(
                resolved_policy,
                input,
            ),
        )
    ]


def _decision_from_final_residual(
    policy: FinalResidualPolicy,
    input: DecisionInput,
) -> Decision:
    result = policy.evaluate(
        review=input.review,
        verification=input.review.repair_verification,
    )
    return Decision(
        outcome="accept" if result.decision == "force_accept" else "manual_review",
        reason=result.reason,
        rule_id="final_residual_policy",
        missing_evidence=[] if result.forceable else ["force_accept_conditions"],
        routed_from="FinalResidualPolicy",
        sub_action={
            "final_residual_decision": result.decision,
            "forceable": result.forceable,
            "canon_risk": result.canon_risk,
            "residual_issues": list(result.residual_issues),
            "requires_human": result.requires_human,
        },
    )
