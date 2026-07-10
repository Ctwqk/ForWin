from __future__ import annotations

from types import SimpleNamespace

from forwin.governance import DecisionEventType, ensure_decision_event_type
from forwin.orchestrator_loop_core.repair_budget import evaluate_repair_body_budget
from forwin.review.repair.service import _default_repair_instruction
from forwin.protocol.review import ContinuityIssue, ReviewVerdict


def test_default_repair_instruction_includes_word_budget_guardrails() -> None:
    instruction = _default_repair_instruction(
        repair_scope="draft",
        context=SimpleNamespace(
            chapter_plan_title="第八十九章",
            chapter_plan_one_line="旧港门禁倒计时。",
            chapter_goals=["打开侧门", "保护密钥"],
            target_chapter_chars=4200,
            min_chapter_chars=3600,
            max_chapter_chars=4800,
        ),
        review=ReviewVerdict(
            verdict="fail",
            issues=[
                ContinuityIssue(
                    rule_name="char_count_high",
                    issue_type="lint",
                    severity="warning",
                    description="章节过长。",
                ),
                ContinuityIssue(
                    rule_name="sub_world_unknown_named_entity",
                    issue_type="subworld_admission",
                    severity="error",
                    description="命名角色未准入。",
                ),
            ],
        ),
    )

    assert instruction.design_patch["target_chapter_chars"] == 4200
    assert instruction.design_patch["min_chapter_chars"] == 3600
    assert instruction.design_patch["max_chapter_chars"] == 4800
    assert instruction.design_patch["repair_max_growth_ratio"] == 1.08
    assert instruction.design_patch["must_replace_not_append"] is True


def test_repair_body_budget_overrun_returns_registered_decision_event() -> None:
    decision = evaluate_repair_body_budget(
        source_char_count=4000,
        result_char_count=5200,
        design_patch={
            "target_chapter_chars": 4200,
            "max_chapter_chars": 4800,
            "repair_max_growth_ratio": 1.08,
            "must_replace_not_append": True,
        },
    )

    assert decision is not None
    assert decision.event_type == DecisionEventType.REPAIR_BODY_OVER_BUDGET
    assert ensure_decision_event_type(decision.event_type) == DecisionEventType.REPAIR_BODY_OVER_BUDGET
    assert decision.reason == "repair-body-over-budget"
    assert decision.payload["source_char_count"] == 4000
    assert decision.payload["result_char_count"] == 5200
    assert decision.payload["max_chapter_chars"] == 4800
    assert decision.payload["repair_max_growth_ratio"] == 1.08
    assert decision.payload["over_max_chapter_chars"] is True
    assert decision.payload["over_growth_ratio"] is True
