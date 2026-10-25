"""PostgreSQL publication races for chapter milestone amendments."""

import json
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from forwin.api_schema.world_conflicts import WorldConflictResolveRequest
from forwin.http.adapters.api_world_conflict_routes import build_handlers
from forwin.models.book_state import GraphDeltaRow
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import ChapterDraft
from forwin.models.genesis import PromptTrace
from forwin.models.planning_v6 import ArcBeatSheetRow, BandPlanRow
from forwin.models.project import ChapterPlan, Project
from forwin.models.task import GenerationTask
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.planning.band_planner.types import PlanIssue
from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
from forwin.planning.plan_step import PlanStepService
from tests.test_arc_beat_sheet import POLICY, StubLLM, _book, _ensure, _service, _sheet
from tests.test_band_planner import StubAudit, _band, _plan_band, _planner
from tests.test_world_conflict_resolution import _amender, _chapter_conflict, _chapter_violation, _session_factory


@pytest.fixture
def Session():
    engine, factory = _session_factory()
    yield factory
    engine.dispose()


def _paused_book(Session):
    world, arc_id = _book(Session)
    sheet_id = _ensure(Session, _service(StubLLM(_sheet(world))), world, arc_id)
    for index, start, end in ((0, 1, 6), (1, 7, 12)):
        outcome = _plan_band(Session, _planner(StubLLM(_band(world, range(start, end + 1)))),
                             world, arc_id, sheet_id, band=(index, start, end))
        assert outcome.band_plan_id is not None
    conflict_id = _chapter_conflict(Session, world.project_id, _chapter_violation(
        protection_node_id="", milestone_ref=world.hero, kind="milestone", ref=world.hero,
        field="state.alive_status", value="dead", impact_level="critical",
    ), chapter=3)
    with Session.begin() as session:
        plan = session.scalar(select(ChapterPlan).where(
            ChapterPlan.project_id == world.project_id, ChapterPlan.chapter_number == 3))
        session.add(ChapterDraft(chapter_plan_id=plan.id, body_text="Saved chapter body."))
    return world, arc_id, sheet_id, conflict_id


def _snapshot(Session, project_id):
    with Session() as session:
        return (
            list(session.execute(select(ArcBeatSheetRow.id, ArcBeatSheetRow.status).where(
                ArcBeatSheetRow.project_id == project_id).order_by(ArcBeatSheetRow.id))),
            list(session.scalars(select(GraphDeltaRow.id).where(
                GraphDeltaRow.project_id == project_id).order_by(GraphDeltaRow.id))),
        )


@pytest.mark.parametrize("winner", ["rewrite_chapter", "active_task"])
def test_milestone_amendment_loses_model_gap_without_publishing(Session, winner):
    world, arc_id, sheet_id, conflict_id = _paused_book(Session)
    before = _snapshot(Session, world.project_id)
    entered, release = threading.Event(), threading.Event()

    class BlockingLLM(StubLLM):
        def chat(self, messages, **kwargs):
            entered.set()
            assert release.wait(10), "model gap was never released"
            return super().chat(messages, **kwargs)

    client = BlockingLLM(_sheet(world, bands=((1, 4), (5, 8), (9, 12))))
    handler = build_handlers(get_session=Session, get_plan_amender=lambda: _amender(StubLLM(), client))[
        "resolve_project_world_conflict"]
    outcomes = []

    def amend():
        try:
            outcomes.append(handler(world.project_id, conflict_id, WorldConflictResolveRequest(
                option="amend_plan", rationale="amend milestone", reason="milestone decision")))
        except Exception as exc:
            outcomes.append(exc)

    thread = threading.Thread(target=amend, daemon=True)
    thread.start()
    try:
        assert entered.wait(10), outcomes
        # All three rows must be available while the model is blocked.
        with Session.begin() as other:
            other.execute(select(WorldPlanConflictRow.id).where(WorldPlanConflictRow.id == conflict_id)
                          .with_for_update(nowait=True))
            other.execute(select(Project.id).where(Project.id == world.project_id)
                          .with_for_update(key_share=True, nowait=True))
            other.execute(select(ChapterPlan.id).where(ChapterPlan.project_id == world.project_id,
                          ChapterPlan.chapter_number == 3).with_for_update(nowait=True))
            if winner == "active_task":
                other.add(GenerationTask(id="racing-task", project_id=world.project_id, status="running"))
        if winner == "rewrite_chapter":
            resolved = handler(world.project_id, conflict_id, WorldConflictResolveRequest(option="rewrite_chapter", rationale="rewrite", reason="rewrite decision"))
            assert resolved.status == "resolved"
    finally:
        release.set()
        thread.join(15)
    assert not thread.is_alive()
    assert len(outcomes) == 1 and isinstance(outcomes[0], HTTPException), outcomes
    assert outcomes[0].status_code == 409
    assert len(client.calls) == 1
    assert _snapshot(Session, world.project_id) == before
    with Session() as session:
        row = session.get(WorldPlanConflictRow, conflict_id)
        assert json.loads(row.result_refs_json) == []
        assert row.status == ("resolved" if winner == "rewrite_chapter" else "open")
        assert ArcBeatSheetRepository(session).active_for_arc(arc_id).id == sheet_id
        traces = session.scalars(select(PromptTrace).where(PromptTrace.project_id == world.project_id)).all()
        assert any(json.loads(trace.input_snapshot_json).get("reason") == f"conflict:{conflict_id}"
                   for trace in traces)


@pytest.mark.parametrize("audit_fails", [False, True], ids=["clean_audit", "failing_audit"])
def test_milestone_revision_preserves_bands_through_future_planning(Session, audit_fails):
    world, arc_id, _, conflict_id = _paused_book(Session)
    with Session() as session:
        plans = session.scalars(select(ChapterPlan).where(ChapterPlan.project_id == world.project_id)).all()
        saved = {row.chapter_number: (row.id, row.title, row.band_plan_id) for row in plans}
        bands = list(session.execute(select(BandPlanRow.id, BandPlanRow.chapter_start, BandPlanRow.chapter_end)
                                    .where(BandPlanRow.project_id == world.project_id)))
    client = StubLLM(_sheet(world, bands=((1, 4), (5, 8), (9, 12))))
    handler = build_handlers(get_session=Session, get_plan_amender=lambda: _amender(StubLLM(), client))[
        "resolve_project_world_conflict"]
    result = handler(world.project_id, conflict_id, WorldConflictResolveRequest(option="amend_plan", rationale="amend", reason="amend decision"))
    assert result.status == "resolved" and len(client.calls) == 1
    with Session() as session:
        repo = ArcBeatSheetRepository(session)
        sheet = repo.load(repo.active_for_arc(arc_id))
        assert [(b.chapter_start, b.chapter_end) for b in sheet.band_breakdown] == [(1, 6), (7, 12)]
        for band_id, start, end in bands:
            row = session.get(BandPlanRow, band_id)
            assert row.status == "active" and (row.chapter_start, row.chapter_end) == (start, end)
        plans = session.scalars(select(ChapterPlan).where(ChapterPlan.project_id == world.project_id)).all()
        assert {p.chapter_number: (p.id, p.title, p.band_plan_id) for p in plans} == saved
        trace = next(t for t in session.scalars(select(PromptTrace).where(PromptTrace.project_id == world.project_id))
                     if json.loads(t.input_snapshot_json).get("reason") == f"conflict:{conflict_id}")
        assert "band_breakdown_restored" in json.loads(trace.output_summary_json)["normalizations"]
    # Model acceptance of the saved chapter; exercise the real subsequent planning stages.
    with Session.begin() as session:
        for number in (1, 2, 3):
            session.get(ChapterPlan, saved[number][0]).status = "accepted"
    errors = [PlanIssue(code="onstage_unavailable", severity="error", chapter_number=4, message="unavailable")]
    planner_client = StubLLM(*([_band(world, range(4, 7))] if audit_fails else []), _band(world, range(7, 13)))
    planner = _planner(planner_client)
    step = PlanStepService(planner, StubAudit(errors if audit_fails else []), SimpleNamespace())
    with Session() as session:
        outcome = step.run(session, CanonCommitRecord(project_id=world.project_id, chapter_number=3), policy=POLICY)
        assert bool(outcome["replanned_band_ids"]) == audit_fails
        assert not outcome["conflict_ids"]
        assert planner.ensure_plan_for_chapter(session, project_id=world.project_id, chapter_number=4,
                                               policy=POLICY).id == saved[4][0]
        step.run(session, CanonCommitRecord(project_id=world.project_id, chapter_number=6), policy=POLICY)
        active = session.scalars(select(BandPlanRow).where(BandPlanRow.project_id == world.project_id,
                                                         BandPlanRow.status == "active")).all()
        assert sorted((b.chapter_start, b.chapter_end) for b in active) == [(1, 6), (7, 12)]
        third = session.get(ChapterPlan, saved[3][0])
        assert (third.id, third.title, third.band_plan_id) == saved[3]
        assert session.scalar(select(ChapterDraft.body_text).where(ChapterDraft.chapter_plan_id == third.id)) == "Saved chapter body."
        for number in range(4, 13):
            plan = planner.ensure_plan_for_chapter(session, project_id=world.project_id, chapter_number=number,
                                                   policy=POLICY)
            owner = session.get(BandPlanRow, plan.band_plan_id)
            assert owner.status == "active" and owner.chapter_start <= number <= owner.chapter_end
    assert [planner_client.payload(i)["band"] for i in range(len(planner_client.calls))] == (
        ([{"band_index": 0, "chapter_start": 1, "chapter_end": 6}] if audit_fails else [])
        + [{"band_index": 1, "chapter_start": 7, "chapter_end": 12}])
