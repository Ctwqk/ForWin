from __future__ import annotations

from forwin.protocol.context import ChapterContextPack
from forwin.writer.prompts import _canon_quality_context_section


def test_prompt_constraints_shrink_when_plan_patches_cover_form_signals() -> None:
    base_quality = {
        "countdown_constraints": [
            {
                "countdown_key": f"倒计时{i}",
                "label": f"倒计时{i}",
                "latest_remaining_minutes": 10 + i,
                "latest_chapter": 12,
                "raw_mention": f"倒计时{i}剩余{10 + i}分钟。",
            }
            for i in range(1, 6)
        ],
        "active_narrative_obligations": [
            {
                "id": f"义务-{i}",
                "type": "reader_promise_payoff",
                "priority": "P1",
                "summary": f"偿还事件-{i}的公开承诺。",
                "deadline_chapter": 13,
                "payoff_test": f"第13章必须写出事件-{i}如何公开。",
                "must_resolve_now": True,
            }
            for i in range(1, 4)
        ],
        "open_signals": [
            {
                "signal_id": f"信号-{i}",
                "signal_type": "form_open_signal_persisting",
                "severity": "error",
                "chapter_number": 12,
                "subject_key": f"信号-{i}",
                "description": f"信号-{i}仍未解决。",
            }
            for i in range(1, 3)
        ],
    }
    before = _canon_quality_context_section(_context(base_quality)) or ""
    after_quality = {
        **base_quality,
        "suppressed_prompt_constraint_keys": [
            "countdown:倒计时1",
            "countdown:倒计时2",
            "obligation:义务-1",
            "signal:信号-1",
        ],
    }
    after_context = _context(after_quality)
    after = _canon_quality_context_section(after_context) or ""
    actual_reduction = 1 - (len(after) / len(before))

    assert actual_reduction >= 0.30
    assert "倒计时1" not in after
    assert "倒计时2" not in after
    assert "倒计时3" in after
    assert "义务-1" not in after
    assert "义务-2" in after
    assert "信号-1" not in after
    assert "信号-2" in after
    assert after_context.canon_quality_context["form_prompt_constraints_suppressed"] == 4
    assert after_context.canon_quality_context["form_prompt_constraints_remaining"] == 6


def _context(canon_quality_context: dict) -> ChapterContextPack:
    return ChapterContextPack(
        project_id="p1",
        project_title="灰城遗档",
        premise="主角：陆明，旧城档案修复师。",
        genre="悬疑科幻",
        setting_summary="核心系统维持公共档案秩序。",
        chapter_number=13,
        chapter_plan_title="计划补丁验证",
        chapter_plan_one_line="陆明执行已提升到计划中的正向任务。",
        chapter_goals=["推进事件-1"],
        canon_quality_context=canon_quality_context,
    )
