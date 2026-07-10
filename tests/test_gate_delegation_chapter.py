from __future__ import annotations

from forwin.generation.pipeline_core.chapter_review_gate import (
    evaluate_candidate_gate,
)
from forwin.protocol.review import ContinuityIssue, ReviewVerdict
from forwin.runtime.policy import RuntimePolicy


class SpyDelegate:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self):
        self.calls += 1
        raise AssertionError("ineligible candidates must not reach gate delegation")


def test_fail_verdict_never_reaches_gate_delegation() -> None:
    delegate = SpyDelegate()
    policy = RuntimePolicy.for_profile("standard").with_user_settings(
        gate_delegate="spark"
    )
    verdict = ReviewVerdict(
        verdict="fail",
        issues=[
            ContinuityIssue(
                rule_name="canon_conflict",
                reviewer="canon",
                severity="error",
                description="hard canon conflict",
            )
        ],
        recommended_action="manual_review",
    )

    outcome = evaluate_candidate_gate(
        policy=policy,
        verdict=verdict,
        eligible=False,
        delegate=delegate,
    )

    assert outcome.pause_required is True
    assert outcome.should_apply_canon is False
    assert delegate.calls == 0


def test_fail_verdict_cannot_be_made_eligible_by_caller() -> None:
    delegate = SpyDelegate()
    policy = RuntimePolicy.for_profile("standard").with_user_settings(
        gate_delegate="spark"
    )
    verdict = ReviewVerdict(verdict="fail", issues=[])

    outcome = evaluate_candidate_gate(
        policy=policy,
        verdict=verdict,
        eligible=True,
        delegate=delegate,
    )

    assert outcome.pause_required is True
    assert outcome.should_apply_canon is False
    assert delegate.calls == 0
