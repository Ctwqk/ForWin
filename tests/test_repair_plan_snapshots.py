from types import SimpleNamespace

from forwin.generation.pipeline_core.review_autofix import ReviewWorkflowStage
from forwin.models.project import ChapterPlan
from forwin.protocol.experience import BandDelightSchedule, ChapterExperiencePlan


def test_repair_snapshots_capture_current_chapter_and_band_plan_columns() -> None:
    plan = ChapterPlan(
        chapter_number=2, title="Arrival", one_line="Cross the bridge", goals_json='["Arrive"]',
        world_bindings_json='[{"kind": "intent", "ref": "intent-1", "how": "Cross"}]',
        reveal_directives_json='[{"id": "reveal-1"}]',
        intended_changes_json='[{"description": "Arrive"}]', hook="Find the door",
    )
    schedule = BandDelightSchedule(band_id="band:2:3", chapter_start=2, chapter_end=3)
    repo = SimpleNamespace(
        get_chapter_experience_plan=lambda *_: ChapterExperiencePlan(),
        get_band_row_for_chapter=lambda *_: None,
        get_band_experience_plan_for_chapter=lambda *_: schedule,
        get_chapter_plan=lambda _project_id, number: plan if number == 2 else None,
    )
    stage = ReviewWorkflowStage()
    chapter = stage._chapter_plan_snapshot(repo=repo, project_id="project-1", chapter_plan=plan)
    band = stage._band_plan_snapshot(repo=repo, project_id="project-1", chapter_number=2)

    assert chapter["plan_columns"] == {
        "world_bindings": [{"kind": "intent", "ref": "intent-1", "how": "Cross"}],
        "reveal_directives": [{"id": "reveal-1"}],
        "intended_changes": [{"description": "Arrive"}],
        "hook": "Find the door",
    }
    assert band["plan_columns"] == {"2": chapter["plan_columns"]}
    assert band["schedule"]["band_id"] == schedule.band_id
