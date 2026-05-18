from __future__ import annotations

from forwin.protocol.context import ChapterContextPack
from forwin.protocol.experience import BandDelightSchedule, BandObligationContract
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


def test_writer_prompt_does_not_inject_current_book_countdown_labels_without_profile() -> None:
    context = ChapterContextPack(
        project_id="p1",
        project_title="通用项目",
        premise="主角：陈星，调查一座失联空间站。",
        genre="科幻",
        setting_summary="空间站事故调查。",
        chapter_number=1,
        chapter_plan_title="失联信号",
        chapter_plan_one_line="陈星收到异常信号。",
        chapter_goals=["进入空间站"],
        canon_quality_context={
            "countdown_constraints": [
                {
                    "countdown_key": "memory_reset",
                    "latest_remaining_minutes": 30,
                    "latest_chapter": 0,
                }
            ],
        },
    )

    prompt = build_single_chapter_draft_prompt(context)
    content = "\n".join(message["content"] for message in prompt)

    assert "memory_reset 倒计时" in content
    assert "记忆重置周期" not in content
    assert "终端审计窗口" not in content


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
    assert "终端审计窗口硬性规则" in content
    assert "不得写成任何大于最新 ledger 的旧尺度" in content


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
    assert "不得再写成正数剩余时间" in content


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
    assert "记忆重置周期硬性规则" in content
    assert "不得写成任何大于最新 ledger 的旧尺度" in content
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

    assert "计划覆盖：当前记忆重置周期已进入 90 分钟级危机" in content
    assert "必须改写为小于等于 90 分钟的连续倒计时" in content
    assert "章内单调规则：本章如果多次写记忆重置周期剩余时间，必须按出现顺序严格不增加" in content
    assert "局部时长必须明确命名为局部时长" in content
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


def test_writer_prompt_suppresses_constraints_already_promoted_to_plan_patches() -> None:
    context = ChapterContextPack(
        project_id="p1",
        project_title="灰城遗档",
        premise="主角：陆明，旧城档案修复师。",
        genre="悬疑科幻",
        setting_summary="核心系统维持公共档案秩序。",
        chapter_number=14,
        chapter_plan_title="门禁来源",
        chapter_plan_one_line="陆明确认终端门禁来源。",
        chapter_goals=["兑现计划补丁"],
        canon_quality_context={
            "suppressed_prompt_constraint_keys": ["obligation:obl-now", "signal:sig-1"],
            "active_narrative_obligations": [
                {
                    "id": "obl-now",
                    "type": "reader_promise_payoff",
                    "priority": "P1",
                    "summary": "偿还前文承诺。",
                    "deadline_chapter": 14,
                    "payoff_test": "第14章必须给出门禁来源证据。",
                    "must_resolve_now": True,
                }
            ],
            "open_signals": [
                {
                    "signal_id": "sig-1",
                    "signal_type": "placeholder_leakage",
                    "severity": "error",
                    "chapter_number": 11,
                    "subject_key": "placeholder",
                    "description": "第11章残留占位符，必须替换为具体证据。",
                }
            ],
        },
    )

    prompt = build_single_chapter_draft_prompt(context)
    content = "\n".join(message["content"] for message in prompt)

    assert "obl-now" not in content
    assert "sig-1" not in content
    assert "第11章残留占位符" not in content
    assert "第14章必须给出门禁来源证据" not in content


def test_writer_prompt_suppresses_countdown_constraint_promoted_to_plan_patch() -> None:
    context = ChapterContextPack(
        project_id="p1",
        project_title="灰城遗档",
        premise="主角：陆明，旧城档案修复师。",
        genre="悬疑科幻",
        setting_summary="核心系统维持公共档案秩序。",
        chapter_number=14,
        chapter_plan_title="倒计时修正",
        chapter_plan_one_line="陆明确认局部窗口。",
        chapter_goals=["兑现计划补丁"],
        canon_quality_context={
            "suppressed_prompt_constraint_keys": ["countdown:倒计时甲"],
            "countdown_constraints": [
                {
                    "countdown_key": "倒计时甲",
                    "label": "倒计时甲",
                    "latest_remaining_minutes": 10,
                    "latest_chapter": 13,
                    "raw_mention": "倒计时甲剩余十分钟。",
                },
                {
                    "countdown_key": "倒计时乙",
                    "label": "倒计时乙",
                    "latest_remaining_minutes": 5,
                    "latest_chapter": 13,
                    "raw_mention": "倒计时乙剩余五分钟。",
                },
            ],
        },
    )

    prompt = build_single_chapter_draft_prompt(context)
    content = "\n".join(message["content"] for message in prompt)

    assert "倒计时甲" not in content
    assert "倒计时乙" in content
    assert "剩余 5 分钟" in content


def test_writer_prompt_includes_band_obligation_contract() -> None:
    context = ChapterContextPack(
        project_id="p1",
        project_title="灰城遗档",
        premise="主角：陆明，旧城档案修复师。",
        genre="悬疑科幻",
        setting_summary="核心系统记忆系统维持公共档案秩序。",
        chapter_number=13,
        chapter_plan_title="审计窗口回响",
        chapter_plan_one_line="陆明继续追查审计窗口真相。",
        chapter_goals=["推进审计窗口真相"],
        band_delight_schedule=BandDelightSchedule(
            band_id="arc-1:band:2",
            chapter_start=11,
            chapter_end=14,
            stall_guard_max_gap=1,
            band_obligation_contract=BandObligationContract(
                open_obligations=["obl-band"],
                must_resolve_by_band_end=["obl-band"],
                allowed_carry_forward=[],
                payoff_tests={"obl-band": "第14章前必须给出审计窗口真相证据。"},
                affected_chapters={"obl-band": [11, 12, 13, 14]},
                writer_context_injections=[
                    {
                        "obligation_id": "obl-band",
                        "deadline_chapter": 14,
                        "payoff_test": "第14章前必须给出审计窗口真相证据。",
                    }
                ],
                reviewer_context_injections=[],
            ),
        ),
    )

    prompt = build_single_chapter_draft_prompt(context)
    content = "\n".join(message["content"] for message in prompt)

    assert "band 叙事义务" in content
    assert "obl-band" in content
    assert "第14章前必须给出审计窗口真相证据" in content
    assert "本 band 结束前必须清偿" in content


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
    assert "不要留下追兵、被困、关键道具损坏、准备公开、正要关闭" in content
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

    assert "如果写到关闭方法、关键道具、坐标或入口" in content
    assert "必须在本章完成使用、关闭或公开" in content
    assert "不要只把它们作为下一步任务" in content


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

    assert "不要把“被困在最终设施内”当作终章结局" in content
    assert "牺牲必须写成已完成的终局代价" in content
    assert "被救出、死亡/牺牲确认、或后日谈确认主线已结清" in content
