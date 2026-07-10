from __future__ import annotations

from forwin.protocol.review import ContinuityIssue, RepairVerification, ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.orchestrator_loop_core.review_autofix import (
    _review_current_output,
    normalize_nonblocking_review_verdict,
)
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


def test_nonblocking_legacy_subworld_residual_can_force_accept() -> None:
    review = ReviewVerdict(
        verdict="fail",
        issues=[
            ContinuityIssue(
                rule_name="sub_world_unknown_named_entity",
                issue_type="subworld_admission",
                severity="error",
                description="命名角色「周洛」未在当前 chapter 的 subworld 准入名单中。",
                entity_names=["周洛"],
                issue_group="director_imbalance",
                blocking=False,
            )
        ],
        repair_verification=RepairVerification(
            fixed_all_must_fix=True,
            preserved_all_must_preserve=True,
            verifier_mode="rule_only",
        ),
    )

    decision = AutoDecisionEngine(build_final_acceptance_rules()).decide(
        _decision_input(review)
    )

    assert decision.outcome == "accept"
    assert decision.reason == "soft-quality-failure-only"
    assert decision.sub_action["final_gate_decision"] == "force_accept"
    assert decision.sub_action["forceable"] is True


def test_nonblocking_legacy_subworld_review_downgrades_before_manual_gate() -> None:
    review = ReviewVerdict(
        verdict="fail",
        issues=[
            ContinuityIssue(
                rule_name="sub_world_unknown_named_entity",
                issue_type="subworld_admission",
                severity="error",
                description="命名角色「白临川」未在当前 chapter 的 subworld 准入名单中。",
                entity_names=["白临川"],
                issue_group="director_imbalance",
                blocking=False,
            ),
            ContinuityIssue(
                rule_name="reward_delivery_thin",
                issue_type="payoff_miss",
                severity="warning",
                description="本章计划奖励与实际回报对齐不足。",
            ),
        ],
    )

    normalized = normalize_nonblocking_review_verdict(review)

    assert normalized.verdict == "warn"
    assert [issue.severity for issue in normalized.issues] == ["warning", "warning"]
    assert normalized.issues[0].original_result["normalized_from_severity"] == "error"


def test_review_current_output_normalizes_nonblocking_legacy_subworld_failure() -> None:
    raw_review = ReviewVerdict(
        verdict="fail",
        issues=[
            ContinuityIssue(
                rule_name="sub_world_unknown_named_entity",
                issue_type="subworld_admission",
                severity="error",
                description="命名角色「白临川」未在当前 chapter 的 subworld 准入名单中。",
                entity_names=["白临川"],
                issue_group="director_imbalance",
                blocking=False,
            )
        ],
    )

    class FakeOrchestrator:
        review_hub = type(
            "ReviewHub",
            (),
            {"review": staticmethod(lambda **_kwargs: raw_review)},
        )()

        def _select_skill_layers(self, **_kwargs):
            return []

        def _call_with_compatible_kwargs(self, fn, **kwargs):
            return fn(**kwargs)

    normalized = _review_current_output(
        FakeOrchestrator(),
        repo=object(),
        checker=object(),
        project_id="project-1",
        context=object(),
        writer_output=WriterOutput(
            chapter_number=1,
            title="第1章",
            body="陆明发现白临川留下的线索。",
            end_of_chapter_summary="陆明得到线索。",
        ),
    )

    assert normalized.verdict == "warn"
    assert normalized.issues[0].severity == "warning"


def test_soft_residual_issue_can_force_accept_after_successful_verification() -> None:
    decision = AutoDecisionEngine(build_final_acceptance_rules()).decide(
        _decision_input(_verified_review("director_imbalance"))
    )

    assert decision.outcome == "accept"
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
