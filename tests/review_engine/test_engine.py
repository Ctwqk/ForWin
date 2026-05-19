from __future__ import annotations

from forwin.protocol.review import ReviewVerdict
from forwin.review_engine.engine import AutoDecisionEngine
from forwin.review_engine.types import Decision, DecisionInput, DecisionRule, PlanLayerHealth


def _input() -> DecisionInput:
    return DecisionInput(
        project_id="project-1",
        chapter_number=1,
        review=ReviewVerdict(verdict="warn"),
        signals=[],
        open_obligations=[],
        operation_mode="copilot",
        attempts_completed=0,
        prior_scope_history=[],
        budget=None,
        target_total_chapters=10,
        plan_layer_health=PlanLayerHealth(),
    )


def test_engine_returns_first_matching_rule() -> None:
    rules = [
        DecisionRule(
            rule_id="first",
            source_dispatcher="fixture",
            priority=10,
            matches=lambda _: True,
            decide=lambda _: Decision("manual_review", "first", "first", [], "fixture", {}),
        ),
        DecisionRule(
            rule_id="second",
            source_dispatcher="fixture",
            priority=20,
            matches=lambda _: True,
            decide=lambda _: Decision("system_block", "second", "second", [], "fixture", {}),
        ),
    ]

    decision = AutoDecisionEngine(rules).decide(_input())

    assert decision.rule_id == "first"
