from __future__ import annotations

from forwin.protocol.context import ChapterContextPack
from forwin.protocol.experience import BandDelightSchedule, BandObligationContract
from forwin.writer.prompt_core import _canon_quality_context_section, build_single_chapter_draft_prompt
from forwin.writer.prompt_core.sections import _story_basics_section


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


def test_writer_prompt_ignores_retired_countdown_and_character_state_constraints() -> None:
    context = ChapterContextPack(
        project_id="p1",
        project_title="通用项目",
        premise="主角：陈星，调查一座失联空间站。",
        genre="科幻",
        setting_summary="空间站事故调查。",
        chapter_number=12,
        chapter_plan_title="失联信号",
        chapter_plan_one_line="陈星收到异常信号。",
        chapter_goals=["进入空间站"],
        canon_quality_context={
            "countdown_constraints": [
                {"countdown_key": "oxygen_lock", "label": "氧舱倒计时", "latest_remaining_minutes": 30, "latest_chapter": 11}
            ],
            "character_state_constraints": [
                {"character_name": "韩遥", "latest_state": "free", "latest_chapter": 11}
            ],
        },
    )

    assert _canon_quality_context_section(context) is None
    content = "\n".join(message["content"] for message in build_single_chapter_draft_prompt(context))
    assert "氧舱倒计时" not in content
    assert "韩遥" not in content
    assert "后文时间数字必须承接前文递减" not in content


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


def test_canon_quality_constraints_precede_the_chapter_plan() -> None:
    context = ChapterContextPack(
        project_id="p1",
        project_title="灰城遗档",
        premise="主角：陆明，旧城档案修复师。",
        genre="悬疑科幻",
        setting_summary="核心系统记忆系统维持公共档案秩序。",
        chapter_number=12,
        chapter_plan_title="最后一日",
        chapter_plan_one_line="陆明必须关闭核心系统。",
        chapter_goals=["关闭核心系统", "公开真相"],
        canon_quality_context={"is_final_chapter": True},
    )

    prompt = build_single_chapter_draft_prompt(context)
    content = "\n".join(message["content"] for message in prompt)

    assert content.index("Canon 质量连续性约束") < content.index("本章计划")
    assert "若【Canon 质量连续性约束】与本章计划、前情摘要或旧设定冲突，必须以 Canon 约束为准" in content


def test_story_basics_section_has_no_example_roles_or_countdowns() -> None:
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

    section = _story_basics_section(context)

    assert "主角姓名：陆明" in section
    assert "倒计时约束" not in section
    assert "系统巡检员" not in section
    assert "剩余4小时" not in section
    assert "剩余7天" not in section


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


def test_writer_prompt_includes_generic_invariant_constraints() -> None:
    context = ChapterContextPack(
        project_id="p1",
        project_title="灰城遗档",
        premise="主角：陆明，旧城档案修复师。",
        genre="悬疑科幻",
        setting_summary="核心系统维持公共档案秩序。",
        chapter_number=14,
        chapter_plan_title="强状态修正",
        chapter_plan_one_line="陆明确认审计窗口和公开期限。",
        chapter_goals=["承接强状态"],
        canon_quality_context={
            "invariant_constraints": [
                {
                    "invariant_key": "deadline:public_hearing",
                    "kind": "deadline",
                    "subject_key": "public_hearing",
                    "label": "公开听证期限",
                    "current_value": {"deadline_chapter": 16},
                    "constraints": {"cannot_extend_without_bridge": True},
                },
            ],
        },
    )

    prompt = build_single_chapter_draft_prompt(context)
    content = "\n".join(message["content"] for message in prompt)

    assert "强状态 invariant ledger" in content
    assert "倒计时 ledger" not in content
    assert "不同倒计时各自独立" not in content
    assert "公开听证期限" in content
    assert "截止" in content
    assert "桥接" in content


def test_writer_prompt_includes_exact_immutable_rule_definition() -> None:
    context = ChapterContextPack(
        project_id="p1",
        project_title="门城协议",
        premise="主角：陆明，负责校验门禁规则。",
        genre="科幻",
        setting_summary="门禁文本会直接改变现实。",
        chapter_number=6,
        chapter_plan_title="再次校验",
        chapter_plan_one_line="陆明复核通行协议。",
        chapter_goals=["保持规则连续"],
        canon_quality_context={
            "invariant_constraints": [
                {
                    "invariant_key": "book_state_rule:rule-transit-protocol",
                    "kind": "active_rule",
                    "subject_key": "rule-transit-protocol",
                    "label": "通行协议",
                    "current_value": {
                        "trigger_condition": "三枚印记同时点亮",
                        "effect_description": "门扉只向右移动一格",
                        "public_version": "三印同亮，门右移一格。",
                    },
                    "constraints": {"immutable_definition": True},
                    "evidence_refs": ["chapter:5"],
                }
            ]
        },
    )

    prompt = build_single_chapter_draft_prompt(context)
    content = "\n".join(message["content"] for message in prompt)

    assert "通行协议" in content
    assert "三印同亮，门右移一格。" in content
    assert "门扉只向右移动一格" in content
    assert "不得静默改写" in content


def test_immutable_rules_are_not_pruned_by_generic_prompt_caps() -> None:
    invariants = [
        {
            "invariant_key": f"book_state_rule:rule-{index}",
            "kind": "active_rule",
            "label": f"规则{index}",
            "current_value": {"public_version": f"精确定义{index}"},
            "constraints": {"immutable_definition": True},
        }
        for index in range(1, 14)
    ]
    context = ChapterContextPack(
        project_id="p1",
        project_title="规则容量",
        premise="测试不可变规则不会被数量裁剪。",
        genre="科幻",
        setting_summary="",
        chapter_number=20,
        chapter_plan_title="容量检查",
        chapter_plan_one_line="逐项遵守既有规则。",
        chapter_goals=["保持规则连续"],
        canon_quality_context={"invariant_constraints": invariants},
    )

    writer_content = "\n".join(
        item["content"] for item in build_single_chapter_draft_prompt(context)
    )

    assert "精确定义13" in writer_content


def test_writer_prompt_suppresses_generic_invariant_constraint_promoted_to_plan_patch() -> None:
    context = ChapterContextPack(
        project_id="p1",
        project_title="灰城遗档",
        premise="主角：陆明，旧城档案修复师。",
        genre="悬疑科幻",
        setting_summary="核心系统维持公共档案秩序。",
        chapter_number=14,
        chapter_plan_title="强状态修正",
        chapter_plan_one_line="陆明确认公开期限。",
        chapter_goals=["承接强状态"],
        canon_quality_context={
            "suppressed_prompt_constraint_keys": ["invariant:deadline:public_hearing"],
            "invariant_constraints": [
                {
                    "invariant_key": "deadline:public_hearing",
                    "kind": "deadline",
                    "subject_key": "public_hearing",
                    "label": "公开听证期限",
                    "current_value": {"deadline_chapter": 16},
                }
            ],
        },
    )

    prompt = build_single_chapter_draft_prompt(context)
    content = "\n".join(message["content"] for message in prompt)

    assert "公开听证期限" not in content


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


def test_writer_prompt_includes_active_structural_patch_debt() -> None:
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
        canon_quality_context={
            "active_structural_patch_debt": [
                {
                    "patch_id": "patch-arc",
                    "scope": "arc",
                    "payoff_tests": ["本 arc 结束前必须解释身份线索。"],
                    "writer_context_injections": [
                        {
                            "scope": "arc",
                            "issue_kind": "identity_ambiguity",
                            "instruction": "身份线索需要在本 arc 内澄清。",
                        }
                    ],
                }
            ],
        },
    )

    prompt = build_single_chapter_draft_prompt(context)
    content = "\n".join(message["content"] for message in prompt)

    assert "结构性计划补丁债务" in content
    assert "patch-arc" in content
    assert "本 arc 结束前必须解释身份线索" in content


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
