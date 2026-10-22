from __future__ import annotations

import pytest

from forwin.canon.eligibility import candidate_ineligibility_reason, extraction_error_override
from forwin.protocol.review import ContinuityIssue, FinalResidualDecision, RepairVerification, ReviewVerdict


MISSING = "本章缺少章节 delta 抽取，无法核验世界一致性。"


def _issue(rule_name: str, *, description: str = "", issue_type: str = "world_consistency",
           severity: str = "error") -> ContinuityIssue:
    return ContinuityIssue(
        rule_name=rule_name, severity=severity, description=description or rule_name,
        issue_type=issue_type, reviewer="world_consistency",
    )


def _unfixed(item: str) -> ContinuityIssue:
    return _issue("repair_unfixed", description=f"repair 未真正修复：{item}", issue_type="repair_verification")


def _verdict(*issues: ContinuityIssue, unfixed: tuple[str, ...] = (), broken: tuple[str, ...] = ()) -> ReviewVerdict:
    """A review the repair loop gave up on, as the review gate stores it."""
    return ReviewVerdict(
        verdict="fail",
        issues=list(issues),
        residual_review_issues=list(issues),
        repair_exhausted=True,
        final_residual_decision=FinalResidualDecision(decision="manual_review_required"),
        repair_verification=RepairVerification(
            fixed_all_must_fix=False, unfixed=list(unfixed), broken_preserve_constraints=list(broken),
        ),
    )


@pytest.mark.parametrize("verdict,codes", [
    (_verdict(_issue("chapter_delta_missing", description=MISSING)), ("chapter_delta_missing",)),
    (
        _verdict(
            _issue("chapter_delta_missing", description=MISSING), _unfixed(MISSING),
            _issue("payoff_miss", severity="warning", issue_type="payoff_miss"),
            unfixed=(MISSING, f"{MISSING} 修复要求：重写正文后会重新抽取章节 delta。"),
        ),
        ("chapter_delta_missing",),
    ),
    (_verdict(_issue("chapter_delta_unverified")), ("chapter_delta_unverified",)),
])
def test_a_review_that_only_lacks_a_usable_extraction_can_be_accepted(verdict, codes) -> None:
    assert candidate_ineligibility_reason(verdict)

    override = extraction_error_override(verdict)

    assert override is not None and override.codes == codes
    accepted = override.review
    assert candidate_ineligibility_reason(accepted) == ""
    assert accepted.verdict == "warn"
    assert [issue.severity for issue in accepted.issues] == ["warning"] * len(accepted.issues)


@pytest.mark.parametrize("verdict", [
    # Another error in the review.
    _verdict(_issue("chapter_delta_missing", description=MISSING), _issue("lint_error", issue_type="lint")),
    # The repair left something else unfixed.
    _verdict(_issue("chapter_delta_missing", description=MISSING), _unfixed("角色名漂移"), unfixed=("角色名漂移",)),
    # The repair broke what it had to preserve.
    _verdict(
        _issue("chapter_delta_missing", description=MISSING),
        _issue("repair_preserve_breach", issue_type="repair_verification"),
        broken=("第一章",),
    ),
    # Nothing about the extraction.
    _verdict(_issue("movement_too_fast")),
    ReviewVerdict(verdict="pass"),
])
def test_any_other_error_keeps_the_rejection(verdict) -> None:
    assert extraction_error_override(verdict) is None
