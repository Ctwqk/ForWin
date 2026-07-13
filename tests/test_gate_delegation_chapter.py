from __future__ import annotations

from pathlib import Path

import pytest

from forwin.generation.gate_delegation import GateResolution
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


class ApprovingDelegate:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> GateResolution:
        self.calls += 1
        return GateResolution(
            resolved=True,
            approved=True,
            decision="approve",
            delegate="spark",
        )


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


def test_ineligible_pass_candidate_never_reaches_gate_delegation() -> None:
    delegate = SpyDelegate()
    policy = RuntimePolicy.for_profile("standard").with_user_settings(
        gate_delegate="spark"
    )

    outcome = evaluate_candidate_gate(
        policy=policy,
        verdict=ReviewVerdict(verdict="pass", issues=[]),
        eligible=False,
        delegate=delegate,
    )

    assert outcome.pause_required is True
    assert outcome.should_apply_canon is False
    assert delegate.calls == 0


@pytest.mark.parametrize("verdict", ["pass", "warn"])
def test_spark_only_approves_canon_eligible_review_opportunities(verdict: str) -> None:
    delegate = ApprovingDelegate()
    policy = RuntimePolicy.for_profile("standard").with_user_settings(
        gate_delegate="spark"
    )

    outcome = evaluate_candidate_gate(
        policy=policy,
        verdict=ReviewVerdict(verdict=verdict, issues=[]),
        eligible=True,
        delegate=delegate,
    )

    assert delegate.calls == 1
    assert outcome.gate_approved is True
    assert outcome.pause_required is False
    assert outcome.should_apply_canon is True


def test_spark_approval_still_flows_through_canon_preparation_and_admission() -> None:
    source = Path("forwin/generation/pipeline_core/project_chapters.py").read_text()

    review_gate = source.index("review_gate = handle_chapter_review_gate(")
    pause_exit = source.index("if review_gate.pause_required:", review_gate)
    preparation = source.index("self.canon_preparation.prepare(", pause_exit)
    admission = source.index("self.canon_admission.commit_plan(", preparation)

    assert review_gate < pause_exit < preparation < admission
