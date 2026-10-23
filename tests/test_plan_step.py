from __future__ import annotations

import json
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from forwin.canon.admission import CanonAdmissionService  # noqa: F401 - initialize the canon package before events
from forwin.maintenance.events import POST_CANON_STEP_NAMES
from forwin.maintenance.post_canon import PostCanonMaintenanceService, save_world_prompt_trace
from forwin.models.base import Base
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.maintenance import PostCanonMaintenanceRun
from forwin.models.planning_v6 import BandPlanRow
from forwin.models.project import ArcPlanVersion, ChapterPlan
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.planning.arc_progression import ArcProgressionService
from forwin.planning.band_planner.types import PlanIssue
from forwin.planning.beat_sheet.service import ArcBeatSheetService
from forwin.planning.plan_step import PlanStepService
from tests.helpers.band_plans import stub_plan, write_band_plan
from tests.test_arc_beat_sheet import StubLLM
from tests.test_band_planner import POLICY, StubAudit, _band, _book, _planner


@pytest.fixture
def Session():
    engine = create_engine("sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def _service(llm, audit=None):
    return PlanStepService(
        _planner(llm), audit or StubAudit(),
        ArcProgressionService(ArcBeatSheetService(llm, save_trace=save_world_prompt_trace)),
    )


def _run(Session, service, world, number):
    with Session() as session:
        return service.run(session, CanonCommitRecord(project_id=world.project_id, chapter_number=number),
                           policy=POLICY)


def _seed_plans(Session, world, statuses):
    with Session.begin() as session:
        rows = write_band_plan(session, world.project_id, {n: stub_plan(n) for n in statuses})
        for row in rows:
            row.status = statuses[row.chapter_number]
        return {row.chapter_number: row.id for row in rows}, rows[0].band_plan_id


def _error(number=4):
    return PlanIssue(code="onstage_unavailable", severity="error", chapter_number=number, message="actor unavailable")


def test_steps_are_world_plan_feedback() -> None:
    assert POST_CANON_STEP_NAMES == ("world", "plan", "feedback")


def test_plan_step_replans_infeasible_remaining_chapters(Session) -> None:
    world, _, _ = _book(Session)
    ids, old_band = _seed_plans(Session, world, {1: "accepted", 2: "drafted", 3: "needs_review", 4: "planned"})
    llm = StubLLM(_band(world, [4], {4: {"milestone_ids": ["milestone-1"]}}))
    audit = StubAudit([_error()])
    result = _run(Session, _service(llm, audit), world, 1)
    assert result["feasibility_issue_count"] == 1
    assert len(result["replanned_band_ids"]) == 1
    assert result["replanned_band_ids"] != [old_band]
    assert result["conflict_ids"] == []
    assert [[plan.chapter_number for plan in call] for call in audit.calls] == [[4]]
    with Session() as session:
        assert session.get(BandPlanRow, old_band).status == "superseded"
        new = session.get(BandPlanRow, result["replanned_band_ids"][0])
        assert new.replan_count == 1
        for number, status in ((1, "accepted"), (2, "drafted"), (3, "needs_review")):
            row = session.get(ChapterPlan, ids[number])
            assert (row.status, row.title, row.band_plan_id) == (status, f"stub chapter {number}", old_band)
        assert session.get(ChapterPlan, ids[4]).band_plan_id == new.id
    assert len(llm.calls) == 1


def test_plan_step_creates_conflict_after_replan_cap(Session) -> None:
    world, _, _ = _book(Session)
    _, band_id = _seed_plans(Session, world, {1: "accepted", 2: "planned", 3: "planned", 4: "planned"})
    with Session.begin() as session:
        session.get(BandPlanRow, band_id).replan_count = POLICY.planning.max_replans_per_band
    llm = StubLLM()
    service = _service(llm, StubAudit([_error(2)], [_error(2)]))
    first = _run(Session, service, world, 1)
    second = _run(Session, service, world, 1)
    assert first["conflict_ids"] == second["conflict_ids"]
    assert len(first["conflict_ids"]) == 1
    with Session() as session:
        conflict = session.scalars(select(WorldPlanConflictRow)).one()
        assert (conflict.source, conflict.required_decider, conflict.blocks_from_chapter) == (
            "plan_feasibility", "human", 2,
        )
        assert json.loads(conflict.options_json) == ["amend_plan"]
    assert llm.calls == []


def test_band_end_plans_next_band(Session) -> None:
    world, _, _ = _book(Session)
    _seed_plans(Session, world, {4: "accepted"})
    llm = StubLLM(_band(world, range(5, 9)))
    service = _service(llm)
    first = _run(Session, service, world, 4)
    second = _run(Session, service, world, 4)
    assert first["planned_band_id"] == second["planned_band_id"]
    assert first["planned_band_id"] is not None
    assert first["band_planning_deferred"] == []
    with Session() as session:
        row = session.get(BandPlanRow, first["planned_band_id"])
        assert (row.chapter_start, row.chapter_end) == (5, 8)
        assert [p.chapter_number for p in session.scalars(select(ChapterPlan).where(
            ChapterPlan.band_plan_id == row.id).order_by(ChapterPlan.chapter_number))] == [5, 6, 7, 8]
    assert len(llm.calls) == 1


def test_arc_end_activates_next_arc_and_plans_first_band(Session) -> None:
    world, arc_id, _ = _book(Session)
    _seed_plans(Session, world, {12: "accepted"})
    with Session.begin() as session:
        arc = ArcPlanVersion(project_id=world.project_id, arc_number=2, chapter_start=13, chapter_end=16,
                             status="planned", arc_synopsis="next arc")
        session.add(arc)
        session.flush()
        next_id = arc.id
    llm = StubLLM({
        "protagonist_ref": world.hero,
        "phases": [{"name": "phase", "chapter_range": [13, 16], "purpose": "next phase"}],
        "band_breakdown": [{"band_index": 0, "chapter_start": 13, "chapter_end": 16}],
    }, _band(world, range(13, 17)))
    service = _service(llm)
    result = _run(Session, service, world, 12)
    replay = _run(Session, service, world, 12)
    assert result["activated_arc_id"] == replay["activated_arc_id"] == next_id
    assert result["planned_band_id"] == replay["planned_band_id"]
    with Session() as session:
        assert session.get(ArcPlanVersion, arc_id).status == "completed"
        assert session.get(ArcPlanVersion, next_id).status == "active"
        row = session.get(BandPlanRow, result["planned_band_id"])
        assert (row.arc_plan_id, row.chapter_start, row.chapter_end) == (next_id, 13, 16)
    assert [kwargs["stage_key"] for _, kwargs in llm.calls] == ["plan.arc_beats", "plan.band"]


def _maintenance(Session, world, plan_id, number, llm, plan_step):
    with Session.begin() as session:
        draft = ChapterDraft(chapter_plan_id=plan_id, body_text="body")
        session.add(draft)
        session.flush()
        review = ChapterReview(draft_id=draft.id, verdict="pass")
        session.add(review)
        session.flush()
        candidate = CandidateDraftRecord(project_id=world.project_id, chapter_plan_id=plan_id,
                                         chapter_number=number, candidate_draft_id=draft.id,
                                         review_id=review.id, status="accepted")
        session.add(candidate)
        session.flush()
        commit = CanonCommitRecord(project_id=world.project_id, chapter_number=number, candidate_id=candidate.id,
                                   idempotency_key="canon-plan-step", status="committed")
        session.add(commit)
        session.flush()
        candidate.canon_commit_id = commit.id
        commit_id = commit.id
    maintenance = PostCanonMaintenanceService(
        session_factory=Session, stage_analyzer=object(), pacing_strategist=object(), replan_governor=object(),
        arc_envelope_manager=object(), comment_analyzer=object(), artifact_store=None, llm_client=llm,
        plan_step=plan_step,
    )
    maintenance.heartbeat = lambda _claim: None
    maintenance._step_runners["world"] = lambda *_: {}
    maintenance._step_runners["feedback"] = lambda *_: {}
    return maintenance, commit_id


def test_plan_step_llm_outage_defers_without_failing_step(Session) -> None:
    world, _, _ = _book(Session)
    ids, _ = _seed_plans(Session, world, {4: "accepted"})
    llm = StubLLM(RuntimeError("provider unavailable"), RuntimeError("provider unavailable"))
    maintenance, commit_id = _maintenance(Session, world, ids[4], 4, llm, _service(llm))
    maintenance.run(canon_commit_id=commit_id, worker_id="test")
    with Session() as session:
        row = session.scalars(select(PostCanonMaintenanceRun).where(
            PostCanonMaintenanceRun.canon_commit_id == commit_id, PostCanonMaintenanceRun.step_name == "plan",
        )).one()
        assert row.status == "succeeded"
        result = json.loads(row.result_json)
        assert set(result) == {"feasibility_issue_count", "replanned_band_ids", "conflict_ids",
                               "planned_band_id", "activated_arc_id", "band_planning_deferred"}
        assert result["band_planning_deferred"] == [
            {"band": [5, 8], "reason": "llm_unavailable", "issue_codes": ["llm_unavailable"]},
        ]
        assert list(session.scalars(select(ChapterPlan).where(ChapterPlan.chapter_number > 4))) == []
        assert list(session.scalars(select(WorldPlanConflictRow))) == []
    assert len(llm.calls) == 2


def test_planning_audit_error_is_deferred_and_keeps_trace(Session) -> None:
    from forwin.models.genesis import PromptTrace

    class RaisingAudit:
        def audit_plans(self, *args, **kwargs):
            raise RuntimeError("audit unavailable")

    world, _, _ = _book(Session)
    _seed_plans(Session, world, {4: "accepted"})
    llm = StubLLM(_band(world, range(5, 9)))
    service = _service(llm)
    service.band_planner = _planner(llm, RaisingAudit())
    result = _run(Session, service, world, 4)
    assert result["planned_band_id"] is None
    assert result["band_planning_deferred"] == [
        {"band": [5, 8], "reason": "audit_error", "issue_codes": ["audit_error"]},
    ]
    with Session() as session:
        trace = session.scalars(select(PromptTrace).where(PromptTrace.stage_key == "plan.band")).one()
        assert json.loads(trace.output_summary_json)["failure_reason"] == "audit_error"
        assert list(session.scalars(select(ChapterPlan).where(ChapterPlan.chapter_number > 4))) == []


@pytest.mark.parametrize("failure", ["invalid_output", "infeasible"])
def test_deferred_reason_is_distinct_from_issue_codes(Session, failure) -> None:
    world, _, _ = _book(Session)
    _seed_plans(Session, world, {4: "accepted"})
    answer = _band(world, range(5, 9))
    if failure == "invalid_output":
        answer["chapters"][0]["pov_ref"] = "unknown-character"
    llm = StubLLM(answer, answer)
    service = _service(llm)
    if failure == "infeasible":
        service.band_planner = _planner(llm, StubAudit([_error(5)], [_error(5)]))
    result = _run(Session, service, world, 4)
    deferred, = result["band_planning_deferred"]
    assert deferred["reason"] == failure
    assert deferred["issue_codes"]
    assert failure not in deferred["issue_codes"]


def test_plan_step_ignores_warnings_and_mid_band_does_not_plan_ahead(Session) -> None:
    world, _, _ = _book(Session)
    _seed_plans(Session, world, {1: "accepted", 2: "planned"})
    llm = StubLLM()
    audit = StubAudit([PlanIssue(code="clock_unanswered", severity="warning", chapter_number=2, message="clock")])
    result = _run(Session, _service(llm, audit), world, 1)
    assert result == {"feasibility_issue_count": 1, "replanned_band_ids": [], "conflict_ids": [],
                      "planned_band_id": None, "activated_arc_id": None, "band_planning_deferred": []}
    assert llm.calls == []


@pytest.fixture
def PgSession():
    from forwin.models.base import get_engine, get_session_factory, init_db
    from tests.postgres import postgres_test_url

    engine = get_engine(postgres_test_url("plan-step-locks"))
    init_db(engine)
    yield get_session_factory(engine)
    engine.dispose()


@pytest.mark.parametrize("boundary", ["replan", "band_end", "arc_end"])
def test_postgres_plan_step_releases_locks_before_model_calls(PgSession, boundary) -> None:
    from forwin.models.project import Project
    from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository

    world, _, _ = _book(PgSession)
    number = {"replan": 1, "band_end": 4, "arc_end": 12}[boundary]
    statuses = {1: "accepted", 2: "planned", 3: "planned", 4: "planned"} if boundary == "replan" else {number: "accepted"}
    ids, _ = _seed_plans(PgSession, world, statuses)
    responses = [_band(world, range(2, 5) if boundary == "replan" else range(5, 9))]
    next_id = None
    if boundary == "arc_end":
        with PgSession.begin() as session:
            arc = ArcPlanVersion(project_id=world.project_id, arc_number=2, chapter_start=13, chapter_end=16,
                                 status="planned", arc_synopsis="next arc")
            session.add(arc)
            session.flush()
            next_id = arc.id
        responses = [{
            "protagonist_ref": world.hero,
            "phases": [{"name": "phase", "chapter_range": [13, 16], "purpose": "next phase"}],
            "band_breakdown": [{"band_index": 0, "chapter_start": 13, "chapter_end": 16}],
        }, _band(world, range(13, 17))]
    probes = []

    class CheckingLLM(StubLLM):
        def chat(self, messages, **kwargs):
            with PgSession.begin() as probe:
                probe.scalars(select(Project).where(Project.id == world.project_id)
                              .with_for_update(key_share=True, nowait=True)).one()
                list(probe.scalars(select(ChapterPlan).where(ChapterPlan.project_id == world.project_id)
                                   .with_for_update(nowait=True)))
                if next_id is not None:
                    assert probe.get(ArcPlanVersion, next_id).status == "active"
                    if kwargs["stage_key"] == "plan.band":
                        assert ArcBeatSheetRepository(probe).active_for_arc(next_id) is not None
            probes.append(kwargs["stage_key"])
            return super().chat(messages, **kwargs)

    llm = CheckingLLM(*responses)
    audit = StubAudit([_error(2)]) if boundary == "replan" else StubAudit()
    maintenance, commit_id = _maintenance(PgSession, world, ids[number], number, llm, _service(llm, audit))
    maintenance.run(canon_commit_id=commit_id, worker_id="postgres-test")
    assert probes == (["plan.arc_beats", "plan.band"] if boundary == "arc_end" else ["plan.band"])
    with PgSession() as session:
        row = session.scalars(select(PostCanonMaintenanceRun).where(
            PostCanonMaintenanceRun.canon_commit_id == commit_id, PostCanonMaintenanceRun.step_name == "plan",
        )).one()
        assert row.status == "succeeded"
        result = json.loads(row.result_json)
        assert result["band_planning_deferred"] == []
        assert result["conflict_ids"] == []


def test_failed_replan_blocks_the_first_infeasible_remaining_chapter(Session) -> None:
    world, _, _ = _book(Session)
    _seed_plans(Session, world, {1: "accepted", 2: "planned", 3: "planned", 4: "planned"})
    llm = StubLLM(RuntimeError("provider unavailable"), RuntimeError("provider unavailable"))
    result = _run(Session, _service(llm, StubAudit([_error(2)])), world, 1)
    assert len(result["conflict_ids"]) == 1
    with Session() as session:
        conflict = session.get(WorldPlanConflictRow, result["conflict_ids"][0])
        assert conflict.blocks_from_chapter == 2
        assert conflict.source == "plan_feasibility"
        assert [item["code"] for item in json.loads(conflict.proposed_change_json)["issues"]] == [
            "onstage_unavailable", "llm_unavailable",
        ]
