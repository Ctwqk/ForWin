from __future__ import annotations

from forwin.protocol.review import (
    FinalResidualDecision,
    RepairVerification,
    ReviewVerdict,
)

from ..types import Decision, DecisionInput, DecisionRule


_HARD_ISSUE_TYPES = {
    "continuity",
    "subworld_admission",
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


def is_force_acceptable_nonblocking_issue(issue) -> bool:
    rule_name = str(getattr(issue, "rule_name", "") or "")
    issue_type = str(getattr(issue, "issue_type", "") or "")
    issue_group = str(getattr(issue, "issue_group", "") or "")
    return (
        rule_name == "sub_world_unknown_named_entity"
        and issue_type == "subworld_admission"
        and issue_group == "director_imbalance"
        and not bool(getattr(issue, "blocking", False))
        and not str(getattr(issue, "blocking_origin", "") or "").strip()
    )


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
        if not verification.fixed_all_must_fix or not verification.preserved_all_must_preserve:
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
                and not is_force_acceptable_nonblocking_issue(issue)
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
                and not is_force_acceptable_nonblocking_issue(issue)
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
