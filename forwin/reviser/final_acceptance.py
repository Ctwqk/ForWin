from __future__ import annotations

from forwin.protocol.review import FinalGateDecision, RepairVerification, ReviewVerdict

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


class FinalAcceptanceGate:
    def evaluate(
        self,
        *,
        operation_mode: str,
        review: ReviewVerdict,
        verification: RepairVerification | None,
    ) -> FinalGateDecision:
        residual_issues = [
            str(issue.description or issue.rule_name or "").strip()
            for issue in review.issues
            if str(issue.severity or "") == "error"
        ]
        if str(operation_mode or "") != "blackbox":
            return FinalGateDecision(
                decision="manual_review_required",
                forceable=False,
                reason="non-blackbox-mode",
                canon_risk="high",
                residual_issues=residual_issues,
                requires_human=True,
            )

        if verification is None:
            return FinalGateDecision(
                decision="manual_review_required",
                forceable=False,
                reason="missing-repair-verification",
                canon_risk="high",
                residual_issues=residual_issues,
                requires_human=True,
            )
        if not verification.fixed_all_must_fix or not verification.preserved_all_must_preserve:
            return FinalGateDecision(
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
            return FinalGateDecision(
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
            return FinalGateDecision(
                decision="manual_review_required",
                forceable=False,
                reason=f"unsupported-residual-issue:{unknown_issue.issue_type or unknown_issue.rule_name}",
                canon_risk="high",
                residual_issues=residual_issues,
                requires_human=True,
            )

        return FinalGateDecision(
            decision="force_accept",
            forceable=True,
            reason="soft-quality-failure-only",
            canon_risk="low",
            residual_issues=residual_issues,
            requires_human=False,
        )
