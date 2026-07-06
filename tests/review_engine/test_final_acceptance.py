from __future__ import annotations

from forwin.protocol.review import ContinuityIssue, RepairVerification, ReviewVerdict
from forwin.review_engine.engine import AutoDecisionEngine
from forwin.review_engine.rules.final_acceptance import build_final_acceptance_rules
from forwin.review_engine.types import DecisionInput, PlanLayerHealth


def _decision_input(review: ReviewVerdict) -> DecisionInput:
    return DecisionInput(
        project_id="project-1",
        chapter_number=89,
        review=review,
        signals=[],
        open_obligations=[],
        operation_mode="blackbox",
        attempts_completed=2,
        prior_scope_history=["draft", "draft"],
        budget=None,
        target_total_chapters=100,
        plan_layer_health=PlanLayerHealth(),
    )


def _verified_review(issue_type: str, *, severity: str = "error") -> ReviewVerdict:
    return ReviewVerdict(
        verdict="fail",
        issues=[
            ContinuityIssue(
                rule_name=issue_type,
                issue_type=issue_type,
                severity=severity,
                description=f"{issue_type} remains",
            )
        ],
        repair_verification=RepairVerification(
            fixed_all_must_fix=True,
            preserved_all_must_preserve=True,
            verifier_mode="rule_only",
        ),
    )


def test_missing_repair_verification_returns_structured_final_gate_decision() -> None:
    review = ReviewVerdict(
        verdict="fail",
        issues=[
            ContinuityIssue(
                rule_name="sub_world_unknown_named_entity",
                issue_type="subworld_admission",
                severity="error",
                description="命名角色未准入。",
            )
        ],
    )

    decision = AutoDecisionEngine(build_final_acceptance_rules()).decide(_decision_input(review))

    assert decision.rule_id == "final_acceptance_gate"
    assert decision.outcome == "manual_review"
    assert decision.reason == "missing-repair-verification"
    assert decision.sub_action["final_gate_decision"] == "manual_review_required"
    assert decision.sub_action["forceable"] is False


def test_hard_residual_issue_requires_manual_review() -> None:
    decision = AutoDecisionEngine(build_final_acceptance_rules()).decide(
        _decision_input(_verified_review("subworld_admission"))
    )

    assert decision.outcome == "manual_review"
    assert decision.reason == "hard-residual-issue:subworld_admission"
    assert decision.sub_action["forceable"] is False


def test_soft_residual_issue_can_force_accept_after_successful_verification() -> None:
    decision = AutoDecisionEngine(build_final_acceptance_rules()).decide(
        _decision_input(_verified_review("director_imbalance"))
    )

    assert decision.outcome == "auto_approve"
    assert decision.reason == "soft-quality-failure-only"
    assert decision.sub_action["final_gate_decision"] == "force_accept"
    assert decision.sub_action["forceable"] is True


def test_unknown_residual_issue_requires_manual_review() -> None:
    decision = AutoDecisionEngine(build_final_acceptance_rules()).decide(
        _decision_input(_verified_review("unexpected_reviewer_issue"))
    )

    assert decision.outcome == "manual_review"
    assert decision.reason == "unsupported-residual-issue:unexpected_reviewer_issue"
    assert decision.sub_action["forceable"] is False
