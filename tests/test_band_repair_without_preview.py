from types import SimpleNamespace

from sqlalchemy import select

from forwin.models.phase import BandExperiencePlan
from forwin.protocol.experience import BandDelightSchedule, ChapterExperiencePlan
from forwin.protocol.review import RepairInstruction
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
    rebuilt_context = object()
    owner = RepairPlanPatchService(
        retrieval_broker=SimpleNamespace(
            build_chapter_context=lambda *_args: rebuilt_context
        ),
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
            context=None,
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
    assert result.context is rebuilt_context
    assert result.failure_reason == ""
