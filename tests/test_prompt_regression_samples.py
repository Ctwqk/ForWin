from __future__ import annotations

from forwin.protocol.context import ChapterContextPack
from forwin.writer.prompts import _canon_quality_context_section


def test_prompt_constraints_shrink_when_plan_patches_cover_form_signals() -> None:
    base_quality = {
        "countdown_constraints": [
            {
                "countdown_key": "倒计时甲",
                "label": "倒计时甲",
                "latest_remaining_minutes": 10,
                "latest_chapter": 12,
                "raw_mention": "倒计时甲剩余十分钟。",
            },
            {
                "countdown_key": "倒计时乙",
                "label": "倒计时乙",
                "latest_remaining_minutes": 5,
                "latest_chapter": 12,
                "raw_mention": "倒计时乙剩余五分钟。",
            },
        ],
        "active_narrative_obligations": [
            {
                "id": "义务-1",
                "type": "reader_promise_payoff",
                "priority": "P1",
                "summary": "偿还事件-1的公开承诺。",
                "deadline_chapter": 13,
                "payoff_test": "第13章必须写出事件-1如何公开。",
                "must_resolve_now": True,
            }
        ],
        "open_signals": [
            {
                "signal_id": "信号-1",
                "signal_type": "form_open_signal_persisting",
                "severity": "error",
                "chapter_number": 12,
                "subject_key": "信号-1",
                "description": "信号-1仍未解决。",
            }
        ],
    }
    before = _canon_quality_context_section(_context(base_quality)) or ""
    after_quality = {
        **base_quality,
        "suppressed_prompt_constraint_keys": ["countdown:倒计时甲", "obligation:义务-1", "signal:信号-1"],
    }
    after_context = _context(after_quality)
    after = _canon_quality_context_section(after_context) or ""

    assert len(after) <= int(len(before) * 0.7)
    assert "倒计时甲" not in after
    assert "倒计时乙" in after
    assert after_context.canon_quality_context["form_prompt_constraints_suppressed"] == 3
    assert after_context.canon_quality_context["form_prompt_constraints_remaining"] == 1


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
