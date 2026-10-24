"""Database-free regression checks for Task 9 run-control fixes."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from forwin.generation import continue_workset
from forwin.generation.pipeline_core.run_control import RunControlStage
from forwin.planning.arc_progression import ArcProgressionService


def test_workset_skips_accepted_chapters_after_rewrite_gap(monkeypatch):
    session = Mock()
    session.get.return_value = SimpleNamespace(creation_status="writing")
    monkeypatch.setattr(continue_workset, "writable_chapter_ranges", lambda *_: {"book": range(1, 7)})
    plans = [SimpleNamespace(chapter_number=n, status="planned" if n == 2 else "accepted") for n in range(1, 5)]
    result = continue_workset.build_continue_generation_workset(session, "book", preloaded_plans=plans)
    assert result.chapter_numbers == (2, 5, 6)
    assert continue_workset.build_continue_generation_workset(
        session, "book", preloaded_plans=plans, max_chapters=2,
    ).chapter_numbers == (2, 5)


def test_activation_recovers_when_active_arc_is_behind():
    session = Mock()
    arcs = [SimpleNamespace(id=str(n), chapter_start=start, chapter_end=start + 5, status=status)
            for n, start, status in [(1, 1, "active"), (2, 7, "planned"), (3, 13, "planned")]]
    session.scalars.return_value = arcs
    result = ArcProgressionService._activate(session, project_id="book", after_chapter=12)
    assert result is arcs[2]
    assert [arc.status for arc in arcs] == ["completed", "completed", "active"]


def test_activation_never_moves_backwards():
    session = Mock()
    arcs = [SimpleNamespace(chapter_start=1, chapter_end=6, status="planned"),
            SimpleNamespace(chapter_start=7, chapter_end=12, status="active")]
    session.scalars.return_value = arcs
    assert ArcProgressionService._activate(session, project_id="book", after_chapter=2) is None
    assert [arc.status for arc in arcs] == ["planned", "active"]


@pytest.mark.parametrize("error_type", [KeyError, IndexError])
def test_planner_programming_error_is_not_retried(monkeypatch, error_type):
    from forwin.generation.pipeline_core import run_control
    session = Mock()
    repo = Mock()
    repo.get_active_arc_plan.return_value = SimpleNamespace(chapter_start=1, chapter_end=6)
    monkeypatch.setattr(run_control, "StateRepository", lambda _: repo)
    stage = RunControlStage()
    stage.band_planner = Mock()
    stage.band_planner.ensure_plan_for_chapter.side_effect = error_type("audit bug")
    stage.arc_progression = Mock()
    with pytest.raises(error_type):
        stage._ensure_chapter_plan(session=session, project_id="book", chapter_number=1, policy=Mock())
    assert stage.band_planner.ensure_plan_for_chapter.call_count == 1
    session.rollback.assert_not_called()
    stage.arc_progression.ensure_beat_sheet.assert_not_called()


@pytest.mark.parametrize("status", ["planned", "failed"])
@pytest.mark.parametrize("history", ["draft", "candidate"])
def test_old_plan_with_history_does_not_need_arc_or_model(status, history):
    from forwin.models.project import ChapterPlan
    from forwin.planning.band_planner.service import BandPlanner
    session = Mock()
    plan = ChapterPlan(id="old-plan", project_id="book", chapter_number=3, band_plan_id=None, status=status)
    session.scalar.side_effect = [plan, "draft-id"] if history == "draft" else [plan, None, "candidate-id"]
    client = Mock()
    planner = BandPlanner(None, feasibility=Mock(), save_trace=Mock())
    planner.llm_client = client
    assert planner.ensure_plan_for_chapter(session, project_id="book", chapter_number=3, policy=Mock()) is plan
    client.chat.assert_not_called()
    planner.save_trace.assert_not_called()
    assert session.scalar.call_count == (2 if history == "draft" else 3)


@pytest.mark.parametrize("plan_bands", [True, False])
def test_missing_plan_without_conflict_has_chapter_unplanned_reason(monkeypatch, plan_bands):
    from forwin.generation.pipeline_core import project_chapters
    from tests.test_run_control_band_planning import _Stage
    conflicts = Mock()
    conflicts.open_blocking.return_value = None
    monkeypatch.setattr(project_chapters, "WorldConflictRepository", lambda _: conflicts)
    stage = _Stage(band_planner=Mock(), arc_progression=Mock(), events=[])
    stage._ensure_chapter_plan = Mock(return_value=None)
    repo = Mock()
    repo.get_chapter_plan.return_value = None
    result = stage._run_project_chapters(session=Mock(), repo=repo, updater=Mock(), checker=Mock(),
                                        project_id="book", chapter_numbers=[1], requested_chapters=1, plan_bands=plan_bands)
    assert (result.pause_reason, result.paused_chapters) == ("chapter_unplanned", [1])
    payloads = [payload["payload"] for _, payload in stage.progress if payload.get("stage") == "paused_for_review"]
    assert payloads == [{"reason": "chapter_unplanned"}]
    assert stage.events == []


def test_chapter_loop_defensively_skips_accepted_plans(monkeypatch):
    from forwin.generation.pipeline_core import project_chapters
    from tests.test_run_control_band_planning import _Stage
    conflicts = Mock()
    conflicts.open_blocking.return_value = None
    monkeypatch.setattr(project_chapters, "WorldConflictRepository", lambda _: conflicts)
    stage = _Stage(band_planner=Mock(), arc_progression=Mock(), events=[])
    stage._ensure_chapter_plan = Mock(return_value=SimpleNamespace(status="accepted"))
    stage._strict_progression_block = Mock(side_effect=AssertionError("accepted chapters must not enter writing"))
    result = stage._run_project_chapters(session=Mock(), repo=Mock(), updater=Mock(), checker=Mock(),
                                        project_id="book", chapter_numbers=[3, 4], requested_chapters=2)
    assert result.failed_chapters == result.paused_chapters == []
    stage._strict_progression_block.assert_not_called()
    assert stage.events == []


def test_project_without_arcs_has_no_writable_range():
    session = Mock()
    session.execute.return_value = Mock(all=lambda: [])
    assert continue_workset.writable_chapter_ranges(session, ["book"]) == {}


@pytest.mark.parametrize("arc_row", [("book", 1, 0, 3), ("book", 8, 0, 3)])
def test_degenerate_arc_range_writes_nothing_and_warns(arc_row, caplog):
    session = Mock()
    session.execute.return_value = Mock(all=lambda: [arc_row])
    with caplog.at_level("WARNING", logger="forwin.generation.continue_workset"):
        ranges = continue_workset.writable_chapter_ranges(session, ["book"])
    assert list(ranges["book"]) == []
    assert "empty arc chapter range" in caplog.text


def test_historical_plan_before_active_arc_bypasses_activation_and_planning(monkeypatch):
    from forwin.generation.pipeline_core import run_control
    repo = Mock()
    repo.get_active_arc_plan.return_value = SimpleNamespace(chapter_start=7, chapter_end=12)
    original = SimpleNamespace(status="planned", title="original")
    repo.get_chapter_plan.return_value = original
    monkeypatch.setattr(run_control, "StateRepository", lambda _: repo)
    stage = RunControlStage()
    stage.band_planner, stage.arc_progression = Mock(), Mock()
    assert stage._ensure_chapter_plan(session=Mock(), project_id="book", chapter_number=3, policy=Mock()) is original
    stage.band_planner.ensure_plan_for_chapter.assert_not_called()
    stage.arc_progression.activate_next_arc_if_needed.assert_not_called()


def test_planning_neighbor_keeps_old_plan_with_draft_history():
    from forwin.models.project import ChapterPlan
    from forwin.planning.band_planner.service import _rewritable
    from forwin.planning.beat_sheet.types import BandSpan
    session = Mock()
    session.scalar.return_value = "previous-draft"
    plan = ChapterPlan(id="old-plan", project_id="book", chapter_number=2, band_plan_id=None, status="planned")
    assert _rewritable(session, BandSpan(band_index=1, chapter_start=1, chapter_end=3), {2: plan}, from_chapter=1) == [1, 3]
