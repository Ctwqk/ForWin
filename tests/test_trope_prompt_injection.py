from __future__ import annotations

import pytest

from forwin.protocol.context import ChapterContextPack
from forwin.protocol.experience import ChapterExperiencePlan
from forwin.protocol.trope_library import TropeTemplate
from forwin.writer.prompt_core.sections import _experience_overlay_section


def _context_with_templates(template_ids: list[str]) -> ChapterContextPack:
    return ChapterContextPack(
        project_id="p1",
        project_title="灰城遗档",
        premise="主角：陆明，旧城档案修复师。",
        genre="悬疑科幻",
        setting_summary="核心系统记忆系统维持公共档案秩序。",
        chapter_number=4,
        chapter_plan_title="权限跃迁",
        chapter_plan_one_line="陆明夺回被封锁的权限。",
        chapter_goals=["让陆明完成一次可见升级"],
        chapter_experience_plan=ChapterExperiencePlan(
            planned_reward_tags=["power"],
            selected_template_ids=template_ids,
        ),
    )


def test_experience_overlay_injects_selected_trope_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = TropeTemplate(
        template_id="power-level-up",
        display_name="升级",
        category="power",
        desire_setup="先写清陆明当前权限不足，必须靠这次升级打开生路。",
        resistance="反派和环境同时压迫，让读者感到升级不是自动发生。",
        payoff="升级必须有肉眼可见的具体变化，并立刻改变战局。",
        aftermath="写出敌人、同伴、陆明自己的三层反应，再抛出下一层门槛。",
        anti_patterns=["只报数值不写变化", "升级无代价", "旁观者没有反应", "结尾无钩子"],
    )
    monkeypatch.setattr(
        "forwin.protocol.trope_library.trope_template_index",
        lambda: {"power-level-up": template},
    )

    section = _experience_overlay_section(_context_with_templates(["power-level-up"]))

    assert section is not None
    assert "本章爽点指令" in section
    assert "升级" in section
    assert "欲望建立：先写清陆明当前权限不足" in section
    assert "阻力加压：反派和环境同时压迫" in section
    assert "爽点兑现：升级必须有肉眼可见的具体变化" in section
    assert "余波钩子：写出敌人、同伴、陆明自己的三层反应" in section
    assert "禁止：只报数值不写变化；升级无代价；旁观者没有反应" in section
    assert "结尾无钩子" not in section


def test_experience_overlay_skips_unknown_selected_trope_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("forwin.protocol.trope_library.trope_template_index", lambda: {})

    section = _experience_overlay_section(_context_with_templates(["unknown-template"]))

    assert section is not None
    assert "本章爽点指令" not in section
    assert "选用模板：unknown-template" not in section
