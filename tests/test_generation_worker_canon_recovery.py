from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from forwin.application.generation import GenerationApplicationService
from forwin.candidate_drafts import (
    CandidateDraftRepository,
    candidate_plan_revision,
)
from forwin.canon.admission import CanonAdmissionService
from forwin.canon.outbox_events import (
    CANON_RECOVERY_EVENT_TYPES,
    canon_event_id,
)
from forwin.canon.plan import CanonCommitPlan
from forwin.canon.preparation import CanonPreparationService
from forwin.config import InfrastructureConfig
from forwin.generation.pipeline_core.result import RunResult
from forwin.generation.task_lease import claim_generation_task
from forwin.generation.task_payload import execution_payload, payload_to_json
from forwin.generation.worker import run_one_generation_task
from forwin.models.audit import DecisionEvent
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.book_state import (
    GraphDeltaRow,
    MapSnapshotRow,
    WorldNodeRow,
    WorldSnapshotRow,
)
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.entity import Entity, EntityAlias
from forwin.models.narrative_obligation import NarrativeObligationRow
from forwin.models.outbox import OutboxEvent
from forwin.models.project import ChapterPlan
from forwin.models.task import GenerationTask
from forwin.naming import (
    EntityAdmissionDecision,
    EntityAdmissionPlan,
    writer_output_admission_fingerprint,
)
from forwin.novel_export.events import NOVEL_EXPORT_REQUESTED
from forwin.observability.ports import NullObservability
from forwin.protocol.book_state import ApprovedGraphDeltaSet, GraphDelta, NodePatch
from forwin.protocol.writer import WriterOutput
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url


@dataclass(frozen=True)
class RecoveryFixture:
    Session: sessionmaker[Session]
    database_url: str
    plan: CanonCommitPlan
    project_id: str
    chapter_plan_id: str
    candidate_id: str
    task_id: str


@pytest.fixture
def recovery_fixture() -> RecoveryFixture:
    database_url = postgres_test_url("generation-worker-canon-recovery")
    engine = get_engine(database_url)
    init_db(engine)
    SessionFactory = get_session_factory(engine)
    task_id = "task-canon-recovery"

    with SessionFactory.begin() as session:
        updater = StateUpdater(session)
        policy = RuntimePolicy.for_profile("standard")
        project = updater.create_project(
            title="Worker Canon Recovery",
            premise="A reclaimed worker must commit exactly once.",
            genre="thriller",
            runtime_policy=policy,
        )
        project.automation_json = '{"primary_publish_platform":"qidian"}'
        session.flush()
        arc = updater.create_arc_plan(project.id, "Arc one")
        chapter = updater.create_chapter_plan(
            project_id=project.id,
            arc_plan_id=arc.id,
            chapter_number=1,
            title="Chapter one",
            one_line="Recover an interrupted Canon commit",
            goals=["Commit exactly once"],
        )
        output = WriterOutput(
            project_id=project.id,
            chapter_number=1,
            title="Chapter one",
            body="Shen Linchuan enters the archive.",
            char_count=33,
            end_of_chapter_summary="A new character enters canon.",
        )
        draft = ChapterDraft(
            chapter_plan_id=chapter.id,
            version=1,
            body_text=output.body,
            summary=output.end_of_chapter_summary,
            char_count=output.char_count,
        )
        session.add(draft)
        session.flush()
        review = ChapterReview(
            draft_id=draft.id,
            verdict="pass",
            issues_json="[]",
            review_meta_json='{"verdict":"pass"}',
        )
        session.add(review)
        session.flush()
        candidate = CandidateDraftRepository(session).create_reviewed_version(
            project_id=project.id,
            chapter_plan=chapter,
            draft=draft,
            review=review,
            writer_output=output,
            plan_revision=candidate_plan_revision(chapter),
            policy_version=1,
        )
        approved = ApprovedGraphDeltaSet(
            project_id=project.id,
            chapter_number=1,
            graph_deltas=[
                GraphDelta(
                    id=f"delta-{candidate.id}",
                    project_id=project.id,
                    chapter_number=1,
                    summary="Register Shen Linchuan",
                    node_patches=[
                        NodePatch(
                            node_id=f"character-{candidate.id}",
                            node_type="character",
                            op="create",
                            new_value={
                                "id": f"character-{candidate.id}",
                                "project_id": project.id,
                                "node_type": "character",
                                "name": "Shen Linchuan",
                                "description": "An archivist entering the story.",
                            },
                        )
                    ],
                )
            ],
            approved_by=["book_state_review"],
            review_verdict_id=review.id,
        )
        entity_plan = EntityAdmissionPlan(
            project_id=project.id,
            chapter_number=1,
            candidate_fingerprint=writer_output_admission_fingerprint(output),
            decisions=[
                EntityAdmissionDecision(
                    mention_name="Shen Linchuan",
                    action="register_character",
                    entity_id=f"character-{candidate.id}",
                    canonical_name="Shen Linchuan",
                    aliases=["Archivist Shen"],
                )
            ],
        )
        session.add(
            NarrativeObligationRow(
                project_id=project.id,
                origin_chapter_number=1,
                origin_draft_id=draft.id,
                origin_review_id=review.id,
                obligation_type="future_payoff",
                status="planned",
                summary="Explain why the archive was sealed.",
            )
        )
        prepared = CanonPreparationService().prepare_from_approved(
            session=session,
            candidate_id=candidate.id,
            approved_book_state_changes=approved,
            entity_admission_plan=entity_plan,
            acceptance_mode="normal",
            repair_attempt_count=0,
            residual_review_issues=[],
            canon_risk_level="low",
        )
        assert prepared.plan is not None
        session.add(
            GenerationTask(
                id=task_id,
                task_kind="generation",
                status="queued",
                current_stage="queued",
                project_id=project.id,
                requested_chapters=1,
                max_chapters=1,
                execution_payload_json=payload_to_json(
                    execution_payload(
                        mode="continue",
                        policy=policy,
                        policy_version=1,
                        auto_continue=False,
                        max_chapters=1,
                    )
                ),
            )
        )
        fixture = RecoveryFixture(
            Session=SessionFactory,
            database_url=database_url,
            plan=prepared.plan,
            project_id=project.id,
            chapter_plan_id=chapter.id,
            candidate_id=candidate.id,
            task_id=task_id,
        )

    yield fixture
    engine.dispose()


def _claim(fixture: RecoveryFixture) -> None:
    with fixture.Session.begin() as session:
        claim = claim_generation_task(
            session,
            worker_id="crashed-worker",
            lease_seconds=30,
        )
        assert claim is not None
        assert claim.task.id == fixture.task_id


def _expire_lease(fixture: RecoveryFixture) -> None:
    expired = (datetime.now(UTC) - timedelta(minutes=5)).replace(tzinfo=None)
    with fixture.Session.begin() as session:
        task = session.get(GenerationTask, fixture.task_id)
        assert task is not None
        task.lease_expires_at = expired
        task.heartbeat_at = expired
        session.add(task)


class RecoveryPipeline:
    def __init__(self, fixture: RecoveryFixture) -> None:
        self.fixture = fixture
        self.calls: list[tuple[str, int | None, int | None]] = []
        self._SessionFactory = fixture.Session
        self.observability = NullObservability()
        self.llm_client = SimpleNamespace(close=lambda: None)
        self.engine = SimpleNamespace(dispose=lambda: None)

    def continue_project(
        self,
        project_id: str,
        *,
        max_chapters: int | None,
        resume_from_chapter: int | None,
    ) -> RunResult:
        self.calls.append((project_id, max_chapters, resume_from_chapter))
        raise AssertionError("Canon recovery must complete before pipeline execution")


def _recovery_application(
    fixture: RecoveryFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[GenerationApplicationService, RecoveryPipeline]:
    pipeline = RecoveryPipeline(fixture)
    monkeypatch.setattr(
        "forwin.application.generation_execution._build_chapter_pipeline_for_task",
        lambda *_args, **_kwargs: pipeline,
    )
    application = GenerationApplicationService(
        session_factory=fixture.Session,
        infrastructure=InfrastructureConfig(database_url=fixture.database_url),
    )
    return application, pipeline


def _assert_single_committed_state(fixture: RecoveryFixture) -> None:
    with fixture.Session() as session:
        chapter = session.get(ChapterPlan, fixture.chapter_plan_id)
        candidate = session.get(CandidateDraftRecord, fixture.candidate_id)
        task = session.get(GenerationTask, fixture.task_id)
        assert chapter is not None and chapter.status == "accepted"
        assert candidate is not None and candidate.status == "accepted"
        assert task is not None and task.status == "completed"
        assert task.completed_chapters_json == "[1]"
        assert session.scalar(select(func.count(CanonCommitRecord.id))) == 1
        assert session.scalar(select(func.count(GraphDeltaRow.id))) == 1
        assert session.scalar(select(func.count(WorldSnapshotRow.id))) == 1
        assert session.scalar(select(func.count(MapSnapshotRow.id))) == 1
        assert session.scalar(select(func.count(WorldNodeRow.id))) == 1
        assert session.scalar(select(func.count(Entity.id))) == 1
        assert session.scalar(select(func.count(EntityAlias.id))) == 1
        assert session.scalar(select(func.count(NarrativeObligationRow.id))) == 1
        outbox_events = list(
            session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.aggregate_id == fixture.project_id,
                )
            )
        )
        assert len(CANON_RECOVERY_EVENT_TYPES) == 3
        assert NOVEL_EXPORT_REQUESTED not in CANON_RECOVERY_EVENT_TYPES
        recovery_events = [
            event
            for event in outbox_events
            if event.event_type in CANON_RECOVERY_EVENT_TYPES
        ]
        assert len(recovery_events) == 3
        assert {event.event_type for event in recovery_events} == set(
            CANON_RECOVERY_EVENT_TYPES
        )
        assert {event.event_id for event in recovery_events} == {
            canon_event_id(fixture.plan.idempotency_key, event_type)
            for event_type in CANON_RECOVERY_EVENT_TYPES
        }
        expected_events = {
            event.event_id: event for event in fixture.plan.outbox_events
        }
        for event in recovery_events:
            expected = expected_events[event.event_id]
            assert event.aggregate_type == expected.aggregate_type
            assert event.aggregate_id == expected.aggregate_id
            assert json.loads(event.payload_json) == expected.payload
        exports = [
            event for event in outbox_events
            if event.event_type == NOVEL_EXPORT_REQUESTED
        ]
        assert len(outbox_events) == 4
        assert len(exports) == 1
        assert exports[0].event_id == f"novel-export:{fixture.project_id}:1"
        assert exports[0].aggregate_type == "project"
        assert json.loads(exports[0].payload_json) == {
            "schema_version": 1,
            "project_id": fixture.project_id,
            "book_revision": 1,
            "display_title": "Worker Canon Recovery",
            "snapshot": None,
        }
        deferred = list(
            session.scalars(
                select(DecisionEvent).where(
                    DecisionEvent.project_id == fixture.project_id,
                    DecisionEvent.chapter_number == 1,
                    DecisionEvent.event_type == "deferred_maintenance_recorded",
                    DecisionEvent.related_object_type == "generation_task",
                    DecisionEvent.related_object_id == fixture.task_id,
                )
            )
        )
        assert len(deferred) == 1


def _assert_expired_control_request_is_acknowledged(
    recovery_fixture: RecoveryFixture,
    *,
    request_attribute: str,
    expected_status: str,
) -> None:
    _claim(recovery_fixture)
    _expire_lease(recovery_fixture)
    with recovery_fixture.Session.begin() as session:
        task = session.get(GenerationTask, recovery_fixture.task_id)
        assert task is not None
        setattr(task, request_attribute, True)

    def forbidden_runner(*_args, **_kwargs) -> None:
        raise AssertionError("control acknowledgement must not run the pipeline")

    application = GenerationApplicationService(
        session_factory=recovery_fixture.Session,
        infrastructure=InfrastructureConfig(database_url=recovery_fixture.database_url),
        runner=forbidden_runner,
    )

    result = run_one_generation_task(
        application_service=application,
        worker_id="control-recovery-worker",
        lease_seconds=30,
    )

    assert result.claimed is True
    with recovery_fixture.Session() as session:
        task = session.get(GenerationTask, recovery_fixture.task_id)
        assert task is not None
        assert task.status == expected_status
        assert task.current_stage == expected_status
        assert task.finished_at is not None
        if expected_status == "paused":
            assert task.paused_at is not None
        else:
            assert task.paused_at is None


def test_expired_running_pause_request_is_acknowledged_without_pipeline_work(
    recovery_fixture: RecoveryFixture,
) -> None:
    _assert_expired_control_request_is_acknowledged(
        recovery_fixture,
        request_attribute="pause_requested",
        expected_status="paused",
    )


def test_expired_running_cancel_request_is_acknowledged_without_pipeline_work(
    recovery_fixture: RecoveryFixture,
) -> None:
    _assert_expired_control_request_is_acknowledged(
        recovery_fixture,
        request_attribute="cancel_requested",
        expected_status="cancelled",
    )


def test_reclaim_after_precommit_crash_commits_candidate_once(
    recovery_fixture: RecoveryFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _claim(recovery_fixture)
    with recovery_fixture.Session() as session:
        candidate = session.get(CandidateDraftRecord, recovery_fixture.candidate_id)
        assert candidate is not None and candidate.status == "ready_for_canon"
    _expire_lease(recovery_fixture)
    outcomes: list[object] = []
    original_commit_plan = CanonAdmissionService.commit_plan

    def record_commit(service, plan, **kwargs):
        outcome = original_commit_plan(service, plan, **kwargs)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(CanonAdmissionService, "commit_plan", record_commit)
    application, pipeline = _recovery_application(recovery_fixture, monkeypatch)

    result = run_one_generation_task(
        application_service=application,
        worker_id="recovery-worker",
        lease_seconds=30,
    )

    assert result.claimed is True
    assert result.task_id == recovery_fixture.task_id
    assert len(outcomes) == 1
    assert outcomes[0].idempotent is False
    assert pipeline.calls == []
    _assert_single_committed_state(recovery_fixture)


def test_reclaim_after_canon_commit_replays_once_then_finishes_task(
    recovery_fixture: RecoveryFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _claim(recovery_fixture)
    first = CanonAdmissionService(session_factory=recovery_fixture.Session).commit_plan(
        recovery_fixture.plan
    )
    assert first.blocked is False
    assert first.idempotent is False
    with recovery_fixture.Session() as session:
        task = session.get(GenerationTask, recovery_fixture.task_id)
        assert task is not None and task.status == "running"
    _expire_lease(recovery_fixture)
    replay_outcomes: list[object] = []
    original_commit_plan = CanonAdmissionService.commit_plan

    def record_replay(service, plan, **kwargs):
        outcome = original_commit_plan(service, plan, **kwargs)
        replay_outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(CanonAdmissionService, "commit_plan", record_replay)
    application, pipeline = _recovery_application(recovery_fixture, monkeypatch)

    result = run_one_generation_task(
        application_service=application,
        worker_id="recovery-worker",
        lease_seconds=30,
    )

    assert result.claimed is True
    assert pipeline.calls == []
    assert len(replay_outcomes) == 1
    assert replay_outcomes[0].commit_id == first.commit_id
    assert replay_outcomes[0].idempotent is True
    _assert_single_committed_state(recovery_fixture)


def test_recovered_terminal_task_still_runs_completion_handler(
    recovery_fixture: RecoveryFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _claim(recovery_fixture)
    outcome = CanonAdmissionService(
        session_factory=recovery_fixture.Session
    ).commit_plan(recovery_fixture.plan)
    assert outcome.blocked is False
    _expire_lease(recovery_fixture)
    application, pipeline = _recovery_application(recovery_fixture, monkeypatch)
    completed: list[RunResult] = []
    monkeypatch.setattr(
        application,
        "_completion_handler",
        lambda _task_id, _payload: completed.append,
    )

    run_one_generation_task(
        application_service=application,
        worker_id="recovery-worker",
        lease_seconds=30,
    )

    assert pipeline.calls == []
    assert len(completed) == 1
    assert completed[0].project_id == recovery_fixture.project_id
    assert completed[0].completed_chapters == [1]
    assert completed[0].status == "completed"


def test_stale_worker_epoch_cannot_enter_canon_transaction(
    recovery_fixture: RecoveryFixture,
) -> None:
    _claim(recovery_fixture)
    with recovery_fixture.Session() as session:
        first_claim = session.get(GenerationTask, recovery_fixture.task_id)
        assert first_claim is not None
        first_epoch = int(first_claim.lease_epoch or 0)
    _expire_lease(recovery_fixture)
    with recovery_fixture.Session.begin() as session:
        reclaimed = claim_generation_task(
            session,
            worker_id="crashed-worker",
            lease_seconds=30,
        )
    assert reclaimed is not None
    assert reclaimed.lease_epoch == first_epoch + 1
    application = GenerationApplicationService(
        session_factory=recovery_fixture.Session,
        infrastructure=InfrastructureConfig(database_url=recovery_fixture.database_url),
    )

    outcome = CanonAdmissionService(
        session_factory=recovery_fixture.Session,
        transaction_guard=application._canon_transaction_guard(
            task_id=recovery_fixture.task_id,
            worker_id="crashed-worker",
            lease_epoch=first_epoch,
        ),
    ).commit_plan(recovery_fixture.plan)

    assert outcome.blocked is True
    assert outcome.stale is True
    assert "lease lost" in outcome.failure_reason
    with recovery_fixture.Session() as session:
        assert session.scalar(select(func.count(CanonCommitRecord.id))) == 0
        candidate = session.get(CandidateDraftRecord, recovery_fixture.candidate_id)
        assert candidate is not None and candidate.status == "ready_for_canon"


def test_new_capacity_wait_does_not_recover_old_chapter_as_its_own(recovery_fixture):
    import json

    from forwin.application.generation import EnqueueGenerationCommand
    from forwin.models.project import Project

    fixture = recovery_fixture
    outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        fixture.plan
    )
    assert outcome.commit_id
    with fixture.Session.begin() as session:
        session.get(GenerationTask, fixture.task_id).status = "completed"
        session.get(Project, fixture.project_id).automation_json = "{}"
    service = GenerationApplicationService(
        session_factory=fixture.Session,
        infrastructure=InfrastructureConfig(database_url=fixture.database_url),
        runner=lambda *args: (_ for _ in ()).throw(
            AssertionError("blocked task must not run pipeline")
        ),
    )
    handle = service.enqueue(
        EnqueueGenerationCommand(
            project_id=fixture.project_id,
            requested_chapters=1,
            max_chapters=1,
            run_until_chapter=2,
            auto_continue=False,
            title="Continue",
            subtitle="",
            root_event_type="continue_requested",
        )
    )
    with fixture.Session() as session:
        assert session.get(GenerationTask, handle.task_id).status == "capacity_wait"
    result = run_one_generation_task(
        application_service=service, worker_id="review-probe"
    )
    assert result.task_id == handle.task_id
    with fixture.Session() as session:
        task = session.get(GenerationTask, handle.task_id)
        assert task.status == "capacity_wait", (
            task.status,
            task.completed_chapters_json,
            task.message,
        )
        assert json.loads(task.completed_chapters_json) == []


def test_offline_canon_publisher_outbox_is_successful_noop(recovery_fixture):
    import json
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from forwin.canon.outbox_events import CANON_PUBLISHER_REQUESTED
    from forwin.models.canon import CanonCommitRecord
    from forwin.models.draft import CandidateDraftRecord
    from forwin.outbox.worker import OutboxClaim
    from forwin.production.capacity import SerialCapacityService
    from forwin.publisher_runtime.canon_jobs import (
        CanonPublisherJobService,
        build_canon_publisher_outbox_handlers,
    )

    fixture = recovery_fixture
    with fixture.Session.begin() as session:
        task = session.get(GenerationTask, fixture.task_id)
        task.status = "running"
        task.lease_owner = "worker"
        task.lease_epoch = 1
        task.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        payload = json.loads(task.execution_payload_json)
        payload.update(long_run_mode="soak_test", isolated=True)
        task.execution_payload_json = json.dumps(payload)
        owner = SerialCapacityService(session)
        owner.reserve(
            fixture.project_id, 1, task_id=task.id, worker_id="worker", lease_epoch=1
        )
        candidate = session.get(CandidateDraftRecord, fixture.candidate_id)
        metadata = json.loads(candidate.metadata_json)
        metadata.update(owner.candidate_provenance(fixture.project_id, 1))
        candidate.metadata_json = json.dumps(metadata)
    outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        fixture.plan
    )
    assert outcome.commit_id
    with fixture.Session() as session:
        assert (
            session.get(CanonCommitRecord, outcome.commit_id).production_mode
            == "soak_test"
        )
    event = next(
        event
        for event in fixture.plan.outbox_events
        if event.event_type == CANON_PUBLISHER_REQUESTED
    )
    assert event.payload["publisher_bindings"] == []
    service = CanonPublisherJobService(
        session_factory=fixture.Session, upload_jobs=SimpleNamespace()
    )
    handler = build_canon_publisher_outbox_handlers(service_provider=lambda: service)[
        CANON_PUBLISHER_REQUESTED
    ]
    handler(
        OutboxClaim(
            row_id="probe",
            event_id=event.event_id,
            event_type=event.event_type,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            payload=event.payload,
            worker_id="probe",
            lease_epoch=1,
            attempts=1,
        )
    )


def test_capacity_wait_resumes_its_prepared_candidate_without_writer(
    recovery_fixture, monkeypatch
):
    import json

    from forwin.models.project import Project

    fixture = recovery_fixture
    with fixture.Session.begin() as session:
        candidate = session.get(CandidateDraftRecord, fixture.candidate_id)
        metadata = json.loads(candidate.metadata_json or "{}")
        metadata["generation_task_id"] = fixture.task_id
        candidate.metadata_json = json.dumps(metadata)
        task = session.get(GenerationTask, fixture.task_id)
        task.status = "capacity_wait"
        task.resume_from_chapter = 1
        task.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.get(Project, fixture.project_id).automation_json = "{}"
    application, pipeline = _recovery_application(fixture, monkeypatch)
    first = run_one_generation_task(application_service=application, worker_id="wait-1")
    assert first.message == "capacity_wait"
    with fixture.Session.begin() as session:
        assert (
            session.get(CandidateDraftRecord, fixture.candidate_id).status
            == "ready_for_canon"
        )
        assert session.scalar(select(func.count(CanonCommitRecord.id))) == 0
        task = session.get(GenerationTask, fixture.task_id)
        task.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.get(
            Project, fixture.project_id
        ).automation_json = '{"primary_publish_platform":"qidian"}'
    run_one_generation_task(application_service=application, worker_id="wait-2")
    assert pipeline.calls == []
    with fixture.Session() as session:
        task = session.get(GenerationTask, fixture.task_id)
        assert task.status == "completed"
        assert json.loads(task.completed_chapters_json) == [1]
        assert session.scalar(select(func.count(CanonCommitRecord.id))) == 1


def test_enqueued_start_does_not_override_recorded_progress_on_crash(recovery_fixture):
    from datetime import UTC, datetime, timedelta

    from forwin.application.generation import EnqueueGenerationCommand
    from forwin.generation.task_lease import (
        claim_generation_task,
        generation_task_resume_from_chapter,
    )
    from forwin.models.project import Project

    fixture = recovery_fixture
    CanonAdmissionService(session_factory=fixture.Session).commit_plan(fixture.plan)
    with fixture.Session.begin() as session:
        session.get(GenerationTask, fixture.task_id).status = "completed"
        session.get(Project, fixture.project_id).target_total_chapters = 100
    service = GenerationApplicationService(
        session_factory=fixture.Session,
        infrastructure=InfrastructureConfig(database_url=fixture.database_url),
    )
    handle = service.enqueue(
        EnqueueGenerationCommand(
            project_id=fixture.project_id,
            requested_chapters=3,
            max_chapters=3,
            run_until_chapter=4,
            auto_continue=False,
            title="Continue",
            subtitle="",
            root_event_type="continue_requested",
        )
    )
    with fixture.Session.begin() as session:
        claim = claim_generation_task(session, worker_id="crashed")
        assert claim.task.id == handle.task_id
    # Real task progress persists through the guarded application update owner.
    service._task_updater(worker_id="crashed", lease_epoch=claim.lease_epoch)(
        handle.task_id, completed_chapters=[2], current_chapter=3
    )
    with fixture.Session.begin() as session:
        task = session.get(GenerationTask, handle.task_id)
        task.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with fixture.Session.begin() as session:
        claim = claim_generation_task(session, worker_id="reclaimed")
        assert claim.claim_kind == "expired_running"
        assert generation_task_resume_from_chapter(claim.task) == 3
