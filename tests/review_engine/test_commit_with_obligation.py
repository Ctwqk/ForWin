from __future__ import annotations

from forwin.narrative_obligations.budget import ObligationBudgetResult
from forwin.protocol.review import ContinuityIssue, ReviewVerdict
from forwin.review_engine.rules.commit_with_obligation import (
    decide_commit_with_obligation,
)
from forwin.review_engine.types import DecisionInput, PlanLayerHealth


def _input(
    issue_kind: str,
    *,
    scope_patch_count: int = 1,
    over_budget: bool = False,
) -> DecisionInput:
    return DecisionInput(
        project_id="project-1",
        chapter_number=6,
        review=ReviewVerdict(
            verdict="warn",
            issues=[
                ContinuityIssue(
                    rule_name=issue_kind,
                    issue_type=issue_kind,
                    severity="warning",
                    description=issue_kind,
                    evidence_refs=[f"issue:{issue_kind}"],
                )
            ],
        ),
        signals=[],
        open_obligations=[],
        operation_mode="blackbox",
        attempts_completed=0,
        prior_scope_history=[],
        budget=ObligationBudgetResult(
            allowed=not over_budget,
            over_budget=over_budget,
            reasons=["budget"] if over_budget else [],
        ),
        target_total_chapters=30,
        plan_layer_health=PlanLayerHealth(active_chapter_patch_count=scope_patch_count),
    )


def test_chapter_plan_issue_with_patch_and_budget_commits_with_obligation() -> None:
    decision = decide_commit_with_obligation(_input("motivation_gap"))

    assert decision.outcome == "commit_with_obligation"
    assert decision.rule_id == "commit_with_obligation_eligible"
    assert decision.sub_action["scope"] == "chapter_plan"


def test_missing_plan_patch_routes_to_manual_review() -> None:
    decision = decide_commit_with_obligation(
        _input("motivation_gap", scope_patch_count=0)
    )

    assert decision.outcome == "manual_review"
    assert decision.rule_id == "commit_with_obligation_missing_patch"


def test_budget_overage_routes_to_system_block() -> None:
    decision = decide_commit_with_obligation(_input("motivation_gap", over_budget=True))

    assert decision.outcome == "system_block"
    assert decision.rule_id == "commit_with_obligation_over_budget"
