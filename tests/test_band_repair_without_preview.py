from types import SimpleNamespace
from unittest.mock import Mock

from forwin.protocol.experience import BandDelightSchedule, ChapterExperiencePlan
from forwin.protocol.review import RepairInstruction
from forwin.protocol.writer import WriterOutput
from forwin.review.repair.service import _apply_repair_patch


def test_band_schedule_repair_commits_without_preview_callback() -> None:
    schedule = BandDelightSchedule(
        band_id="arc-1:band:1",
        chapter_start=1,
        chapter_end=4,
    )
    rebuilt_context = object()
    replace_band_schedule = Mock()
    execution = SimpleNamespace(
        retrieval_broker=SimpleNamespace(
            build_chapter_context=Mock(return_value=rebuilt_context)
        ),
        _band_schedule_patch_payload=Mock(
            return_value=schedule.model_dump(mode="python")
        ),
        _replace_band_schedule=replace_band_schedule,
        _chapter_plan_snapshot=Mock(return_value={"chapter_number": 2}),
        _band_plan_snapshot=Mock(return_value={"band_id": schedule.band_id}),
    )
    repo = SimpleNamespace(
        get_chapter_experience_plan=Mock(return_value=ChapterExperiencePlan()),
        get_band_experience_plan_for_chapter=Mock(return_value=schedule),
        get_latest_arc_structure_draft=Mock(return_value=None),
    )
    session = Mock()
    chapter_plan = SimpleNamespace(chapter_number=2)
    instruction = RepairInstruction(
        repair_scope="band_plan",
        failure_type="continuity",
        design_patch={"stall_guard_max_gap": 1},
    )

    result = _apply_repair_patch(
        execution,
        session=session,
        repo=repo,
        project_id="project-1",
        chapter_plan=chapter_plan,
        context=object(),
        current_output=WriterOutput(
            project_id="project-1",
            chapter_number=2,
            title="Chapter 2",
            body="Draft",
            end_of_chapter_summary="Summary",
        ),
        repair_scope="band_plan",
        repair_instruction=instruction,
    )

    replace_band_schedule.assert_called_once()
    session.flush.assert_called_once_with()
    assert result[1] is rebuilt_context
    assert result[4] == ""
