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


def test_writer_prompt_preserves_existing_character_identity_and_gender() -> None:
    context = ChapterContextPack(
        project_id="p1",
        project_title="旧城遗档",
        premise="主角：林澈，旧城档案修复师。",
        genre="悬疑科幻",
        setting_summary="白塔记忆系统维持公共档案秩序。",
        chapter_number=12,
        chapter_plan_title="倒计时：最后一日",
        chapter_plan_one_line="林澈必须关闭白塔。",
        chapter_goals=["关闭白塔系统"],
    )

    prompt = build_single_chapter_draft_prompt(context)
    content = "\n".join(message["content"] for message in prompt)

    assert "不得突然改变已登场命名人物的性别、代词、亲属关系或辈分" in content
    assert "不要把前文女性角色改写成叔叔、父亲、祖父、男人" in content


def test_writer_prompt_includes_canon_quality_countdown_constraints() -> None:
    context = ChapterContextPack(
        project_id="p1",
        project_title="旧城遗档",
        premise="主角：林澈，旧城档案修复师。",
        genre="悬疑科幻",
        setting_summary="白塔记忆系统维持公共档案秩序。",
        chapter_number=12,
        chapter_plan_title="倒计时：最后一日",
        chapter_plan_one_line="林澈必须关闭白塔。",
        chapter_goals=["关闭白塔系统"],
        canon_quality_context={
            "countdown_constraints": [
                {
                    "countdown_key": "archive_cleanup",
                    "label": "终端审计窗口",
                    "latest_remaining_minutes": 180,
                    "latest_chapter": 11,
                    "raw_mention": "三小时",
                }
            ]
        },
    )

    prompt = build_single_chapter_draft_prompt(context)
    content = "\n".join(message["content"] for message in prompt)

    assert "Canon 质量连续性约束" in content
    assert "终端审计窗口/archive_cleanup" in content
    assert "第11章已剩余 180 分钟" in content
    assert "必须小于等于 180 分钟" in content
    assert "终端审计/授权窗口硬性规则" in content
    assert "不得写成四小时、五小时、一天或任何更大的剩余时间" in content


def test_memory_reset_countdown_constraint_overrides_older_summaries() -> None:
    context = ChapterContextPack(
        project_id="p1",
        project_title="旧城遗档",
        premise="主角：林澈，旧城档案修复师。",
        genre="悬疑科幻",
        setting_summary="白塔记忆系统维持公共档案秩序。",
        chapter_number=12,
        chapter_plan_title="倒计时：最后一日",
        chapter_plan_one_line="林澈必须关闭白塔。",
        chapter_goals=["关闭白塔系统", "公开真相"],
        previous_chapter_summaries=[
            "第7章曾提到记忆重置周期还剩七天。",
            "第11章确认记忆重置周期只剩不到二十四小时。",
        ],
        canon_quality_context={
            "is_final_chapter": True,
            "countdown_constraints": [
                {
                    "countdown_key": "memory_reset",
                    "label": "记忆重置周期",
                    "latest_remaining_minutes": 1440,
                    "latest_chapter": 11,
                    "raw_mention": "不到二十四小时",
                }
            ],
        },
    )

    prompt = build_single_chapter_draft_prompt(context)
    content = "\n".join(message["content"] for message in prompt)

    assert "ledger 上限优先于前情摘要、章节计划和旧设定" in content
    assert "不得把记忆重置周期写回五天、七天、三十多天" in content
    assert "只能写小于等于 1440 分钟" in content


def test_writer_prompt_changes_hook_contract_for_final_chapter() -> None:
    context = ChapterContextPack(
        project_id="p1",
        project_title="旧城遗档",
        premise="主角：林澈，旧城档案修复师。",
        genre="悬疑科幻",
        setting_summary="白塔记忆系统维持公共档案秩序。",
        project_target_total_chapters=12,
        chapter_number=12,
        chapter_plan_title="倒计时：最后一日",
        chapter_plan_one_line="林澈必须关闭白塔。",
        chapter_goals=["关闭白塔系统", "公开真相"],
    )

    prompt = build_single_chapter_draft_prompt(context)
    content = "\n".join(message["content"] for message in prompt)

    assert "本章是全书终章或当前目标终章" in content
    assert "不要留下追兵、被困、钥匙损坏、准备公开、正要关闭" in content
    assert "本章结尾必须留下明确钩子" not in content


def test_final_writer_prompt_requires_executed_resolution_not_new_prerequisites() -> None:
    context = ChapterContextPack(
        project_id="p1",
        project_title="旧城遗档",
        premise="主角：林澈，旧城档案修复师。",
        genre="悬疑科幻",
        setting_summary="白塔记忆系统维持公共档案秩序。",
        chapter_number=12,
        chapter_plan_title="倒计时：最后一日",
        chapter_plan_one_line="林澈必须关闭白塔。",
        chapter_goals=["关闭白塔系统", "公开真相"],
        canon_quality_context={"is_final_chapter": True},
    )

    prompt = build_single_chapter_draft_prompt(context)
    content = "\n".join(message["content"] for message in prompt)

    assert "如果写到关闭方法、钥匙、坐标、芯片或锁孔" in content
    assert "必须在本章完成使用、关闭或公开" in content
    assert "不要新增需要下一章解决的三把钥匙、下一层入口、未知坐标或新倒计时" in content
    assert "不要把“去档案公会交最后一段记录”" in content
    assert "最后一段记录、剩余证据、最后一份档案" in content


def test_final_writer_prompt_forbids_unresolved_trapped_sacrifice() -> None:
    context = ChapterContextPack(
        project_id="p1",
        project_title="旧城遗档",
        premise="主角：林澈，旧城档案修复师。",
        genre="悬疑科幻",
        setting_summary="白塔记忆系统维持公共档案秩序。",
        chapter_number=12,
        chapter_plan_title="倒计时：最后一日",
        chapter_plan_one_line="林澈必须关闭白塔。",
        chapter_goals=["关闭白塔系统", "公开真相"],
        canon_quality_context={"is_final_chapter": True},
    )

    prompt = build_single_chapter_draft_prompt(context)
    content = "\n".join(message["content"] for message in prompt)

    assert "不要把“被困在机房/地下/白塔内”当作终章结局" in content
    assert "牺牲必须写成已完成的终局代价" in content
    assert "被救出、死亡/牺牲确认、或后日谈确认主线已结清" in content
