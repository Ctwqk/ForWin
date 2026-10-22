from __future__ import annotations

from dataclasses import dataclass

from forwin.book_state.extraction.evidence import UNUSABLE_EXTRACTION_CODES
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


@dataclass(frozen=True)
class ExtractionErrorOverride:
    """A person's accept of a review that failed only for want of a usable extraction."""

    codes: tuple[str, ...]
    # The review without the errors the accept overrides; canon judges the rest.
    review: ReviewVerdict


def extraction_error_override(verdict: ReviewVerdict) -> ExtractionErrorOverride | None:
    """The override for a failed review whose every error is about the missing extraction.

    Canon extracts the chapter again, so an accept may pass those errors. A repair_unfixed
    error counts when everything the repair left unfixed is one of them; any other error,
    and any broken must_preserve, keeps the rejection.
    """
    errors = [issue for issue in verdict.issues if issue.severity == "error"]
    unusable = [issue for issue in errors if issue.rule_name in UNUSABLE_EXTRACTION_CODES]
    verification = verdict.repair_verification
    if not unusable or (verification is not None and verification.broken_preserve_constraints):
        return None
    descriptions = tuple(issue.description.strip() for issue in unusable if issue.description.strip())
    unfixed = list(verification.unfixed) if verification is not None else []
    # A must_fix item is the issue description followed by its fix.
    if not all(str(item).strip().startswith(descriptions) for item in unfixed):
        return None
    overridden = [
        issue for issue in errors
        if issue in unusable or (issue.rule_name == "repair_unfixed" and verification is not None)
    ]
    if len(overridden) != len(errors):
        return None
    remaining = [issue for issue in verdict.issues if issue not in overridden]
    return ExtractionErrorOverride(
        codes=tuple(dict.fromkeys(issue.rule_name for issue in unusable)),
        review=verdict.model_copy(update={
            "verdict": "warn",
            "issues": remaining,
            "residual_review_issues": [
                issue for issue in verdict.residual_review_issues if issue not in overridden
            ],
            "final_residual_decision": None,
            "repair_verification": None,
        }),
    )


__all__ = [
    "ExtractionErrorOverride",
    "candidate_ineligibility_reason",
    "extraction_error_override",
]
