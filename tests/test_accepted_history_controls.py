"""Formal pacing/checkpoint reads must exclude unaccepted draft versions."""

import hashlib
import json

import pytest
from sqlalchemy import select

from forwin.generation.pipeline_core.audit_control import AuditControlStage
from forwin.generation.pipeline_core.world_projection import PostCanonStage
from forwin.models import (
    ArcPlanVersion,
    BandCheckpoint,
    BandExperiencePlan,
    CandidateDraftRecord,
    ChapterDraft,
    ChapterPlan,
    ChapterReview,
    DecisionEvent,
    Project,
)
from forwin.models.base import get_engine, get_session_factory
from forwin.models.canon import CanonCommitRecord
from forwin.observability.pipeline_trace import (
    PipelineAuditContext,
    PipelineTraceRecorder,
)
from forwin.planning.stage_analysis import (
    PacingStrategist,
    ReplanGovernor,
    StageAssessment,
)
from forwin.runtime.policy import RuntimePolicy
from forwin.runtime.policy_store import ProjectPolicyStore
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url


@pytest.fixture
def history_session():
    engine = get_engine(postgres_test_url("accepted-history-controls"))
    try:
        with get_session_factory(engine)() as session:
            yield session
    finally:
        engine.dispose()


def _book(session):
    project = Project(title="历史读侧", premise="档案调查", genre="悬疑")
    session.add(project)
    session.flush()
    ProjectPolicyStore(session).initialize(
        project, RuntimePolicy.for_profile("standard")
    )
    arc = ArcPlanVersion(
        project_id=project.id,
        status="active",
        chapter_start=1,
        chapter_end=4,
        arc_synopsis="封存档案并追查责任",
    )
    session.add(arc)
    session.flush()
    return project, arc


def _draft(session, chapter, *, version, body, verdict="pass"):
    draft = ChapterDraft(
        chapter_plan_id=chapter.id,
        version=version,
        body_text=body,
        summary="",
        char_count=len(body),
    )
    session.add(draft)
    session.flush()
    review = ChapterReview(draft_id=draft.id, verdict=verdict)
    session.add(review)
    session.flush()
    candidate = CandidateDraftRecord(
        project_id=chapter.project_id,
        chapter_plan_id=chapter.id,
        chapter_number=chapter.chapter_number,
        candidate_draft_id=draft.id,
        review_id=review.id,
        version=version,
        body_hash=hashlib.sha256(body.encode()).hexdigest(),
        status="reviewed",
    )
    session.add(candidate)
    session.flush()
    return draft, review, candidate


def _accepted(session, project, arc, number, *, body="档案已封存。", verdict="pass"):
    chapter = ChapterPlan(
        project_id=project.id,
        arc_plan_id=arc.id,
        chapter_number=number,
        title=f"第{number}章",
        status="accepted",
    )
    session.add(chapter)
    session.flush()
    draft, review, candidate = _draft(
        session,
        chapter,
        version=1,
        body=body,
        verdict=verdict,
    )
    commit = CanonCommitRecord(
        idempotency_key=f"{chapter.id}:acceptance",
        project_id=project.id,
        chapter_plan_id=chapter.id,
        chapter_number=number,
        candidate_id=candidate.id,
        chapter_title=chapter.title,
        base_book_revision=number - 1,
        status="committed",
    )
    session.add(commit)
    session.flush()
    chapter.active_commit_id = commit.id
    candidate.status = "accepted"
    candidate.canon_status = "canon"
    candidate.canon_commit_id = commit.id
    session.flush()
    return chapter, draft, review


def _band(session, project, arc):
    band = BandExperiencePlan(
        project_id=project.id,
        arc_id=arc.id,
        band_id="band-1",
        chapter_start=1,
        chapter_end=3,
        task_contract_json=json.dumps(
            [
                {
                    "task_type": "plot_advance",
                    "required_keywords": ["档案已封存"],
                    "source": "explicit",
                    "description": "封存档案",
                }
            ],
            ensure_ascii=False,
        ),
        schedule_json=json.dumps(
            {"band_id": "band-1", "chapter_start": 1, "chapter_end": 3}
        ),
    )
    session.add(band)
    session.flush()


class _Controls(AuditControlStage, PostCanonStage):
    def __init__(self):
        self.policy = RuntimePolicy.for_profile("standard")
        self.trace_recorder = PipelineTraceRecorder(
            audit=PipelineAuditContext(),
            artifact_store=None,
            observability=None,
        )


def _checkpoint(session, project):
    result = _Controls()._run_post_canon_band_checkpoint(
        session=session,
        repo=StateRepository(session),
        updater=StateUpdater(session),
        project_id=project.id,
        chapter_number=3,
    )
    assert result["status"] != "error", session.get(
        BandCheckpoint, result["id"]
    ).issues_json
    return result


def test_pacing_counts_stable_accepted_chapters_before_limiting(history_session):
    session = history_session
    project, arc = _book(session)
    chapters = [
        _accepted(session, project, arc, number, body="文" * 2800)[0]
        for number in (1, 2, 3)
    ]
    for version in (2, 3):
        _draft(session, chapters[-1], version=version, body="旧" * 100, verdict="fail")
    future = ChapterPlan(
        project_id=project.id,
        arc_plan_id=arc.id,
        chapter_number=4,
        status="planned",
        title="第4章",
        one_line="既定下一章",
    )
    session.add(future)
    session.flush()
    pacing = PacingStrategist().analyze(
        session=session, project_id=project.id, chapter_number=3
    )
    assert pacing.recent_char_counts == [2800, 2800, 2800]
    assert pacing.verdict == "steady"
    assert (
        ReplanGovernor().apply_if_needed(
            session=session,
            project_id=project.id,
            chapter_number=3,
            pacing=pacing,
            stage=StageAssessment("rising", 0.4, "", 0),
        )
        is None
    )
    assert future.one_line == "既定下一章"


@pytest.mark.parametrize(
    "contamination", ["pending_revision", "failed_revision", "new_review_on_same_draft"]
)
def test_band_checkpoint_uses_accepted_body_and_bound_review(
    history_session, contamination
):
    session = history_session
    project, arc = _book(session)
    accepted = [
        _accepted(
            session,
            project,
            arc,
            number,
            body="档案已封存。" if number == 1 else "他们继续核对证据。",
        )
        for number in (1, 2, 3)
    ]
    _band(session, project, arc)
    chapter, draft, _review = accepted[0]
    if contamination in {"pending_revision", "failed_revision"}:
        _draft(
            session,
            chapter,
            version=2,
            body="候选改稿，内容仍待审核。",
            verdict="fail" if contamination == "failed_revision" else "pass",
        )
    else:
        session.add(ChapterReview(draft_id=draft.id, verdict="fail"))
        session.flush()
    result = _checkpoint(session, project)
    assert result["status"] == "pass"
    event = session.scalar(
        select(DecisionEvent).where(DecisionEvent.related_object_id == result["id"])
    )
    assert json.loads(event.payload_json)["status"] == "pass"


@pytest.mark.parametrize("missing", ["active_pointer", "body"])
def test_band_checkpoint_never_accepts_missing_canonical_body(history_session, missing):
    session = history_session
    project, arc = _book(session)
    accepted = [_accepted(session, project, arc, number) for number in (1, 2, 3)]
    _band(session, project, arc)
    chapter, draft, _review = accepted[0]
    if missing == "active_pointer":
        chapter.active_commit_id = None
    else:
        draft.body_text = ""
    session.flush()
    result = _checkpoint(session, project)
    assert result["status"] == "fail"


@pytest.mark.parametrize(
    "failure", ["needs_review", "planned_no_body", "accepted_review_fail"]
)
def test_band_checkpoint_preserves_existing_failure_and_gate_block(
    history_session, failure
):
    session = history_session
    project, arc = _book(session)
    _accepted(session, project, arc, 1)
    _accepted(session, project, arc, 2)
    if failure == "planned_no_body":
        session.add(
            ChapterPlan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=3,
                title="第3章",
                status="planned",
            )
        )
    else:
        chapter, _draft_row, _review = _accepted(
            session,
            project,
            arc,
            3,
            verdict="fail" if failure == "accepted_review_fail" else "pass",
        )
        if failure == "needs_review":
            chapter.status = "needs_review"
            chapter.active_commit_id = None
    _band(session, project, arc)
    session.flush()
    result = _checkpoint(session, project)
    assert result["status"] == "fail"
    event = session.scalar(
        select(DecisionEvent).where(DecisionEvent.related_object_id == result["id"])
    )
    assert json.loads(event.payload_json)["gate_outcome"]["blocked"] is True
    session.add(
        BandExperiencePlan(
            project_id=project.id,
            arc_id=arc.id,
            band_id="band-2",
            chapter_start=4,
            chapter_end=4,
        )
    )
    session.flush()
    code, _band_id, _message = _Controls()._strict_progression_block(
        session=session,
        repo=StateRepository(session),
        updater=StateUpdater(session),
        project=project,
        chapter_number=4,
    )
    assert code == (
        "band_checkpoint_fail"
        if failure == "accepted_review_fail"
        else "chapter_not_canon"
    )
