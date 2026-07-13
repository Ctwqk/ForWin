from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from forwin.candidate_drafts import (
    CandidateDraftRepository,
    candidate_plan_revision,
)
from forwin.canon.admission import CanonAdmissionService
from forwin.canon.preparation import CanonPreparationService
from forwin.config import InfrastructureConfig
from forwin.generation.task_lease import claim_generation_task
from forwin.generation.task_repository import GenerationTaskRepository
from forwin.generation.worker import run_one_generation_task
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
from forwin.naming import EntityAdmissionDecision, EntityAdmissionPlan
from forwin.protocol.book_state import ApprovedGraphDeltaSet, GraphDelta, NodePatch
from forwin.protocol.writer import WriterOutput
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url


@dataclass(frozen=True)
class RecoveryFixture:
    Session: sessionmaker[Session]
    database_url: str
    plan: object
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
        project = updater.create_project(
            title="Worker Canon Recovery",
            premise="A reclaimed worker must commit exactly once.",
            genre="thriller",
            runtime_policy=RuntimePolicy.for_profile("standard"),
        )
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
            candidate_fingerprint=candidate.body_hash,
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
    expired = (datetime.now(timezone.utc) - timedelta(minutes=5)).replace(tzinfo=None)
    with fixture.Session.begin() as session:
        task = session.get(GenerationTask, fixture.task_id)
        assert task is not None
        task.lease_expires_at = expired
        task.heartbeat_at = expired
        session.add(task)


def _recovery_application(
    fixture: RecoveryFixture,
    outcomes: list[object],
):
    admission = CanonAdmissionService(session_factory=fixture.Session)

    def execute_claimed(task, *, resume_from_chapter: int, worker_id: str) -> None:
        assert task.id == fixture.task_id
        assert resume_from_chapter == 1
        assert worker_id == "recovery-worker"
        outcome = admission.commit_plan(fixture.plan)
        assert outcome.blocked is False
        outcomes.append(outcome)
        with fixture.Session.begin() as session:
            GenerationTaskRepository(session).update(
                fixture.task_id,
                {
                    "status": "completed",
                    "current_stage": "completed",
                    "current_chapter": 1,
                    "completed_chapters": [1],
                },
            )

    return SimpleNamespace(
        session_factory=fixture.Session,
        infrastructure=InfrastructureConfig(database_url=fixture.database_url),
        execute_claimed=execute_claimed,
    )


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
        assert session.scalar(select(func.count(OutboxEvent.id))) == 2


def test_reclaim_after_precommit_crash_commits_candidate_once(
    recovery_fixture: RecoveryFixture,
) -> None:
    _claim(recovery_fixture)
    with recovery_fixture.Session() as session:
        candidate = session.get(CandidateDraftRecord, recovery_fixture.candidate_id)
        assert candidate is not None and candidate.status == "ready_for_canon"
    _expire_lease(recovery_fixture)
    outcomes: list[object] = []

    result = run_one_generation_task(
        application_service=_recovery_application(recovery_fixture, outcomes),
        worker_id="recovery-worker",
        lease_seconds=30,
    )

    assert result.claimed is True
    assert result.task_id == recovery_fixture.task_id
    assert len(outcomes) == 1
    assert outcomes[0].idempotent is False
    _assert_single_committed_state(recovery_fixture)


def test_reclaim_after_canon_commit_replays_once_then_finishes_task(
    recovery_fixture: RecoveryFixture,
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
    outcomes: list[object] = []

    result = run_one_generation_task(
        application_service=_recovery_application(recovery_fixture, outcomes),
        worker_id="recovery-worker",
        lease_seconds=30,
    )

    assert result.claimed is True
    assert len(outcomes) == 1
    assert outcomes[0].commit_id == first.commit_id
    assert outcomes[0].idempotent is True
    _assert_single_committed_state(recovery_fixture)
