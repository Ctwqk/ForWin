from __future__ import annotations

from forwin.protocol.context import ChapterContextPack
from forwin.writer.prompts import build_single_chapter_draft_prompt


def test_writer_prompt_includes_protagonist_naming_contract() -> None:
    context = ChapterContextPack(
        project_id="p1",
        project_title="灰城遗档",
        premise="主角：陆明，旧城档案修复师。",
        genre="悬疑科幻",
        setting_summary="核心系统记忆系统维持公共档案秩序。",
        chapter_number=1,
        chapter_plan_title="档案室的裂缝",
        chapter_plan_one_line="陆明发现家族档案被抹除。",
        chapter_goals=["陆明发现倒计时"],
    )

    prompt = build_single_chapter_draft_prompt(context)
    content = "\n".join(message["content"] for message in prompt)

    assert "主角姓名：陆明" in content
    assert "不要用“工作人员”" in content
    assert "第 1 章正文前 300 字内必须出现主角姓名" in content
    assert "只保留一条线性时间线" in content
    assert "不要把多个备选版本的同一场景拼接进正文" in content


def test_writer_prompt_preserves_existing_character_identity_and_gender() -> None:
    context = ChapterContextPack(
        project_id="p1",
        project_title="灰城遗档",
        premise="主角：陆明，旧城档案修复师。",
        genre="悬疑科幻",
        setting_summary="核心系统记忆系统维持公共档案秩序。",
        chapter_number=12,
        chapter_plan_title="倒计时：最后一日",
        chapter_plan_one_line="陆明必须关闭核心系统。",
        chapter_goals=["关闭核心系统"],
    )

    prompt = build_single_chapter_draft_prompt(context)
    content = "\n".join(message["content"] for message in prompt)

    assert "不得突然改变已登场命名人物的性别、代词、亲属关系或辈分" in content
    assert "不要把前文女性角色改写成叔叔、父亲、祖父、男人" in content


def test_writer_prompt_includes_canon_quality_countdown_constraints() -> None:
    context = ChapterContextPack(
        project_id="p1",
        project_title="灰城遗档",
        premise="主角：陆明，旧城档案修复师。",
        genre="悬疑科幻",
        setting_summary="核心系统记忆系统维持公共档案秩序。",
        chapter_number=12,
        chapter_plan_title="倒计时：最后一日",
        chapter_plan_one_line="陆明必须关闭核心系统。",
        chapter_goals=["关闭核心系统"],
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
    assert "终端审计窗口：第11章已剩余 180 分钟" in content
    assert "archive_cleanup" not in content
    assert "第11章已剩余 180 分钟" in content
    assert "必须小于等于 180 分钟" in content
    assert "终端审计/授权窗口硬性规则" in content
    assert "不得写成四小时、五小时、一天或任何更大的剩余时间" in content


def test_writer_prompt_marks_closed_countdown_windows_as_not_reopenable() -> None:
    context = ChapterContextPack(
        project_id="p1",
        project_title="灰城遗档",
        premise="主角：陆明，旧城档案修复师。",
        genre="悬疑科幻",
        setting_summary="核心系统记忆系统维持公共档案秩序。",
        chapter_number=33,
        chapter_plan_title="陆氏守门人的真相",
        chapter_plan_one_line="陆明进入底层档案库。",
        chapter_goals=["进入底层档案库"],
        canon_quality_context={
            "countdown_constraints": [
                {
                    "countdown_key": "terminal_audit_window",
                    "label": "终端审计窗口",
                    "latest_remaining_minutes": 0,
                    "latest_chapter": 32,
                    "raw_mention": "窗口已关闭",
                    "status": "resolved",
                }
            ],
        },
    )

    prompt = build_single_chapter_draft_prompt(context)
    content = "\n".join(message["content"] for message in prompt)

    assert "终端审计窗口：第32章已剩余 0 分钟" in content
    assert "已经归零、关闭或解决" in content
    assert "不得再写成 10 分钟、三小时、一天等正数剩余时间" in content
    assert "不得写“终端审计窗口还剩/只有/显示/跳到”加任何正数时间" in content


def test_memory_reset_countdown_constraint_overrides_older_summaries() -> None:
    context = ChapterContextPack(
        project_id="p1",
        project_title="灰城遗档",
        premise="主角：陆明，旧城档案修复师。",
        genre="悬疑科幻",
        setting_summary="核心系统记忆系统维持公共档案秩序。",
        chapter_number=12,
        chapter_plan_title="倒计时：最后一日",
        chapter_plan_one_line="陆明必须关闭核心系统。",
        chapter_goals=["关闭核心系统", "公开真相"],
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
    assert content.index("Canon 质量连续性约束") < content.index("本章计划")
    assert "若【Canon 质量连续性约束】与本章计划、前情摘要或旧设定冲突，必须以 Canon 约束为准" in content
    assert "不得把记忆重置周期写回五天、七天、三十多天" in content
    assert "只能写小于等于 1440 分钟" in content


def test_minute_level_memory_reset_constraint_marks_day_scale_plan_as_stale() -> None:
    context = ChapterContextPack(
        project_id="p1",
        project_title="灰城遗档",
        premise="主角：陆明，旧城档案修复师。",
        genre="悬疑科幻",
        setting_summary="核心系统记忆系统维持公共档案秩序。",
        chapter_number=23,
        chapter_plan_title="倒计时加速",
        chapter_plan_one_line="陆明发现记忆重置周期只剩不到十天。",
        chapter_goals=["发现倒计时加速"],
        canon_quality_context={
            "countdown_constraints": [
                {
                    "countdown_key": "memory_reset",
                    "label": "记忆重置周期",
                    "latest_remaining_minutes": 90,
                    "latest_chapter": 22,
                    "raw_mention": "九十分钟",
                }
            ],
        },
    )

    prompt = build_single_chapter_draft_prompt(context)
    content = "\n".join(message["content"] for message in prompt)

    assert "计划覆盖：当前记忆重置周期已经进入 90 分钟级危机" in content
    assert "若出现“不到十天”“九天”“八天”“七天”“三天后”等天级安排" in content
    assert "三小时”“四十八小时”“两天" in content
    assert "系统日志原本还有三天" in content
    assert "必须改写为小于等于 90 分钟的连续倒计时" in content
    assert "章内单调规则：本章如果多次写记忆重置剩余时间，必须按出现顺序严格不增加" in content
    assert "巡逻间隔、认证窗口、解除窗口等局部时长必须明确命名为局部时长" in content
    assert "不要另造“主线倒计时”" in content
    assert "memory_reset" not in content


def test_writer_prompt_includes_character_state_constraints() -> None:
    context = ChapterContextPack(
        project_id="p1",
        project_title="灰城遗档",
        premise="主角：陆明，旧城档案修复师。",
        genre="悬疑科幻",
        setting_summary="核心系统记忆系统维持公共档案秩序。",
        chapter_number=32,
        chapter_plan_title="追踪器解除窗口",
        chapter_plan_one_line="陆明与韩青会合。",
        chapter_goals=["解除追踪器"],
        canon_quality_context={
            "character_state_constraints": [
                {
                    "character_name": "韩青",
                    "latest_state": "free",
                    "latest_chapter": 31,
                    "evidence_refs": ["recent_canon:31"],
                }
            ],
        },
    )

    prompt = build_single_chapter_draft_prompt(context)
    content = "\n".join(message["content"] for message in prompt)

    assert "已进入 canon 的角色状态约束" in content
    assert "韩青：第31章已脱困/自由" in content
    assert "不得把TA写回被捕、被关押、被羁押、被固定、仍在羁押室或等待救援" in content
    assert "可以写TA仍受追踪器、系统权限、伤势或路线限制" in content


def test_writer_prompt_includes_future_plan_audit_summary() -> None:
    context = ChapterContextPack(
        project_id="p1",
        project_title="灰城遗档",
        premise="主角：陆明，旧城档案修复师。",
        genre="悬疑科幻",
        setting_summary="核心系统记忆系统维持公共档案秩序。",
        chapter_number=23,
        chapter_plan_title="倒计时加速",
        chapter_plan_one_line="陆明必须按九十分钟推进。",
        chapter_goals=["继续核心系统倒计时"],
        canon_quality_context={
            "future_plan_audit_summary": {
                "status": "fail",
                "issues": [
                    {
                        "issue_type": "countdown_future_plan_conflict",
                        "target_chapter": 24,
                        "description": "第24章计划仍写九天倒计时。",
                    }
                ],
                "applied_plan_patch_ids": ["patch-24"],
            }
        },
    )

    prompt = build_single_chapter_draft_prompt(context)
    content = "\n".join(message["content"] for message in prompt)

    assert "Future plan audit" in content
    assert "countdown_future_plan_conflict" in content
    assert "patch-24" in content


def test_writer_prompt_changes_hook_contract_for_final_chapter() -> None:
    context = ChapterContextPack(
        project_id="p1",
        project_title="灰城遗档",
        premise="主角：陆明，旧城档案修复师。",
        genre="悬疑科幻",
        setting_summary="核心系统记忆系统维持公共档案秩序。",
        project_target_total_chapters=12,
        chapter_number=12,
        chapter_plan_title="倒计时：最后一日",
        chapter_plan_one_line="陆明必须关闭核心系统。",
        chapter_goals=["关闭核心系统", "公开真相"],
    )

    prompt = build_single_chapter_draft_prompt(context)
    content = "\n".join(message["content"] for message in prompt)

    assert "本章是全书终章或当前目标终章" in content
    assert "不要留下追兵、被困、钥匙损坏、准备公开、正要关闭" in content
    assert "本章结尾必须留下明确钩子" not in content


def test_final_writer_prompt_requires_executed_resolution_not_new_prerequisites() -> None:
    context = ChapterContextPack(
        project_id="p1",
        project_title="灰城遗档",
        premise="主角：陆明，旧城档案修复师。",
        genre="悬疑科幻",
        setting_summary="核心系统记忆系统维持公共档案秩序。",
        chapter_number=12,
        chapter_plan_title="倒计时：最后一日",
        chapter_plan_one_line="陆明必须关闭核心系统。",
        chapter_goals=["关闭核心系统", "公开真相"],
        canon_quality_context={"is_final_chapter": True},
    )

    prompt = build_single_chapter_draft_prompt(context)
    content = "\n".join(message["content"] for message in prompt)

    assert "如果写到关闭方法、钥匙、坐标、芯片或锁孔" in content
    assert "必须在本章完成使用、关闭或公开" in content
    assert "不要新增需要下一章解决的三把钥匙、下一层入口、未知坐标或新倒计时" in content
    assert "不要把“去指定机构交最后一段记录”" in content
    assert "最后一段记录、剩余证据、最后一份档案" in content


def test_final_writer_prompt_forbids_unresolved_trapped_sacrifice() -> None:
    context = ChapterContextPack(
        project_id="p1",
        project_title="灰城遗档",
        premise="主角：陆明，旧城档案修复师。",
        genre="悬疑科幻",
        setting_summary="核心系统记忆系统维持公共档案秩序。",
        chapter_number=12,
        chapter_plan_title="倒计时：最后一日",
        chapter_plan_one_line="陆明必须关闭核心系统。",
        chapter_goals=["关闭核心系统", "公开真相"],
        canon_quality_context={"is_final_chapter": True},
    )

    prompt = build_single_chapter_draft_prompt(context)
    content = "\n".join(message["content"] for message in prompt)

    assert "不要把“被困在机房/地下设施/系统核心内”当作终章结局" in content
    assert "牺牲必须写成已完成的终局代价" in content
    assert "被救出、死亡/牺牲确认、或后日谈确认主线已结清" in content
