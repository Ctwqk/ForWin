from __future__ import annotations

import importlib
import importlib.util
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from forwin.models.phase import BandExperiencePlan
from forwin.models.project import ChapterPlan
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.experience import BandDelightSchedule, ChapterExperiencePlan
from forwin.protocol.review import RepairInstruction
from forwin.retrieval import RetrievalBroker
from forwin.state.repo import StateRepository
from forwin.writer import prompt_core
from tests.test_candidate_review_owner import database as _shared_database

database = _shared_database


def _owner():
    assert importlib.util.find_spec("forwin.review.repair.plan_patch") is not None, (
        "A0 needs independent plan patch ownership"
    )
    module = importlib.import_module("forwin.review.repair.plan_patch")
    broker = RetrievalBroker(context_budget_chars=50_000)
    broker.build_chapter_context = (
        lambda repo, project_id, plan: ChapterContextPack(
            project_id=project_id, project_title="Book", premise="Fixture",
            genre="fantasy", setting_summary="", chapter_number=plan.chapter_number,
            chapter_plan_title=plan.title, chapter_plan_one_line="", chapter_goals=[],
            chapter_experience_plan=repo.get_chapter_experience_plan(
                project_id, plan.chapter_number
            ),
        )
    )
    owner = module.RepairPlanPatchService(
        retrieval_broker=broker,
        arc_envelope_manager=SimpleNamespace(
            _derive_chapter_experience_plan=lambda **kwargs: ChapterExperiencePlan(
                question_hook=f"rebuilt {kwargs['chapter_number']}"
            )
        ),
    )
    return module, owner


def _request(
    module,
    session,
    chapter,
    *,
    scope="draft",
    design_patch=None,
    must_fix=None,
):
    return module.RepairPlanPatchRequest(
        session=session,
        repo=StateRepository(session),
        project_id="book",
        chapter_plan=chapter,
        context=ChapterContextPack(
            project_id="book",
            chapter_number=chapter.chapter_number,
            project_title="Book",
            premise="Fixture",
            genre="fantasy",
            setting_summary="",
            chapter_plan_title=chapter.title,
            chapter_plan_one_line="",
            chapter_goals=[],
        ),
        repair_scope=scope,
        instruction=RepairInstruction(
            repair_scope=scope,
            failure_type="continuity",
            must_fix=must_fix if must_fix is not None else ["补充证据"],
            design_patch=design_patch or {"question_hook": "Where?"},
        ),
    )


def _writer_prompt(context):
    return "\n".join(
        item["content"]
        for item in prompt_core.build_single_chapter_draft_prompt(context)
    )


def test_draft_patch_is_transient_and_leaves_plan_untouched(database):
    session, chapter = database
    module, owner = _owner()
    result = owner.apply(_request(module, session, chapter))
    assert result.context.chapter_experience_plan.question_hook == "Where?"
    assert result.chapter_snapshot["transient_overlay"] is True
    assert result.failure_reason == ""
    session.flush()
    session.expire(chapter)
    assert chapter.experience_plan_json == "{}"
    assert chapter.title == "誓约"


def test_chapter_patch_flushes_actual_plan_and_rebuilds_context(database):
    session, chapter = database
    module, owner = _owner()
    result = owner.apply(
        _request(
            module,
            session,
            chapter,
            scope="chapter_plan",
            design_patch={
                "chapter_plan_title": "新题",
                "chapter_goals": ["取证"],
                "chapter_task_contract": ["保持秘密"],
                "question_hook": "Who?",
            },
        )
    )
    assert chapter.title == "新题"
    assert json.loads(chapter.goals_json) == ["取证"]
    assert json.loads(chapter.task_contract_json) == ["保持秘密"]
    assert json.loads(chapter.experience_plan_json)["question_hook"] == "Who?"
    assert result.context.chapter_plan_title == "新题"
    assert result.chapter_snapshot["title"] == "新题"
    assert result.chapter_snapshot["transient_overlay"] is False
    session.rollback()
    assert session.get(ChapterPlan, "chapter").title == "誓约"


def test_band_patch_replaces_schedule_and_refreshes_current_and_future_only(database):
    session, chapter = database
    module, owner = _owner()
    chapter.chapter_number = 2
    session.add_all(
        [
            ChapterPlan(
                id="earlier",
                project_id="book",
                arc_plan_id="arc",
                chapter_number=1,
                title="Before",
            ),
            ChapterPlan(
                id="future",
                project_id="book",
                arc_plan_id="arc",
                chapter_number=3,
                title="After",
            ),
        ]
    )
    schedule = BandDelightSchedule(band_id="band", chapter_start=1, chapter_end=3)
    session.add(
        BandExperiencePlan(
            id="old-band",
            project_id="book",
            arc_id="arc",
            band_id="band",
            chapter_start=1,
            chapter_end=3,
            schedule_json=schedule.model_dump_json(),
        )
    )
    session.commit()
    result = owner.apply(
        _request(
            module,
            session,
            chapter,
            scope="band_plan",
            design_patch={"stall_guard_max_gap": 1},
        )
    )
    bands = list(session.scalars(select(BandExperiencePlan)))
    assert len(bands) == 1
    assert bands[0].id != "old-band"
    assert bands[0].stall_guard_max_gap == 1
    assert session.get(ChapterPlan, "earlier").experience_plan_json == "{}"
    assert json.loads(chapter.experience_plan_json)["question_hook"] == "rebuilt 2"
    assert (
        json.loads(session.get(ChapterPlan, "future").experience_plan_json)[
            "question_hook"
        ]
        == "rebuilt 3"
    )
    assert result.band_snapshot["schedule"]["stall_guard_max_gap"] == 1
    assert result.failure_reason == ""


def test_accepted_plan_patch_refuses_before_mutation(database):
    session, chapter = database
    module, owner = _owner()
    chapter.active_commit_id = "accepted"
    with pytest.raises(ValueError, match="isolated candidate revision"):
        owner.apply(_request(module, session, chapter, scope="chapter_plan"))
    assert chapter.experience_plan_json == "{}"
    assert chapter.title == "誓约"


def test_stale_countdown_repair_preserves_supplied_identity_and_bound_in_writer_input(
    database,
):
    session, chapter = database
    module, owner = _owner()
    diagnostic = (
        "港口潮汐倒计时 tide_gate 回溯旧倒计时为180分钟，"
        "accepted canon 当前为45分钟级别。"
    )

    result = owner.apply(
        _request(module, session, chapter, must_fix=[diagnostic])
    )

    anchors = result.context.chapter_experience_plan.rule_anchors
    assert len(anchors) == 1
    assert "tide_gate" in anchors[0]
    assert "180分钟" in anchors[0]
    assert "45分钟" in anchors[0]
    assert "小于等于45分钟" in anchors[0]
    prompt = _writer_prompt(result.context)
    assert diagnostic in prompt
    assert anchors[0] in prompt
    for invented_story_fact in ("记忆重置", "系统日志", "公开伪数据", "误导信息"):
        assert invented_story_fact not in anchors[0]
        assert invented_story_fact not in prompt


def test_non_monotonic_countdown_repairs_keep_multiple_clocks_separate(database):
    session, chapter = database
    module, owner = _owner()
    diagnostics = [
        "药效倒计时 med-A 从20分钟回升到45分钟，未给出 reset 或 branch clock 的正文证据。",
        "设备电量倒计时 battery-B 从12分钟延长到30分钟，未给出 reset 或 branch clock 的正文证据。",
    ]

    result = owner.apply(
        _request(module, session, chapter, must_fix=diagnostics)
    )

    anchors = result.context.chapter_experience_plan.rule_anchors
    assert len(anchors) == 2
    med_anchor = next(item for item in anchors if "med-A" in item)
    battery_anchor = next(item for item in anchors if "battery-B" in item)
    assert "battery-B" not in med_anchor
    assert "med-A" not in battery_anchor
    assert "20分钟" in med_anchor and "45分钟" in med_anchor
    assert "12分钟" in battery_anchor and "30分钟" in battery_anchor
    for anchor in anchors:
        assert "不得无证据回升或延长" in anchor
        assert "同一倒计时" in anchor
        assert "正文证据" in anchor


def test_countdown_repair_preserves_supplied_branch_evidence_without_rewriting_canon(
    database,
):
    session, chapter = database
    module, owner = _owner()
    diagnostic = (
        "港口潮汐倒计时 tide_gate 从18分钟回升到120分钟；"
        "正文已明确 tide_gate 切换到支线潮窗 branch clock tide_backup，"
        "并在数值变化前给出观测站证据。"
    )

    result = owner.apply(
        _request(module, session, chapter, must_fix=[diagnostic])
    )

    anchor = result.context.chapter_experience_plan.rule_anchors[0]
    assert diagnostic in anchor
    assert "tide_gate" in anchor and "tide_backup" in anchor
    assert "本条诊断已给出" in anchor
    assert "同一倒计时" in anchor
    assert "候选修复不得改写 accepted canon ledger" in anchor
    assert "120分钟必须改成小于等于18分钟" not in anchor


def test_non_countdown_repair_adds_no_countdown_rule(database):
    session, chapter = database
    module, owner = _owner()

    result = owner.apply(
        _request(
            module,
            session,
            chapter,
            must_fix=["港口潮汐窗口需要加重气味描写。"],
        )
    )

    assert result.context.chapter_experience_plan.rule_anchors == []
