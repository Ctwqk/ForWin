from __future__ import annotations

from forwin.planning.contracts import PlanTaskItem
from forwin.review.plan_checks import evaluate_task_contract


def test_natural_language_goal_without_machine_assertions_is_not_marked_unfulfilled() -> None:
    task = PlanTaskItem(
        task_type="plot_advance",
        description="植入第一个可验证异常锚点：签名时间戳偏差",
        source="derived_from_goals",
    )

    issues = evaluate_task_contract(
        [task],
        combined_text="三列差分都稳定显示签名早于基准四十七分钟。",
        reviewer="plan_contract",
        issue_type="plan_task_fulfillment",
        target_scope="chapter",
    )

    assert issues == []


def test_explicit_required_keywords_remain_enforced() -> None:
    task = PlanTaskItem(
        task_type="plot_advance",
        description="植入可验证异常锚点",
        required_keywords=["签名", "四十七分钟"],
        source="explicit",
    )

    issues = evaluate_task_contract(
        [task],
        combined_text="季澈检查了签名记录。",
        reviewer="plan_contract",
        issue_type="plan_task_fulfillment",
        target_scope="chapter",
    )

    assert len(issues) == 1
    assert issues[0].rule_name == "plan_task_unfulfilled"
