from __future__ import annotations

from forwin.protocol.review import ReviewVerdict
from forwin.review_engine.types import Decision, DecisionInput, PlanLayerHealth


def test_decision_input_and_decision_are_serializable() -> None:
    input_payload = DecisionInput(
        project_id="project-1",
        chapter_number=3,
        review=ReviewVerdict(verdict="pass"),
        signals=[],
        open_obligations=[],
        operation_mode="copilot",
        attempts_completed=0,
        prior_scope_history=[],
        budget=None,
        target_total_chapters=30,
        plan_layer_health=PlanLayerHealth(),
    )
    decision = Decision(
        outcome="manual_review",
        reason="shadow fixture",
        rule_id="fixture_manual_review",
        missing_evidence=[],
        routed_from="fixture",
        sub_action={},
    )

    assert input_payload.project_id == "project-1"
    assert decision.rule_id == "fixture_manual_review"
