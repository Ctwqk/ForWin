from types import SimpleNamespace

from sqlalchemy import select

from forwin.models.phase import BandExperiencePlan
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.experience import BandDelightSchedule, ChapterExperiencePlan
from forwin.protocol.review import RepairInstruction
from forwin.retrieval import RetrievalBroker
from forwin.review.repair.plan_patch import (
    RepairPlanPatchRequest,
    RepairPlanPatchService,
)
from forwin.state.repo import StateRepository
from tests.test_candidate_review_owner import database as _shared_database

database = _shared_database


def test_band_schedule_repair_commits_without_preview_callback(database) -> None:
    session, chapter = database
    schedule = BandDelightSchedule(band_id="band", chapter_start=1, chapter_end=1)
    session.add(
        BandExperiencePlan(
            id="old",
            project_id="book",
            arc_id="arc",
            band_id="band",
            chapter_start=1,
            chapter_end=1,
            schedule_json=schedule.model_dump_json(),
        )
    )
    session.commit()
    rebuilt_context = ChapterContextPack(
        project_id="book", project_title="Book", premise="Fixture", genre="fantasy",
        setting_summary="", chapter_number=1, chapter_plan_title=chapter.title,
        chapter_plan_one_line="", chapter_goals=[],
    )
    broker = RetrievalBroker(context_budget_chars=50_000)
    broker.build_chapter_context = lambda *_args: rebuilt_context
    owner = RepairPlanPatchService(
        retrieval_broker=broker,
        arc_envelope_manager=SimpleNamespace(
            _derive_chapter_experience_plan=lambda **_kwargs: ChapterExperiencePlan()
        ),
    )
    result = owner.apply(
        RepairPlanPatchRequest(
            session=session,
            repo=StateRepository(session),
            project_id="book",
            chapter_plan=chapter,
            context=rebuilt_context,
            repair_scope="band_plan",
            instruction=RepairInstruction(
                repair_scope="band_plan",
                failure_type="continuity",
                design_patch={"stall_guard_max_gap": 1},
            ),
        )
    )
    rows = list(session.scalars(select(BandExperiencePlan)))
    assert len(rows) == 1
    assert rows[0].id != "old"
    assert rows[0].stall_guard_max_gap == 1
    assert result.context.model_dump(exclude={"repair_contract", "context_budget_summary"}) == (
        rebuilt_context.model_dump(exclude={"repair_contract", "context_budget_summary"})
    )
    assert result.context.context_budget_summary["rendered_context_chars"] == broker._estimate_chars(result.context)
    assert result.context.context_budget_summary["soft_budget_exceeded"] is False
    assert result.failure_reason == ""
