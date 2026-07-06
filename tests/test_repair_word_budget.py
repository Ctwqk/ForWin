from __future__ import annotations

from types import SimpleNamespace

from forwin.orchestrator_loop_core.repair_loop import _default_repair_instruction
from forwin.protocol.review import ContinuityIssue, ReviewVerdict


def test_default_repair_instruction_includes_word_budget_guardrails() -> None:
    instruction = _default_repair_instruction(
        SimpleNamespace(config=SimpleNamespace()),
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
