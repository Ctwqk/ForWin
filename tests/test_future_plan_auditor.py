from __future__ import annotations

import json

from forwin.models import ChapterPlan
from forwin.models.phase import BandExperiencePlan
from forwin.narrative_obligations.types import NarrativeObligation
from forwin.planning.future_plan_auditor import FuturePlanAuditor
from forwin.protocol.experience import BandDelightSchedule


def _plan(number: int, *, one_line: str, goals: list[str] | None = None) -> ChapterPlan:
    return ChapterPlan(
        id=f"plan-{number}",
        project_id="project-1",
        arc_plan_id="arc-1",
        chapter_number=number,
        title=f"第{number}章",
        one_line=one_line,
        goals_json=json.dumps(goals or [], ensure_ascii=False),
        task_contract_json="[]",
        experience_plan_json="{}",
        status="planned",
    )


def test_future_plan_auditor_flags_day_scale_memory_reset_after_minute_ledger() -> None:
    result = FuturePlanAuditor().audit_plans(
        project_id="project-1",
        current_chapter=22,
        trigger_stage="post_acceptance",
        plans=[
            _plan(23, one_line="陆明发现记忆重置周期只剩不到十天。"),
            _plan(24, one_line="第九天，核心系统审计窗口继续逼近。", goals=["确认记忆重置还剩九天"]),
        ],
        canon_quality_context={
            "countdown_constraints": [
                {
                    "countdown_key": "memory_reset",
                    "label": "记忆重置周期",
                    "latest_remaining_minutes": 90,
                    "latest_chapter": 22,
                    "raw_mention": "九十分钟",
                }
            ]
        },
        obligations=[],
        target_total_chapters=60,
        include_current=False,
    )

    assert result.status == "fail"
    assert [issue.issue_type for issue in result.issues] == [
        "countdown_future_plan_conflict",
        "countdown_future_plan_conflict",
    ]
    assert [patch.patch_type for patch in result.plan_patches] == [
        "future_plan_audit",
        "future_plan_audit",
    ]
    assert result.plan_patches[0].target_plan_id == "plan-23"
    assert result.plan_patches[0].affected_chapters == [23]
    assert result.plan_patches[0].writer_context_injections[0]["countdown_key"] == "memory_reset"
    assert "90" in result.plan_patches[0].expected_resolution_tests[0]


def test_future_plan_auditor_flags_stale_duration_in_adjacent_clause() -> None:
    result = FuturePlanAuditor().audit_plans(
        project_id="project-1",
        current_chapter=23,
        trigger_stage="pre_write",
        plans=[
            _plan(23, one_line="陆明发现记忆重置周期比预期更短，只剩不到十天，而核心系统开始出现异常波动。"),
        ],
        canon_quality_context={
            "countdown_constraints": [
                {
                    "countdown_key": "memory_reset",
                    "label": "记忆重置周期",
                    "latest_remaining_minutes": 90,
                    "latest_chapter": 22,
                    "raw_mention": "九十分钟",
                }
            ]
        },
        obligations=[],
        target_total_chapters=60,
        include_current=True,
    )

    assert result.status == "fail"
    assert [issue.issue_type for issue in result.issues] == ["countdown_future_plan_conflict"]
    assert result.plan_patches[0].patch_type == "canon_plan_staleness"


def test_future_plan_auditor_allows_explicit_reset_or_branch_clock() -> None:
    result = FuturePlanAuditor().audit_plans(
        project_id="project-1",
        current_chapter=22,
        trigger_stage="post_acceptance",
        plans=[
            _plan(23, one_line="核心系统启动新的分支倒计时：九天后清理外部缓存。"),
        ],
        canon_quality_context={
            "countdown_constraints": [
                {
                    "countdown_key": "memory_reset",
                    "label": "记忆重置周期",
                    "latest_remaining_minutes": 90,
                    "latest_chapter": 22,
                }
            ]
        },
        obligations=[],
        target_total_chapters=60,
        include_current=False,
    )

    assert result.status == "pass"
    assert result.issues == []
    assert result.plan_patches == []


def test_future_plan_auditor_keeps_countdown_keys_separate() -> None:
    result = FuturePlanAuditor().audit_plans(
        project_id="project-1",
        current_chapter=22,
        trigger_stage="post_acceptance",
        plans=[
            _plan(23, one_line="终端审计授权窗口还有四小时，记忆重置周期只剩九十分钟。"),
        ],
        canon_quality_context={
            "countdown_constraints": [
                {
                    "countdown_key": "memory_reset",
                    "label": "记忆重置周期",
                    "latest_remaining_minutes": 90,
                    "latest_chapter": 22,
                },
                {
                    "countdown_key": "archive_cleanup",
                    "label": "终端审计窗口",
                    "latest_remaining_minutes": 240,
                    "latest_chapter": 22,
                },
            ]
        },
        obligations=[],
        target_total_chapters=60,
        include_current=False,
    )

    assert result.status == "pass"
    assert result.issues == []


def test_future_plan_auditor_applies_countdown_patch_to_plan_source() -> None:
    plan = _plan(
        23,
        one_line="陆明发现记忆重置周期只剩不到十天。",
        goals=["确认记忆重置还剩九天"],
    )
    result = FuturePlanAuditor().audit_plans(
        project_id="project-1",
        current_chapter=22,
        trigger_stage="post_acceptance",
        plans=[plan],
        canon_quality_context={
            "countdown_constraints": [
                {
                    "countdown_key": "memory_reset",
                    "label": "记忆重置周期",
                    "latest_remaining_minutes": 90,
                    "latest_chapter": 22,
                }
            ]
        },
        obligations=[],
        target_total_chapters=60,
        include_current=False,
    )

    FuturePlanAuditor().apply_plan_patch(plan, result.plan_patches[0])

    assert "不到十天" not in plan.one_line
    assert "九天" not in plan.goals_json
    assert "90分钟" in plan.one_line
    assert "90分钟" in plan.goals_json


def test_future_plan_auditor_rewrites_stale_duration_in_adjacent_clause() -> None:
    plan = _plan(
        23,
        one_line="陆明发现记忆重置周期比预期更短，只剩不到十天，而核心系统开始出现异常波动。",
    )
    result = FuturePlanAuditor().audit_plans(
        project_id="project-1",
        current_chapter=23,
        trigger_stage="pre_write",
        plans=[plan],
        canon_quality_context={
            "countdown_constraints": [
                {
                    "countdown_key": "memory_reset",
                    "label": "记忆重置周期",
                    "latest_remaining_minutes": 90,
                    "latest_chapter": 22,
                }
            ]
        },
        obligations=[],
        target_total_chapters=60,
        include_current=True,
    )

    FuturePlanAuditor().apply_plan_patch(plan, result.plan_patches[0])

    assert "不到十天" not in plan.one_line
    assert "不超过90分钟" in plan.one_line


def test_future_plan_auditor_adds_minute_level_hard_rule_when_missing() -> None:
    result = FuturePlanAuditor().audit_plans(
        project_id="project-1",
        current_chapter=23,
        trigger_stage="pre_write",
        plans=[
            _plan(23, one_line="陆明进入核心系统核心区，准备确认家族档案被抹除的原因。"),
        ],
        canon_quality_context={
            "countdown_constraints": [
                {
                    "countdown_key": "memory_reset",
                    "label": "记忆重置周期",
                    "latest_remaining_minutes": 90,
                    "latest_chapter": 22,
                }
            ]
        },
        obligations=[],
        target_total_chapters=60,
        include_current=True,
    )

    assert result.status == "warn"
    assert [issue.issue_type for issue in result.issues] == ["countdown_plan_hard_constraint_missing"]
    assert result.issues[0].blocking is False
    assert result.plan_patches[0].patch_type == "canon_plan_staleness"
    assert "旧计划/旧摘要时间不得写成前文事实" in result.plan_patches[0].writer_context_injections[0]["instruction"]


def test_future_plan_auditor_applies_minute_level_hard_rule_to_rule_anchors() -> None:
    plan = _plan(
        23,
        one_line="陆明进入核心系统核心区，准备确认家族档案被抹除的原因。",
    )
    result = FuturePlanAuditor().audit_plans(
        project_id="project-1",
        current_chapter=23,
        trigger_stage="pre_write",
        plans=[plan],
        canon_quality_context={
            "countdown_constraints": [
                {
                    "countdown_key": "memory_reset",
                    "label": "记忆重置周期",
                    "latest_remaining_minutes": 90,
                    "latest_chapter": 22,
                }
            ]
        },
        obligations=[],
        target_total_chapters=60,
        include_current=True,
    )

    FuturePlanAuditor().apply_plan_patch(plan, result.plan_patches[0])
    experience = json.loads(plan.experience_plan_json)

    assert any("旧计划/旧摘要时间不得写成前文事实" in item for item in experience["rule_anchors"])
    assert any("三天/七天" in item for item in experience["rule_anchors"])


def test_future_plan_auditor_does_not_cross_rewrite_other_countdown_rule_anchors() -> None:
    plan = _plan(
        23,
        one_line="陆明进入核心系统核心区，准备确认家族档案被抹除的原因。",
    )
    plan.experience_plan_json = json.dumps(
        {
            "rule_anchors": [
                (
                    "主倒计时必须延续最新 canon ledger：剩余时间不得超过 9 分钟。"
                    "archive_cleanup必须延续最新 canon ledger：剩余时间不得超过 11 分钟。"
                    "memory_reset必须延续最新 canon ledger：剩余时间不得超过 90 分钟。"
                )
            ]
        },
        ensure_ascii=False,
    )
    result = FuturePlanAuditor().audit_plans(
        project_id="project-1",
        current_chapter=23,
        trigger_stage="pre_write",
        plans=[plan],
        canon_quality_context={
            "countdown_constraints": [
                {
                    "countdown_key": "main",
                    "label": "主倒计时",
                    "latest_remaining_minutes": 9,
                    "latest_chapter": 22,
                }
            ]
        },
        obligations=[],
        target_total_chapters=60,
        include_current=True,
    )

    FuturePlanAuditor().apply_plan_patch(plan, result.plan_patches[0])
    experience = json.loads(plan.experience_plan_json)
    joined = json.dumps(experience["rule_anchors"], ensure_ascii=False)

    assert "archive_cleanup必须延续最新 canon ledger：剩余时间不得超过 11 分钟" in joined
    assert "memory_reset必须延续最新 canon ledger：剩余时间不得超过 90 分钟" in joined
    assert "archive_cleanup必须延续最新 canon ledger：剩余时间不得超过不超过9分钟" not in joined


def test_future_plan_auditor_cleans_polluted_countdown_instructions_from_plan_text() -> None:
    plan = _plan(
        23,
        one_line=(
            "archive_cleanup必须延续最新 canon ledger：剩余时间不得超过 11 分钟。 "
            "memory_reset必须延续最新 canon ledger：剩余时间不得超过不超过11分钟。 "
            "[late] 陆明发现记忆重置周期只剩不到十天。"
        ),
        goals=[
            "main必须延续最新 canon ledger：剩余时间不得超过 9 分钟。",
            "确认记忆重置还剩九天",
        ],
    )
    plan.experience_plan_json = json.dumps(
        {
            "immersion_anchors": [
                (
                    "main必须延续最新 canon ledger：剩余时间不得超过 9 分钟。"
                    "archive_cleanup必须延续最新 canon ledger：剩余时间不得超过不超过9分钟。"
                    "[midpoint] 陆明发现核心系统异常。"
                )
            ],
            "progress_markers": [
                "archive_cleanup必须延续最新 canon ledger：剩余时间不得超过 11 分钟。",
                "给出真实线索",
            ],
            "rule_anchors": [
                "archive_cleanup必须延续最新 canon ledger：剩余时间不得超过不超过9分钟。",
                "核心系统用于选择性抹除历史",
            ],
        },
        ensure_ascii=False,
    )
    result = FuturePlanAuditor().audit_plans(
        project_id="project-1",
        current_chapter=23,
        trigger_stage="pre_write",
        plans=[plan],
        canon_quality_context={
            "countdown_constraints": [
                {
                    "countdown_key": "memory_reset",
                    "label": "记忆重置周期",
                    "latest_remaining_minutes": 90,
                    "latest_chapter": 22,
                }
            ]
        },
        obligations=[],
        target_total_chapters=60,
        include_current=True,
    )

    FuturePlanAuditor().apply_plan_patch(plan, result.plan_patches[0])
    experience = json.loads(plan.experience_plan_json)
    serialized = json.dumps(
        {
            "one_line": plan.one_line,
            "goals": json.loads(plan.goals_json),
            "experience": experience,
        },
        ensure_ascii=False,
    )

    assert "必须延续最新 canon ledger" not in plan.one_line
    assert "不超过不超过" not in serialized
    assert "不到十天" not in plan.one_line
    assert "不超过90分钟" in plan.one_line
    assert "确认记忆重置不超过90分钟" in json.loads(plan.goals_json)
    assert experience["immersion_anchors"] == ["[midpoint] 陆明发现核心系统异常。"]
    assert experience["progress_markers"] == ["给出真实线索"]
    assert any("记忆重置周期必须延续最新 canon ledger" in item for item in experience["rule_anchors"])


def test_future_plan_auditor_patches_polluted_plan_even_without_stale_duration() -> None:
    plan = _plan(
        23,
        one_line=(
            "memory_reset必须延续最新 canon ledger：剩余时间不得超过 90 分钟。 "
            "陆明进入核心系统底层。"
        ),
        goals=["确认陆氏原始档案去向"],
    )
    plan.experience_plan_json = json.dumps(
        {
            "rule_anchors": [
                "记忆重置周期必须延续最新 canon ledger：剩余时间不得超过 90 分钟。旧计划/旧摘要时间不得写成前文事实；不得写“系统日志原本还有三天/七天/几小时”、“主角以为还有几天”或任何大于最新 ledger 的旧尺度，除非明确标记为公开伪数据、误导信息、reset 或 branch clock。本章所有记忆重置/校准/熔铸窗口只能继续小于等于 90 分钟，不要写回三天/七天/三小时/两小时等旧尺度。"
            ],
            "progress_markers": [
                "archive_cleanup必须延续最新 canon ledger：剩余时间不得超过不超过9分钟。",
            ],
        },
        ensure_ascii=False,
    )
    result = FuturePlanAuditor().audit_plans(
        project_id="project-1",
        current_chapter=23,
        trigger_stage="pre_write",
        plans=[plan],
        canon_quality_context={
            "countdown_constraints": [
                {
                    "countdown_key": "memory_reset",
                    "label": "记忆重置周期",
                    "latest_remaining_minutes": 90,
                    "latest_chapter": 22,
                }
            ]
        },
        obligations=[],
        target_total_chapters=60,
        include_current=True,
    )

    assert result.status == "warn"
    assert [issue.issue_type for issue in result.issues] == ["countdown_plan_instruction_pollution"]
    FuturePlanAuditor().apply_plan_patch(plan, result.plan_patches[0])
    experience = json.loads(plan.experience_plan_json)

    assert "必须延续最新 canon ledger" not in plan.one_line
    assert plan.one_line == "陆明进入核心系统底层。"
    assert experience["progress_markers"] == []
    assert any("记忆重置周期必须延续最新 canon ledger" in item for item in experience["rule_anchors"])


def test_future_plan_auditor_flags_false_accepted_canon_countdown_contract() -> None:
    result = FuturePlanAuditor().audit_plans(
        project_id="project-1",
        current_chapter=25,
        trigger_stage="post_acceptance",
        plans=[
            _plan(
                26,
                one_line="陆明进入后门终端。",
                goals=[
                    (
                        "第24章 accepted canon：陆明独自进入核心系统底层电梯确认启动紧急重置协议；"
                        "韩青被系统巡检员抓获；记忆重置倒计时不超过9分钟。"
                        "第25-36章必须紧接此状态。"
                    )
                ],
            )
        ],
        canon_quality_context={
            "countdown_constraints": [
                {
                    "countdown_key": "memory_reset",
                    "label": "记忆重置周期",
                    "latest_remaining_minutes": 77,
                    "latest_chapter": 25,
                    "raw_mention": "77分钟",
                }
            ]
        },
        obligations=[],
        target_total_chapters=36,
        include_current=False,
    )

    assert result.status == "fail"
    assert [issue.issue_type for issue in result.issues] == ["countdown_plan_false_prior_conflict"]
    assert result.plan_patches[0].patch_type == "future_plan_audit"


def test_future_plan_auditor_rewrites_false_accepted_canon_countdown_contract() -> None:
    plan = _plan(
        26,
        one_line="陆明进入后门终端。",
        goals=[
            (
                "第24章 accepted canon：陆明独自进入核心系统底层电梯确认启动紧急重置协议；"
                "韩青被系统巡检员抓获；记忆重置倒计时不超过9分钟。"
                "第25-36章必须紧接此状态。"
            )
        ],
    )
    result = FuturePlanAuditor().audit_plans(
        project_id="project-1",
        current_chapter=25,
        trigger_stage="post_acceptance",
        plans=[plan],
        canon_quality_context={
            "countdown_constraints": [
                {
                    "countdown_key": "memory_reset",
                    "label": "记忆重置周期",
                    "latest_remaining_minutes": 77,
                    "latest_chapter": 25,
                    "raw_mention": "77分钟",
                }
            ]
        },
        obligations=[],
        target_total_chapters=36,
        include_current=False,
    )

    FuturePlanAuditor().apply_plan_patch(plan, result.plan_patches[0])

    serialized = json.dumps(json.loads(plan.goals_json), ensure_ascii=False)
    assert "不超过9分钟" not in serialized
    assert "不超过77分钟" in serialized


def test_future_plan_auditor_flags_custody_plan_after_recent_release() -> None:
    result = FuturePlanAuditor().audit_plans(
        project_id="project-1",
        current_chapter=31,
        trigger_stage="post_acceptance",
        plans=[
            _plan(
                32,
                one_line="陆明在不破坏韩青被捕状态的前提下打开救援窗口。",
                goals=["确认韩青仍被羁押，并让她提供核心系统底层档案库入口。"],
            )
        ],
        canon_quality_context={
            "character_state_constraints": [
                {
                    "character_name": "韩青",
                    "transition_type": "custody_state",
                    "latest_state": "free",
                    "latest_chapter": 31,
                    "evidence_refs": ["chapter:31"],
                }
            ]
        },
        obligations=[],
        target_total_chapters=36,
        include_current=False,
    )

    assert result.status == "fail"
    assert [issue.issue_type for issue in result.issues] == ["custody_future_plan_conflict"]
    assert result.plan_patches[0].patch_type == "future_plan_audit"
    assert result.plan_patches[0].writer_context_injections[0]["character_name"] == "韩青"
    assert "已脱困" in result.plan_patches[0].expected_resolution_tests[0]


def test_future_plan_auditor_rewrites_custody_plan_after_recent_release() -> None:
    plan = _plan(
        32,
        one_line="陆明在不破坏韩青被捕状态的前提下打开救援窗口。",
        goals=["确认韩青仍被羁押，并让她提供核心系统底层档案库入口。"],
    )
    result = FuturePlanAuditor().audit_plans(
        project_id="project-1",
        current_chapter=32,
        trigger_stage="pre_write",
        plans=[plan],
        canon_quality_context={
            "character_state_constraints": [
                {
                    "character_name": "韩青",
                    "transition_type": "custody_state",
                    "latest_state": "free",
                    "latest_chapter": 31,
                    "evidence_refs": ["chapter:31"],
                }
            ]
        },
        obligations=[],
        target_total_chapters=36,
        include_current=True,
    )

    FuturePlanAuditor().apply_plan_patch(plan, result.plan_patches[0])

    serialized = json.dumps(
        {
            "title": plan.title,
            "one_line": plan.one_line,
            "goals": json.loads(plan.goals_json),
            "experience": json.loads(plan.experience_plan_json),
        },
        ensure_ascii=False,
    )
    assert "救援窗口" not in serialized
    assert "被捕状态" not in serialized
    assert "仍被羁押" not in serialized
    assert "已脱困但仍受追踪器或系统权限限制" in serialized
    assert "不得把韩青写回被捕/羁押/固定状态" in serialized


def test_future_plan_auditor_flags_closed_countdown_reopened_in_plan() -> None:
    result = FuturePlanAuditor().audit_plans(
        project_id="project-1",
        current_chapter=32,
        trigger_stage="pre_write",
        plans=[
            _plan(32, one_line="陆明发现终端审计窗口还有8分钟。"),
        ],
        canon_quality_context={
            "countdown_constraints": [
                {
                    "countdown_key": "terminal_audit_window",
                    "label": "终端审计窗口",
                    "latest_remaining_minutes": 0,
                    "latest_chapter": 31,
                    "status": "resolved",
                }
            ]
        },
        obligations=[],
        target_total_chapters=36,
        include_current=True,
    )

    assert result.status == "fail"
    assert [issue.issue_type for issue in result.issues] == ["countdown_closed_future_plan_conflict"]
    assert result.plan_patches[0].patch_type == "canon_plan_staleness"


def test_future_plan_auditor_rewrites_closed_countdown_to_closed_state() -> None:
    plan = _plan(32, one_line="陆明发现终端审计窗口还有8分钟。")
    result = FuturePlanAuditor().audit_plans(
        project_id="project-1",
        current_chapter=32,
        trigger_stage="pre_write",
        plans=[plan],
        canon_quality_context={
            "countdown_constraints": [
                {
                    "countdown_key": "terminal_audit_window",
                    "label": "终端审计窗口",
                    "latest_remaining_minutes": 0,
                    "latest_chapter": 31,
                    "status": "resolved",
                }
            ]
        },
        obligations=[],
        target_total_chapters=36,
        include_current=True,
    )

    FuturePlanAuditor().apply_plan_patch(plan, result.plan_patches[0])

    serialized = json.dumps(
        {
            "one_line": plan.one_line,
            "experience": json.loads(plan.experience_plan_json),
        },
        ensure_ascii=False,
    )
    assert "8分钟" not in serialized
    assert "终端审计窗口已关闭" in serialized


def test_future_plan_auditor_generates_band_binding_patch_for_band_obligation() -> None:
    schedule = BandDelightSchedule(
        band_id="arc-1:band:2",
        chapter_start=11,
        chapter_end=14,
        stall_guard_max_gap=1,
    )
    band_row = BandExperiencePlan(
        id="band-row-1",
        project_id="project-1",
        arc_id="arc-1",
        band_id="arc-1:band:2",
        chapter_start=11,
        chapter_end=14,
        schedule_json=schedule.model_dump_json(),
        task_contract_json="[]",
    )
    obligation = NarrativeObligation(
        id="obl-band",
        project_id="project-1",
        origin_chapter_number=10,
        obligation_type="reader_promise_payoff",
        priority="P1",
        status="active",
        summary="band 内兑现读者承诺。",
        hardness="design_debt",
        deadline_chapter=14,
        payoff_test="第14章前必须给出审计窗口真相证据。",
        linked_plan_patch_ids=["patch-old"],
        metadata={"minimum_scope": "band"},
    )

    result = FuturePlanAuditor().audit_plans(
        project_id="project-1",
        current_chapter=10,
        trigger_stage="post_acceptance",
        plans=[],
        band_rows=[band_row],
        canon_quality_context={},
        obligations=[obligation],
        target_total_chapters=20,
        include_current=False,
    )

    assert result.status == "fail"
    assert result.issues[0].issue_type == "obligation_missing_from_band_plan"
    patch = result.plan_patches[0]
    assert patch.patch_type == "obligation_band_plan_binding"
    assert patch.target_scope == "band"
    assert patch.target_band_id == "arc-1:band:2"
    assert patch.affected_chapters == [11, 12, 13, 14]


def test_future_plan_auditor_skips_chapter_binding_when_band_contract_covers_obligation() -> None:
    schedule_payload = {
        "band_id": "arc-1:band:2",
        "chapter_start": 11,
        "chapter_end": 14,
        "stall_guard_max_gap": 1,
        "band_obligation_contract": {
            "open_obligations": ["obl-band"],
            "must_resolve_by_band_end": ["obl-band"],
            "allowed_carry_forward": [],
            "payoff_tests": {"obl-band": "第14章前必须给出审计窗口真相证据。"},
            "affected_chapters": {"obl-band": [11, 12, 13, 14]},
            "writer_context_injections": [],
            "reviewer_context_injections": [],
        },
    }
    band_row = BandExperiencePlan(
        id="band-row-1",
        project_id="project-1",
        arc_id="arc-1",
        band_id="arc-1:band:2",
        chapter_start=11,
        chapter_end=14,
        schedule_json=json.dumps(schedule_payload, ensure_ascii=False),
        task_contract_json="[]",
    )
    deadline_plan = _plan(14, one_line="兑现审计窗口。")
    obligation = NarrativeObligation(
        id="obl-band",
        project_id="project-1",
        origin_chapter_number=10,
        obligation_type="reader_promise_payoff",
        priority="P1",
        status="active",
        summary="band 内兑现读者承诺。",
        hardness="design_debt",
        deadline_chapter=14,
        payoff_test="第14章前必须给出审计窗口真相证据。",
        linked_plan_patch_ids=["patch-old"],
        metadata={"minimum_scope": "band"},
    )

    result = FuturePlanAuditor().audit_plans(
        project_id="project-1",
        current_chapter=10,
        trigger_stage="post_acceptance",
        plans=[deadline_plan],
        band_rows=[band_row],
        canon_quality_context={},
        obligations=[obligation],
        target_total_chapters=20,
        include_current=False,
    )

    assert result.status == "pass"
    assert result.issues == []
    assert result.plan_patches == []


def test_future_plan_auditor_promotes_must_resolve_now_obligation_to_pre_write_patch() -> None:
    plan = _plan(12, one_line="陆明准备进入终端大厅。")
    obligation = NarrativeObligation(
        id="obl-now",
        project_id="project-1",
        origin_chapter_number=10,
        obligation_type="reader_promise_payoff",
        priority="P1",
        status="active",
        summary="偿还前文承诺。",
        hardness="canon_risk",
        deadline_chapter=12,
        payoff_test="第12章必须给出终端门禁来源证据。",
        must_resolve_now=True,
    )

    result = FuturePlanAuditor().audit_plans(
        project_id="project-1",
        current_chapter=11,
        trigger_stage="pre_write",
        plans=[plan],
        canon_quality_context={},
        obligations=[obligation],
        target_total_chapters=20,
        include_current=False,
    )

    assert result.status == "fail"
    assert [issue.issue_type for issue in result.issues] == ["obligation_pre_write_required"]
    assert result.plan_patches[0].patch_type == "obligation_pre_write"
    assert result.metadata["suppressed_prompt_constraint_keys"] == ["obligation:obl-now"]

    FuturePlanAuditor().apply_plan_patch(plan, result.plan_patches[0])
    serialized = json.dumps(
        {"goals": json.loads(plan.goals_json), "experience": json.loads(plan.experience_plan_json)},
        ensure_ascii=False,
    )
    assert "obl-now" in serialized
    assert "第12章必须给出终端门禁来源证据" in serialized


def test_future_plan_auditor_promotes_stale_open_signal_to_pre_write_patch() -> None:
    plan = _plan(14, one_line="陆明进入下一处证据点。")

    result = FuturePlanAuditor().audit_plans(
        project_id="project-1",
        current_chapter=13,
        trigger_stage="pre_write",
        plans=[plan],
        canon_quality_context={
            "open_signals": [
                {
                    "signal_id": "sig-1",
                    "signal_type": "placeholder_leakage",
                    "severity": "error",
                    "chapter_number": 10,
                    "subject_key": "placeholder",
                    "description": "第10章残留占位符，必须替换为具体证据。",
                }
            ]
        },
        obligations=[],
        target_total_chapters=20,
        include_current=False,
    )

    assert result.status == "fail"
    assert [issue.issue_type for issue in result.issues] == ["stale_open_signal_pre_write_required"]
    assert result.plan_patches[0].patch_type == "signal_pre_write"
    assert result.metadata["suppressed_prompt_constraint_keys"] == ["signal:sig-1"]

    FuturePlanAuditor().apply_plan_patch(plan, result.plan_patches[0])
    serialized = json.dumps(
        {"goals": json.loads(plan.goals_json), "experience": json.loads(plan.experience_plan_json)},
        ensure_ascii=False,
    )
    assert "sig-1" in serialized
    assert "第10章残留占位符" in serialized
