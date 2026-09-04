from __future__ import annotations

from forwin.protocol.review import ReviewVerdict


def candidate_ineligibility_reason(verdict: ReviewVerdict) -> str:
    if verdict.verdict not in {"pass", "warn"}:
        return f"review verdict {verdict.verdict} is not Canon-eligible"
    final_residual = verdict.final_residual_decision
    if final_residual is not None:
        if final_residual.decision != "force_accept":
            return f"final residual decision {final_residual.decision} blocks Canon"
        if final_residual.canon_risk == "high":
            return "high-risk final residual blocks Canon"
    verification = verdict.repair_verification
    if verification is not None and (
        verification.fixed_all_must_fix is False
        or verification.preserved_all_must_preserve is False
    ):
        return "repair verification is incomplete"
    residuals = verdict.residual_review_issues or []
    if any(issue.blocking or issue.severity == "error" for issue in residuals):
        return "hard residual review issues block Canon"
    return ""


__all__ = ["candidate_ineligibility_reason"]
