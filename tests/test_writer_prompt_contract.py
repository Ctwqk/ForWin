from __future__ import annotations

from forwin.protocol.context import ChapterContextPack
from forwin.writer.prompts import build_single_chapter_draft_prompt


def test_writer_prompt_includes_protagonist_naming_contract() -> None:
    context = ChapterContextPack(
        project_id="p1",
        project_title="旧城遗档",
        premise="主角：林澈，旧城档案修复师。",
        genre="悬疑科幻",
        setting_summary="白塔记忆系统维持公共档案秩序。",
        chapter_number=1,
        chapter_plan_title="档案室的裂缝",
        chapter_plan_one_line="林澈发现家族档案被抹除。",
        chapter_goals=["林澈发现倒计时"],
    )

    prompt = build_single_chapter_draft_prompt(context)
    content = "\n".join(message["content"] for message in prompt)

    assert "主角姓名：林澈" in content
    assert "不要用“工作人员”" in content
    assert "第 1 章正文前 300 字内必须出现主角姓名" in content
