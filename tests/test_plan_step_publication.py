"""Deterministic publication interleavings; PostgreSQL cases use two real sessions."""

from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import select

from forwin.canon.admission import CanonAdmissionService  # noqa: F401 - initialize canon before maintenance
from forwin.maintenance.post_canon import PostCanonLeaseLost
from forwin.models.canon import CanonCommitRecord
from forwin.models.genesis import PromptTrace
from forwin.models.maintenance import PostCanonMaintenanceRun
from forwin.models.planning_v6 import BandPlanRow
from forwin.models.project import ChapterPlan
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
from forwin.planning.beat_sheet.types import ArcBeatSheetData
from forwin.planning.feasibility import FeasibilityAuditor
from tests.test_arc_beat_sheet import StubLLM
from tests.test_band_planner import POLICY, HookedLLM, StubAudit, _band, _book, _planner
from tests.test_plan_step import (
    PgSession, Session, _error, _maintenance, _run, _seed_plans, _service,
)


@pytest.fixture(params=["sqlite", "postgres"])
def RaceSession(request):
    return request.getfixturevalue("PgSession" if request.param == "postgres" else "Session")


def _versions(session, world, index):
    return list(session.scalars(select(BandPlanRow).where(
        BandPlanRow.project_id == world.project_id, BandPlanRow.band_index == index,
    ).order_by(BandPlanRow.version)))


def _traces(session, world):
    return [json.loads(row.output_summary_json) for row in session.scalars(select(PromptTrace).where(
        PromptTrace.project_id == world.project_id, PromptTrace.stage_key == "plan.band",
    ))]


@pytest.mark.parametrize("window", ["before_entry", "during_model"])
@pytest.mark.parametrize("winner", ["plan_step", "run_control"])
def test_next_band_publication_keeps_first_winner(RaceSession, monkeypatch, window, winner):
    world, _, _ = _book(RaceSession)
    _seed_plans(RaceSession, world, {4: "accepted"})
    winner_ids = []

    def publish_winner():
        client = StubLLM(_band(world, range(5, 9), {5: {"title": "winning chapter"}}))
        if winner == "plan_step":
            winner_ids.append(_run(RaceSession, _service(client), world, 4)["planned_band_id"])
        else:
            with RaceSession.begin() as other:
                plan = _planner(client).ensure_plan_for_chapter(
                    other, project_id=world.project_id, chapter_number=5, policy=POLICY,
                )
                winner_ids.append(plan.band_plan_id)

    llm = (HookedLLM(publish_winner, _band(world, range(5, 9))) if window == "during_model"
           else StubLLM(_band(world, range(5, 9))))
    service = _service(llm)
    if window == "before_entry":
        original = service.band_planner.plan_band

        def after_missing_check(session, **kwargs):
            assert kwargs["expected_band_plan_id"] is None
            publish_winner()
            return original(session, **kwargs)

        monkeypatch.setattr(service.band_planner, "plan_band", after_missing_check)
    result = _run(RaceSession, service, world, 4)
    assert result["planned_band_id"] == winner_ids[0]
    with RaceSession() as session:
        version, = _versions(session, world, 1)
        assert version.id == winner_ids[0] and version.status == "active"
        plan = session.scalar(select(ChapterPlan).where(
            ChapterPlan.project_id == world.project_id, ChapterPlan.chapter_number == 5,
        ))
        assert plan.title == "winning chapter" and plan.band_plan_id == version.id
        assert sum(trace["superseded_by_concurrent_plan"] for trace in _traces(session, world)) == 1


def test_run_control_loses_to_plan_step_during_model(RaceSession):
    world, _, _ = _book(RaceSession)
    _seed_plans(RaceSession, world, {4: "accepted"})
    winners = []

    def publish_winner():
        winners.append(_run(RaceSession, _service(StubLLM(_band(world, range(5, 9)))), world, 4))

    with RaceSession.begin() as session:
        plan = _planner(HookedLLM(publish_winner, _band(world, range(5, 9)))).ensure_plan_for_chapter(
            session, project_id=world.project_id, chapter_number=5, policy=POLICY,
        )
        assert plan.band_plan_id == winners[0]["planned_band_id"]
    with RaceSession() as session:
        assert len(_versions(session, world, 1)) == 1


def test_lease_takeover_before_planner_entry_keeps_succeeded_result(RaceSession, monkeypatch):
    world, _, _ = _book(RaceSession)
    ids, _ = _seed_plans(RaceSession, world, {4: "accepted"})
    llm = StubLLM(_band(world, range(5, 9), {5: {"title": "replacement worker"}}),
                  _band(world, range(5, 9)))
    service = _service(llm)
    maintenance, commit_id = _maintenance(RaceSession, world, ids[4], 4, llm, service)
    now = [datetime.now(timezone.utc)]
    maintenance._clock = lambda: now[0]
    original = service.band_planner.plan_band
    entered = []

    def after_missing_check(session, **kwargs):
        if not entered:
            entered.append(True)
            assert kwargs["expected_band_plan_id"] is None
            now[0] += timedelta(seconds=601)
            maintenance.run(canon_commit_id=commit_id, worker_id="replacement")
        return original(session, **kwargs)

    monkeypatch.setattr(service.band_planner, "plan_band", after_missing_check)
    with pytest.raises(PostCanonLeaseLost):
        maintenance.run(canon_commit_id=commit_id, worker_id="stale")
    with RaceSession() as session:
        run = session.scalar(select(PostCanonMaintenanceRun).where(
            PostCanonMaintenanceRun.canon_commit_id == commit_id, PostCanonMaintenanceRun.step_name == "plan",
        ))
        version, = _versions(session, world, 1)
        assert (run.status, run.lease_epoch) == ("succeeded", 2)
        assert json.loads(run.result_json)["planned_band_id"] == version.id
        assert version.status == "active"
        assert session.scalar(select(ChapterPlan.title).where(
            ChapterPlan.project_id == world.project_id, ChapterPlan.chapter_number == 5,
        )) == "replacement worker"


@pytest.mark.parametrize("window", ["before_entry", "during_model", "failed_model"])
def test_stale_audit_defers_without_exceeding_cap(RaceSession, monkeypatch, window):
    world, _, _ = _book(RaceSession)
    _, old_id = _seed_plans(RaceSession, world, {1: "accepted", 2: "planned", 3: "planned", 4: "planned"})
    policy = POLICY.model_copy(update={"planning": POLICY.planning.model_copy(update={"max_replans_per_band": 1})})
    winners = []

    def publish_winner():
        if winners:
            return
        with RaceSession.begin() as other:
            winners.append(_planner(StubLLM(_band(world, range(2, 5), {2: {"title": "winning repair"}})))
                           .replan_remaining(other, project_id=world.project_id, band_plan_id=old_id,
                                             expected_band_plan_id=old_id, from_chapter=2, policy=policy, notes=[]))

    responses = ([RuntimeError("provider unavailable")] * 2 if window == "failed_model"
                 else [_band(world, range(2, 5))])
    llm = (StubLLM(*responses) if window == "before_entry" else HookedLLM(publish_winner, *responses))
    service = _service(llm, StubAudit([_error(2)]))
    if window == "before_entry":
        original = service.band_planner.replan_remaining

        def after_cap_decision(session, **kwargs):
            assert kwargs["expected_band_plan_id"] == old_id
            publish_winner()
            return original(session, **kwargs)

        monkeypatch.setattr(service.band_planner, "replan_remaining", after_cap_decision)
    with RaceSession() as session:
        result = service.run(session, CanonCommitRecord(project_id=world.project_id, chapter_number=1), policy=policy)
    assert result["replanned_band_ids"] == result["conflict_ids"] == []
    assert result["band_planning_deferred"] == [
        {"band": [1, 4], "reason": "superseded_by_concurrent_plan", "issue_codes": []},
    ]
    with RaceSession() as session:
        versions = _versions(session, world, 0)
        assert [row.replan_count for row in versions] == [0, 1]
        assert versions[-1].id == winners[0].band_plan_id
        assert session.scalar(select(ChapterPlan.title).where(
            ChapterPlan.project_id == world.project_id, ChapterPlan.chapter_number == 2,
        )) == "winning repair"
        assert list(session.scalars(select(WorldPlanConflictRow).where(
            WorldPlanConflictRow.project_id == world.project_id,
        ))) == []


def test_publication_rechecks_cap_and_human_override_remains(RaceSession):
    world, _, _ = _book(RaceSession)
    _, old_id = _seed_plans(RaceSession, world, {1: "accepted", 2: "planned", 3: "planned", 4: "planned"})
    policy = POLICY.model_copy(update={"planning": POLICY.planning.model_copy(update={"max_replans_per_band": 1})})

    def reach_cap():
        with RaceSession.begin() as other:
            other.get(BandPlanRow, old_id).replan_count = 1

    with RaceSession.begin() as session:
        outcome = _planner(HookedLLM(reach_cap, _band(world, range(2, 5)))).replan_remaining(
            session, project_id=world.project_id, band_plan_id=old_id, expected_band_plan_id=old_id,
            from_chapter=2, policy=policy, notes=[],
        )
        assert outcome.band_plan_id is None and outcome.failure_reason == "replan_cap_reached"
    with RaceSession() as session:
        assert len(_versions(session, world, 0)) == 1
    with RaceSession.begin() as session:
        outcome = _planner(StubLLM(_band(world, range(2, 5)))).replan_remaining(
            session, project_id=world.project_id, band_plan_id=old_id,
            from_chapter=2, policy=policy, notes=["human amendment"],
        )
        assert session.get(BandPlanRow, outcome.band_plan_id).replan_count == 2


def _revise_sheet(session, world, arc_id):
    repo = ArcBeatSheetRepository(session)
    data = repo.load(repo.active_for_arc(arc_id)).model_dump(mode="json")
    data["milestones"].append({"id": "milestone-new", "description": "new requirement",
                               "target_range": [8, 8], "required_refs": [world.hero], "hard": True})
    return repo.create(world.project_id, arc_id, ArcBeatSheetData.model_validate(data),
                       source="llm", reason="amendment")


@pytest.mark.parametrize("outage", [False, True], ids=["replan", "defer"])
def test_same_span_sheet_revision_is_not_reused(RaceSession, outage):
    world, arc_id, _ = _book(RaceSession)
    previous, _ = _seed_plans(RaceSession, world, {4: "accepted"})
    ids, old_id = _seed_plans(RaceSession, world, {5: "accepted", 6: "drafted", 7: "needs_review", 8: "planned"})
    with RaceSession.begin() as session:
        session.get(ChapterPlan, previous[4]).milestone_ids_json = '["milestone-1"]'
        new_sheet = _revise_sheet(session, world, arc_id).id
    llm = (StubLLM(RuntimeError("offline"), RuntimeError("offline")) if outage else
           StubLLM(_band(world, [8], {8: {"milestone_ids": ["milestone-new"], "onstage_refs": [world.hero]}})))
    service = _service(llm, FeasibilityAuditor())
    service.band_planner = _planner(llm, FeasibilityAuditor())
    result = _run(RaceSession, service, world, 4)
    assert result["feasibility_issue_count"] >= 1
    assert result["planned_band_id"] != old_id
    with RaceSession() as session:
        for number, status in ((5, "accepted"), (6, "drafted"), (7, "needs_review")):
            row = session.get(ChapterPlan, ids[number])
            assert (row.status, row.title, row.band_plan_id) == (status, f"stub chapter {number}", old_id)
        if outage:
            assert result["band_planning_deferred"][0]["reason"] == "llm_unavailable"
            assert len(_versions(session, world, 1)) == 1
        else:
            assert result["band_planning_deferred"] == []
            assert session.get(BandPlanRow, result["planned_band_id"]).beat_sheet_id == new_sheet
            assert json.loads(session.get(ChapterPlan, ids[8]).milestone_ids_json) == ["milestone-new"]


def test_sheet_revision_during_model_cannot_publish_stale_plan(RaceSession):
    world, arc_id, _ = _book(RaceSession)
    _seed_plans(RaceSession, world, {4: "accepted"})

    def revise():
        with RaceSession.begin() as other:
            _revise_sheet(other, world, arc_id)

    result = _run(RaceSession, _service(HookedLLM(revise, _band(world, range(5, 9)))), world, 4)
    assert result["planned_band_id"] is None
    assert result["band_planning_deferred"] == [
        {"band": [5, 8], "reason": "superseded_beat_sheet", "issue_codes": []},
    ]
    with RaceSession() as session:
        assert _versions(session, world, 1) == []


def test_complete_band_requires_current_associations(RaceSession):
    world, _, _ = _book(RaceSession)
    _, previous = _seed_plans(RaceSession, world, {4: "accepted"})
    ids, old_id = _seed_plans(RaceSession, world, {5: "planned", 6: "planned", 7: "planned", 8: "planned"})
    with RaceSession.begin() as session:
        session.get(ChapterPlan, ids[8]).band_plan_id = previous
    result = _run(RaceSession, _service(StubLLM(_band(world, range(5, 9)))), world, 4)
    assert result["planned_band_id"] not in (None, old_id)
    with RaceSession() as session:
        assert all(session.get(ChapterPlan, row_id).band_plan_id == result["planned_band_id"] for row_id in ids.values())


def test_stale_sheet_with_only_protected_chapters_is_deferred(RaceSession):
    world, arc_id, _ = _book(RaceSession)
    _seed_plans(RaceSession, world, {4: "accepted"})
    ids, old_id = _seed_plans(RaceSession, world, {5: "accepted", 6: "drafted", 7: "needs_review", 8: "accepted"})
    with RaceSession.begin() as session:
        _revise_sheet(session, world, arc_id)
    llm = StubLLM()
    result = _run(RaceSession, _service(llm), world, 4)
    assert result["planned_band_id"] is None
    assert result["band_planning_deferred"] == [
        {"band": [5, 8], "reason": "band_not_current", "issue_codes": []},
    ]
    assert llm.calls == []
    with RaceSession() as session:
        assert all(session.get(ChapterPlan, row_id).band_plan_id == old_id for row_id in ids.values())


def test_stale_band_audit_failure_is_deferred(RaceSession):
    world, arc_id, _ = _book(RaceSession)
    _seed_plans(RaceSession, world, {4: "accepted"})
    _seed_plans(RaceSession, world, {5: "planned", 6: "planned", 7: "planned", 8: "planned"})
    with RaceSession.begin() as session:
        _revise_sheet(session, world, arc_id)

    class RaisingAudit:
        def audit_plans(self, *args, **kwargs):
            raise RuntimeError("audit unavailable")

    llm = StubLLM()
    result = _run(RaceSession, _service(llm, RaisingAudit()), world, 4)
    assert result["planned_band_id"] is None
    assert result["band_planning_deferred"] == [
        {"band": [5, 8], "reason": "audit_error", "issue_codes": ["audit_error"]},
    ]
    assert llm.calls == []


def test_crash_after_amendment_sheet_commit_is_recovered(RaceSession, monkeypatch):
    from forwin.planning.amend import PlanAmender
    from forwin.planning.band_planner.types import PlanIssue
    from forwin.planning.beat_sheet.types import BandSpan
    from forwin.planning.feasibility import open_plan_feasibility_conflict
    from forwin.world_engine.conflict_types import ConflictDecision

    world, arc_id, old_sheet = _book(RaceSession)
    _seed_plans(RaceSession, world, {4: "accepted"})
    _, old_band = _seed_plans(RaceSession, world, {5: "planned", 6: "planned", 7: "planned", 8: "planned"})
    with RaceSession.begin() as session:
        conflict = open_plan_feasibility_conflict(
            session, project_id=world.project_id, band=BandSpan(band_index=1, chapter_start=5, chapter_end=8),
            band_plan_id=old_band, policy=POLICY,
            issues=[PlanIssue(code="milestone_uncovered", severity="error", chapter_number=8,
                              message="revise milestone", ref="milestone-new")],
        )
        conflict_id = conflict.id

    class RevisionService:
        def revise(self, session, **kwargs):
            return _revise_sheet(session, world, arc_id)

    class SimulatedCrash(BaseException):
        pass

    def crash(session, **kwargs):
        # The actual amender must have committed its sheet before entering the band planner.
        with RaceSession() as other:
            assert ArcBeatSheetRepository(other).active_for_arc(arc_id).id != old_sheet
            assert _versions(other, world, 1)[0].status == "active"
        raise SimulatedCrash()

    planner = _planner(StubLLM())
    monkeypatch.setattr(planner, "replan_remaining", crash)
    with RaceSession() as session, pytest.raises(SimulatedCrash):
        PlanAmender(planner, RevisionService()).amend(
            session, conflict=session.get(WorldPlanConflictRow, conflict_id), policy=POLICY,
            decision=ConflictDecision(option="amend_plan", decided_by="human", rationale="revise the milestone",
                                      decided_at=datetime.now(timezone.utc)),
        )
    result = _run(RaceSession, _service(StubLLM(_band(
        world, range(5, 9), {8: {"milestone_ids": ["milestone-new"]}},
    ))), world, 4)
    assert result["planned_band_id"] not in (None, old_band)
    with RaceSession() as session:
        assert session.get(BandPlanRow, result["planned_band_id"]).beat_sheet_id == (
            ArcBeatSheetRepository(session).active_for_arc(arc_id).id
        )
