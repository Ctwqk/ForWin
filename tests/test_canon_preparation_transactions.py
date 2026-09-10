from __future__ import annotations

import json
from types import SimpleNamespace

from sqlalchemy import select

from forwin.audit.events import DecisionEventType
from forwin.canon.admission import CanonAdmissionService
from forwin.canon.preparation import (
    BookStatePreparationOutcome,
    CanonPreparationService,
)
from forwin.canon.types import CanonQualityGateOutcome
from forwin.generation.pipeline_core.acceptance import AcceptanceStage
from forwin.models.audit import DecisionEvent
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord
from forwin.models.outbox import OutboxEvent
from forwin.models.project import ChapterPlan, Project
from forwin.observability.pipeline_trace import (
    PipelineAuditContext,
    PipelineTraceRecorder,
)
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.runtime.policy import RuntimePolicy
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.storage import ArtifactStore
from tests import test_canon_atomic_transaction as atomic

prepared_canon = atomic.prepared_canon


def test_manual_preparation_is_committed_before_independent_atomic_admission(
    prepared_canon,
    tmp_path,
):
    prepared = prepared_canon
    with prepared.Session.begin() as session:
        session.get(ChapterPlan, prepared.chapter_plan_id).status = "needs_review"
    output = WriterOutput(
        project_id=prepared.project_id,
        chapter_number=1,
        title="Chapter one",
        body="Shen Linchuan enters the archive.",
        char_count=33,
        end_of_chapter_summary="A new character enters canon.",
        generation_meta={
            "entity_admission_plan": prepared.plan.entity_admission_plan.model_dump(
                mode="json"
            )
        },
    )

    class ApprovedQuality:
        def evaluate(self, **_kwargs):
            return CanonQualityGateOutcome()

    class ApprovedBookState:
        def prepare(self, **_kwargs):
            return BookStatePreparationOutcome(
                approved_changes=prepared.plan.approved_book_state_changes
            )

    class ObservedAdmission(CanonAdmissionService):
        def commit_plan(self, plan, **kwargs):
            # A different PostgreSQL connection sees the freshly prepared plan;
            # NOWAIT also proves the preparation transaction released its locks.
            with prepared.Session.begin() as observer:
                observer.scalar(
                    select(Project)
                    .where(Project.id == prepared.project_id)
                    .with_for_update(nowait=True)
                )
                candidate = observer.scalar(
                    select(CandidateDraftRecord)
                    .where(CandidateDraftRecord.id == prepared.candidate_id)
                    .with_for_update(nowait=True)
                )
                assert candidate.status == "ready_for_canon"
                assert (
                    json.loads(candidate.canon_commit_plan_json)["acceptance_mode"]
                    == "human_approved"
                )
                assert observer.scalar(
                    select(DecisionEvent.id).where(
                        DecisionEvent.event_type
                        == DecisionEventType.CANON_COMMIT_STARTED
                    )
                )
                assert list(observer.scalars(select(CanonCommitRecord))) == []
                assert list(observer.scalars(select(OutboxEvent))) == []
            return super().commit_plan(plan, **kwargs)

    stage = AcceptanceStage()
    stage._SessionFactory = prepared.Session
    stage._make_state_helpers = lambda session: (
        StateRepository(session),
        StateUpdater(session),
        None,
    )
    stage.policy = RuntimePolicy.for_profile("standard")
    stage.llm_client = None
    stage.artifact_store = ArtifactStore(str(tmp_path))
    stage.trace_recorder = PipelineTraceRecorder(
        audit=PipelineAuditContext(task_id="manual-task", root_event_id="manual-root"),
        artifact_store=stage.artifact_store,
        observability=SimpleNamespace(_record_span=lambda _span: None),
    )
    stage.canon_preparation = CanonPreparationService(
        quality_preparer=ApprovedQuality(), book_state_preparer=ApprovedBookState()
    )
    stage.canon_admission = ObservedAdmission(session_factory=prepared.Session)
    stage._load_writer_output_from_meta = lambda _path: output
    stage._load_review_verdict = lambda _row: ReviewVerdict(verdict="pass")
    stage._record_decision_event = stage.trace_recorder.record_event
    stage._run_phase3_pass = lambda **_kwargs: {"run_ids": {}}
    stage._run_post_canon_order_controls = lambda **_kwargs: None
    stage.post_canon_maintenance = SimpleNamespace(
        barrier_blocking_reasons=lambda _commit: []
    )

    result = stage.accept_review(prepared.project_id, 1)
    assert result["status"] == "accepted"
    with prepared.Session() as session:
        chapter = session.get(ChapterPlan, prepared.chapter_plan_id)
        commits = list(session.scalars(select(CanonCommitRecord)))
        assert len(commits) == 1
        assert chapter.active_commit_id == commits[0].id
        assert (
            session.get(CandidateDraftRecord, prepared.candidate_id).status
            == "accepted"
        )
        assert len(list(session.scalars(select(OutboxEvent)))) == 3
        approved = session.scalar(
            select(DecisionEvent).where(
                DecisionEvent.event_type == DecisionEventType.REVIEW_APPROVED
            )
        )
        assert approved.task_id == "manual-task"
        assert approved.causal_root_id == "manual-root"
