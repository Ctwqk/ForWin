"""Run A regressions: exercise real pre-write feasibility and amendment verification."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from forwin.book_state import BookStateCompiler
from forwin.generation.pipeline_core.audit_control import AuditControlStage
from forwin.models.project import ChapterPlan
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.planning.amend import PlanAmender
from forwin.planning.band_planner.types import BandPlanOutcome
from forwin.protocol.book_state import ApprovedGraphDeltaSet, GraphDelta, NodePatch
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from tests.test_plan_step import Session
from tests.test_run_control_band_planning import Session as PgSession, _Stage
from tests.test_world_conflict_resolution import (
    _band_world, _plan_conflict, _amend, _conflict_row,
)
from tests.test_band_planner import _planner
from tests.test_arc_beat_sheet import StubLLM


@pytest.fixture(params=["sqlite", "postgres"])
def Database(request):
    return request.getfixturevalue("PgSession" if request.param == "postgres" else "Session")


class RealAuditStage(AuditControlStage, _Stage):
    def __init__(self, factory):
        super().__init__(band_planner=_planner(StubLLM()), arc_progression=None, events=[])
        self._SessionFactory = factory
        self.llm_client = None
        self._audit_task_id = self._audit_root_event_id = ""

    def _is_transient_llm_like(self, exc):
        return False

    def _make_state_helpers(self, session):
        return StateRepository(session), StateUpdater(session), SimpleNamespace()


def _execute(factory, world, chapter):
    stage = RealAuditStage(factory)
    with factory() as session:
        result = stage._run_project_chapters(
            session=session, repo=StateRepository(session), updater=StateUpdater(session), checker=SimpleNamespace(),
            project_id=world.project_id, chapter_numbers=[chapter], requested_chapters=1,
        )
    return stage, result


def _later_move(factory, world):
    with factory.begin() as session:
        result = BookStateCompiler(session).compile(ApprovedGraphDeltaSet(
            project_id=world.project_id, chapter_number=4, graph_deltas=[GraphDelta(
                id=f"later-move-{world.project_id}", project_id=world.project_id, chapter_number=4,
                node_patches=[NodePatch(node_id=world.hero, node_type="character", op="set",
                    field_path="state.location_id", old_value=world.loc(1), new_value=world.loc(3))],
            )],
        ))
        assert result.committed, result.blocked_reasons


def test_historical_rewrite_reaches_writer_with_real_prewrite_audit(Database):
    world, _, _ = _band_world(Database)
    _later_move(Database, world)
    with Database.begin() as session:
        for plan in session.scalars(select(ChapterPlan).where(ChapterPlan.project_id == world.project_id)):
            plan.status = "planned" if plan.chapter_number == 3 else "accepted"
    stage, result = _execute(Database, world, 3)
    assert stage.events == ["write:3:title-3"]
    assert result.failed_chapters == []
    with Database() as session:
        assert session.scalar(select(WorldPlanConflictRow).where(
            WorldPlanConflictRow.project_id == world.project_id)) is None


def test_prewrite_feasibility_block_pauses_without_failing_plan(Database):
    world, _, _ = _band_world(Database)
    with Database.begin() as session:
        plan = session.scalar(select(ChapterPlan).where(
            ChapterPlan.project_id == world.project_id, ChapterPlan.chapter_number == 1))
        plan.goals_json = json.dumps(["secret truth-1"])
    stage, result = _execute(Database, world, 1)
    assert result.failed_chapters == []
    assert result.paused_chapters == [1]
    assert result.pause_reason == "world_conflict"
    assert stage.events == []
    with Database() as session:
        plan = session.scalar(select(ChapterPlan).where(
            ChapterPlan.project_id == world.project_id, ChapterPlan.chapter_number == 1))
        conflict = session.scalar(select(WorldPlanConflictRow).where(
            WorldPlanConflictRow.project_id == world.project_id))
        assert plan.status == "planned"
        assert conflict.status == "open"
        pauses = [payload for _, payload in stage.progress if payload.get("stage") == "paused_for_review"]
        assert pauses[-1]["payload"]["conflict_id"] == conflict.id


class NoopPlanner:
    def __init__(self, band_id):
        self.band_id = band_id
        self.starts = []

    def replan_remaining(self, session, **kwargs):
        self.starts.append(kwargs["from_chapter"])
        return BandPlanOutcome(band_plan_id=self.band_id)


def test_amendment_cannot_resolve_without_replanning_blocked_chapter(Database):
    from forwin.world_engine.conflict_resolution import ConflictOptionUnavailable
    world, _, band_id = _band_world(Database)
    _later_move(Database, world)
    conflict_id = _plan_conflict(Database, world, band_plan_id=band_id)
    with Database.begin() as session:
        session.get(WorldPlanConflictRow, conflict_id).blocks_from_chapter = 3
    planner = NoopPlanner(band_id)
    with pytest.raises(ConflictOptionUnavailable):
        _amend(Database, world.project_id, conflict_id, PlanAmender(planner, None))
    assert planner.starts == [3]
    status, refs = _conflict_row(Database, world.project_id, conflict_id)
    assert status == "open"
    assert any(ref.startswith("amend_failed:") for ref in refs)


def test_amendment_reaudits_the_blocked_chapter(Database):
    from forwin.world_engine.conflict_resolution import ConflictOptionUnavailable
    world, _, band_id = _band_world(Database)
    with Database.begin() as session:
        plan = session.scalar(select(ChapterPlan).where(
            ChapterPlan.project_id == world.project_id, ChapterPlan.chapter_number == 1))
        plan.goals_json = json.dumps(["secret truth-1"])
        plan_id = plan.id
    conflict_id = _plan_conflict(Database, world, band_plan_id=band_id)

    class ClaimedPlanner(NoopPlanner):
        def replan_remaining(self, session, **kwargs):
            return BandPlanOutcome(band_plan_id=self.band_id, chapter_plan_ids=[plan_id])

    with pytest.raises(ConflictOptionUnavailable, match="writer_text_states_secret"):
        _amend(Database, world.project_id, conflict_id, PlanAmender(ClaimedPlanner(band_id), None))
    assert _conflict_row(Database, world.project_id, conflict_id)[0] == "open"


def test_real_amendment_resolves_only_after_blocked_plan_passes(Database):
    from tests.test_world_conflict_resolution import _amender
    from tests.test_arc_beat_sheet import StubLLM
    from tests.test_band_planner import _band
    world, _, band_id = _band_world(Database)
    conflict_id = _plan_conflict(Database, world, band_plan_id=band_id)
    result = _amend(Database, world.project_id, conflict_id, _amender(
        StubLLM(_band(world, range(1, 5), {3: {"milestone_ids": ["milestone-1"]}})), StubLLM(),
    ))
    assert result.band_plan_id != band_id
    assert _conflict_row(Database, world.project_id, conflict_id)[0] == "resolved"


def test_drafted_legacy_secret_plan_cannot_be_falsely_resolved(Database):
    from forwin.models.draft import ChapterDraft
    from forwin.world_engine.conflict_resolution import ConflictOptionUnavailable
    from tests.test_world_conflict_resolution import _amender
    from tests.test_arc_beat_sheet import StubLLM
    from tests.test_band_planner import _band
    world, _, band_id = _band_world(Database)
    with Database.begin() as session:
        plan = session.scalar(select(ChapterPlan).where(
            ChapterPlan.project_id == world.project_id, ChapterPlan.chapter_number == 1))
        plan.band_plan_id = None
        plan.goals_json = json.dumps(["secret truth-1"])
        session.add(ChapterDraft(chapter_plan_id=plan.id, body_text="legacy draft"))
    stage, result = _execute(Database, world, 1)
    assert result.paused_chapters == [1] and stage.events == []
    with Database() as session:
        conflict = session.scalar(select(WorldPlanConflictRow).where(
            WorldPlanConflictRow.project_id == world.project_id))
        conflict_id = conflict.id
    # An answer omitting the blocked legacy row must not falsely close its conflict.
    with pytest.raises(ConflictOptionUnavailable):
        _amend(Database, world.project_id, conflict_id, _amender(
            StubLLM(_band(world, range(2, 5), {3: {"milestone_ids": ["milestone-1"]}})), StubLLM(),
        ))
    status, refs = _conflict_row(Database, world.project_id, conflict_id)
    assert status == "open"
    assert any(ref.startswith("amend_failed:") for ref in refs)


@pytest.mark.parametrize("status", ["planned", "failed"])
@pytest.mark.parametrize("method", ["plan_band", "replan_remaining"])
def test_human_amendment_rewrites_only_blocked_legacy_chapter(Database, status, method):
    from forwin.models.draft import ChapterDraft
    from forwin.models.planning_v6 import BandPlanRow
    from tests.test_band_planner import _band, _plan_band, _plans
    from tests.test_world_conflict_resolution import _amender

    world, arc_id, _ = _band_world(Database)
    with Database.begin() as session:
        plans = _plans(session, world.project_id)
        ids = {number: plan.id for number, plan in plans.items()}
        for number in (1, 2):
            plans[number].band_plan_id = None
            session.add(ChapterDraft(chapter_plan_id=ids[number], body_text="legacy draft"))
        plans[1].status = status
        plans[1].goals_json = json.dumps(["secret truth-1"])
    # Automatic planning preserves both legacy rows despite their rewritable status.
    automatic = _plan_band(
        Database, _planner(StubLLM(_band(world, (3, 4), {3: {"milestone_ids": ["milestone-1"]}}))),
        world, arc_id, None,
    )
    if method == "plan_band":
        with Database.begin() as session:
            session.get(BandPlanRow, automatic.band_plan_id).status = "superseded"
    stage, result = _execute(Database, world, 1)
    assert result.paused_chapters == [1] and stage.events == []
    with Database() as session:
        conflict_id = session.scalar(select(WorldPlanConflictRow.id).where(
            WorldPlanConflictRow.project_id == world.project_id))
    result = _amend(Database, world.project_id, conflict_id, _amender(
        StubLLM(_band(world, (1, 3, 4), {3: {"milestone_ids": ["milestone-1"]}})), StubLLM(),
    ))
    assert result.band_plan_id
    assert _conflict_row(Database, world.project_id, conflict_id)[0] == "resolved"
    with Database() as session:
        plans = _plans(session, world.project_id)
        assert {number: plan.id for number, plan in plans.items()} == ids
        assert plans[1].band_plan_id == result.band_plan_id
        assert json.loads(plans[1].goals_json) == ["goal-1"]
        assert plans[2].band_plan_id is None
        assert len(list(session.scalars(select(ChapterDraft).where(
            ChapterDraft.chapter_plan_id.in_([ids[1], ids[2]]))))) == 2
    stage, result = _execute(Database, world, 1)
    assert stage.events == ["write:1:title-1"]
    assert result.paused_chapters == result.failed_chapters == []


@pytest.mark.parametrize("method", ["plan_band", "replan_remaining"])
@pytest.mark.parametrize("status", ["accepted", "drafted", "needs_review"])
def test_legacy_amendment_preserves_rows_taken_by_writer(Database, method, status):
    from forwin.models.draft import ChapterDraft
    from forwin.models.project import ArcPlanVersion
    from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
    from forwin.planning.beat_sheet.types import BandSpan
    from tests.test_band_planner import POLICY, HookedLLM, _band, _plans

    world, arc_id, band_id = _band_world(Database)
    with Database.begin() as session:
        plans = _plans(session, world.project_id)
        for number, protected_status in ((2, "accepted"), (3, "drafted"), (4, "needs_review")):
            plans[number].status = protected_status
        plans[1].status = "planned"
        plans[1].band_plan_id = None
        session.add(ChapterDraft(chapter_plan_id=plans[1].id, body_text="legacy draft"))
    with Database.begin() as session:
        plan = _plans(session, world.project_id)[1]

        def writer_takes_row():
            plan.status = status
            session.flush()

        planner = _planner(HookedLLM(writer_takes_row, _band(world, (1,), {1: {"title": "amended"}})))
        arguments = {"project_id": world.project_id, "policy": POLICY, "amend_chapters": frozenset({1, 2, 3, 4})}
        if method == "plan_band":
            outcome = planner.plan_band(
                session, **arguments, arc_plan=session.get(ArcPlanVersion, arc_id),
                beat_sheet_row=ArcBeatSheetRepository(session).active_for_arc(arc_id),
                band=BandSpan(band_index=0, chapter_start=1, chapter_end=4),
            )
        else:
            outcome = planner.replan_remaining(session, **arguments, band_plan_id=band_id, from_chapter=1, notes=())
        assert planner.llm_client.calls
        assert outcome.chapter_plan_ids == []
        session.refresh(plan)
        assert plan.status == status
        assert plan.title == "title-1" and plan.band_plan_id is None
        plans = _plans(session, world.project_id)
        assert [plans[number].status for number in (2, 3, 4)] == ["accepted", "drafted", "needs_review"]
        assert [plans[number].title for number in (2, 3, 4)] == ["title-2", "title-3", "title-4"]
