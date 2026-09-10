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


def _request(module, session, chapter, *, scope="draft", design_patch=None):
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
            must_fix=["补充证据"],
            design_patch=design_patch or {"question_hook": "Where?"},
        ),
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
