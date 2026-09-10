"""Revision and publication guards preserve accepted identity and evidence."""

import json

import pytest
from sqlalchemy import select

from forwin.canon.admission import CanonAdmissionService
from forwin.canon.historical_rewrite import (
    HistoricalCanonRewriteRepository,
    HistoricalRewriteInvalid,
)
from forwin.models.canon import CanonCommitRecord
from forwin.models.project import ChapterPlan, Project
from tests import test_canon_atomic_transaction as atomic_tests

prepared_canon = atomic_tests.prepared_canon


def test_acceptance_assigns_stable_identity_and_book_revision(prepared_canon):
    fixture = prepared_canon
    outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        fixture.plan
    )
    assert outcome.commit_id
    with fixture.Session() as session:
        chapter = session.get(ChapterPlan, fixture.chapter_plan_id)
        commit = session.get(CanonCommitRecord, outcome.commit_id)
        assert getattr(chapter, "active_commit_id", None) == commit.id
        assert getattr(commit, "chapter_plan_id", None) == chapter.id
        assert getattr(commit, "acceptance_revision", None) == 1
        assert (
            getattr(session.get(Project, fixture.project_id), "book_revision", None)
            == 1
        )
    replay = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        fixture.plan
    )
    assert replay.idempotent
    with fixture.Session() as session:
        assert session.get(Project, fixture.project_id).book_revision == 1


def test_revision_request_is_idempotent_and_retains_original_acceptance(prepared_canon):
    fixture = prepared_canon
    outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        fixture.plan
    )
    assert outcome.commit_id
    with fixture.Session.begin() as session:
        repository = HistoricalCanonRewriteRepository(session)
        first = repository.mark_pending(
            project_id=fixture.project_id, chapter_number=1, reason="Revise wording"
        )
        second = repository.mark_pending(
            project_id=fixture.project_id, chapter_number=1, reason="Retry request"
        )
        assert first.id == second.id
        chapter = session.get(ChapterPlan, fixture.chapter_plan_id)
        assert chapter.status == "accepted"
        assert chapter.active_commit_id == outcome.commit_id
        assert json.loads(first.payload_json)["base_book_revision"] == 1


def test_retry_api_reports_retained_accepted_status(prepared_canon):
    from forwin.api_schema import ChapterReviewRetryRequest
    from forwin.application.projects.reviews import retry_chapter_review

    fixture = prepared_canon
    CanonAdmissionService(session_factory=fixture.Session).commit_plan(fixture.plan)
    response = retry_chapter_review(
        fixture.project_id,
        1,
        ChapterReviewRetryRequest(reason="revision", allow_accepted=True),
        config=object(),
        get_session=fixture.Session,
        active_generation_task_error_cls=RuntimeError,
        require_reason=lambda reason, **kwargs: reason,
        project_has_active_generation_task=lambda *args, **kwargs: False,
        generation_task_conflict_message=lambda _: "conflict",
        log_decision_event=lambda *args, **kwargs: None,
        create_continue_generation_task=lambda **kwargs: "unused",
    )
    assert response.status == "accepted"


def test_historical_acceptance_fails_closed_without_full_suffix_validation(
    prepared_canon,
):
    from forwin.canon.historical_rewrite import HistoricalCanonRewriteService

    fixture = prepared_canon
    assert (
        CanonAdmissionService(session_factory=fixture.Session)
        .commit_plan(fixture.plan)
        .commit_id
    )
    with fixture.Session.begin() as session:
        with pytest.raises(HistoricalRewriteInvalid, match="full.suffix validation"):
            HistoricalCanonRewriteService(session).require_first_acceptance(fixture.plan)
        assert session.scalar(select(CanonCommitRecord.chapter_number)) == 1


@pytest.mark.parametrize("state", ["reserved", "published"])
def test_publication_protection_survives_job_and_binding_deletion(
    prepared_canon, state
):
    from forwin.models import canon

    fixture = prepared_canon
    outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        fixture.plan
    )
    with fixture.Session.begin() as session:
        protection_type = getattr(canon, "CanonPublicationProtection", None)
        assert protection_type is not None, (
            "publication protection needs independent durable evidence"
        )
        session.add(
            protection_type(
                project_id=fixture.project_id,
                chapter_plan_id=fixture.chapter_plan_id,
                chapter_number=1,
                canon_commit_id=outcome.commit_id,
                platform_id="fanqie",
                upload_job_id="deleted-job",
                state=state,
                content_sha256=fixture.plan.candidate_body_hash,
            )
        )
    with (
        fixture.Session.begin() as session,
        pytest.raises(HistoricalRewriteInvalid, match="publication"),
    ):
        HistoricalCanonRewriteRepository(session).mark_pending(
            project_id=fixture.project_id, chapter_number=1, reason="retry"
        )


def test_active_readers_ignore_immutable_prior_commit_for_same_candidate(
    prepared_canon,
):
    from forwin.knowledge_system.checkpoints import latest_projection_target
    from forwin.maintenance.post_canon import PostCanonMaintenanceService

    fixture = prepared_canon
    outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        fixture.plan
    )
    with fixture.Session.begin() as session:
        previous = session.get(CanonCommitRecord, outcome.commit_id)
        replacement = CanonCommitRecord(
            id="context-revision",
            idempotency_key="context-revision",
            candidate_id=previous.candidate_id,
            project_id=fixture.project_id,
            chapter_plan_id=fixture.chapter_plan_id,
            chapter_number=1,
            acceptance_revision=2,
            base_book_revision=1,
        )
        session.add(replacement)
        session.flush()
        session.get(
            ChapterPlan, fixture.chapter_plan_id
        ).active_commit_id = replacement.id
    with fixture.Session() as session:
        assert (
            latest_projection_target(session, fixture.project_id).canon_commit_id
            == "context-revision"
        )
        assert session.get(CanonCommitRecord, outcome.commit_id).status == "committed"
    service = PostCanonMaintenanceService(
        session_factory=fixture.Session,
        stage_analyzer=None,
        pacing_strategist=None,
        replan_governor=None,
        arc_envelope_manager=None,
        world_simulator=None,
        artifact_store=None,
        llm_client=None,
    )
    assert (
        service.resolve_canon_commit(project_id=fixture.project_id, chapter_number=1)
        == "context-revision"
    )


def test_graph_delta_range_reads_only_active_commit_manifest(prepared_canon):
    from forwin.book_state.repository import BookStateRepository

    fixture = prepared_canon
    outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        fixture.plan
    )
    with fixture.Session.begin() as session:
        previous = session.get(CanonCommitRecord, outcome.commit_id)
        assert json.loads(previous.graph_delta_ids_json)
        replacement = CanonCommitRecord(
            id="empty-context",
            idempotency_key="empty-context",
            candidate_id=previous.candidate_id,
            project_id=fixture.project_id,
            chapter_plan_id=fixture.chapter_plan_id,
            chapter_number=1,
            acceptance_revision=2,
            base_book_revision=1,
            graph_delta_ids_json="[]",
        )
        session.add(replacement)
        session.flush()
        session.get(
            ChapterPlan, fixture.chapter_plan_id
        ).active_commit_id = replacement.id
    with fixture.Session() as session:
        assert (
            BookStateRepository(session).list_graph_deltas(
                fixture.project_id, through_chapter=1
            )
            == []
        )


def test_database_rejects_active_commit_owned_by_another_chapter(prepared_canon):
    from sqlalchemy.exc import IntegrityError

    fixture = prepared_canon
    outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        fixture.plan
    )
    with pytest.raises(IntegrityError), fixture.Session.begin() as session:
        chapter = session.get(ChapterPlan, fixture.chapter_plan_id)
        session.add(
            ChapterPlan(
                project_id=chapter.project_id,
                arc_plan_id=chapter.arc_plan_id,
                chapter_number=2,
                active_commit_id=outcome.commit_id,
            )
        )
        session.flush()


def _accepted_fixture(request, backend):
    if backend == "postgres":
        fixture = request.getfixturevalue("prepared_canon")
        outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
            fixture.plan
        )
        return fixture, outcome
    from types import SimpleNamespace

    from tests.test_post_canon_maintenance import _durable_service

    _, sessions, _ = _durable_service()
    return SimpleNamespace(
        Session=sessions,
        project_id="project-1",
        chapter_plan_id="plan-1",
        plan=SimpleNamespace(candidate_body_hash="a" * 64),
    ), SimpleNamespace(commit_id="canon-1")


@pytest.mark.parametrize("backend", ["postgres", "sqlite"])
def test_legacy_status_reset_cannot_invalidate_active_acceptance(request, backend):
    from forwin.state.updater import StateUpdater

    fixture, outcome = _accepted_fixture(request, backend)
    with (
        fixture.Session.begin() as session,
        pytest.raises(ValueError, match="revision"),
    ):
        StateUpdater(session).mark_chapter_status(fixture.project_id, 1, "planned")
    with fixture.Session() as session:
        chapter = session.get(ChapterPlan, fixture.chapter_plan_id)
        assert chapter.status == "accepted"
        assert chapter.active_commit_id == outcome.commit_id


@pytest.mark.parametrize("backend", ["postgres", "sqlite"])
def test_world_edit_cannot_rewrite_a_public_chapters_facts(request, backend):
    from forwin.models.canon import CanonPublicationProtection
    from forwin.models.knowledge import KnowledgeEditProposalRow
    from forwin.protocol.book_state import ApprovedGraphDeltaSet

    fixture, outcome = _accepted_fixture(request, backend)
    with fixture.Session.begin() as session:
        session.add(
            CanonPublicationProtection(
                project_id=fixture.project_id,
                chapter_plan_id=fixture.chapter_plan_id,
                chapter_number=1,
                canon_commit_id=outcome.commit_id,
                platform_id="fanqie",
                upload_job_id="public-job",
                state="published",
                content_sha256=fixture.plan.candidate_body_hash,
            )
        )
        session.add(
            KnowledgeEditProposalRow(
                id="world-edit", project_id=fixture.project_id, status="pending"
            )
        )
    with fixture.Session.begin() as session:
        with pytest.raises(ValueError, match="publication protection"):
            CanonAdmissionService().commit_world_edit(
                session=session,
                project_id=fixture.project_id,
                proposal_id="world-edit",
                approved_changes=ApprovedGraphDeltaSet(
                    project_id=fixture.project_id, chapter_number=1, graph_deltas=[]
                ),
                reason="rewrite past facts",
                trigger="test",
            )
        assert session.get(KnowledgeEditProposalRow, "world-edit").status == "pending"


@pytest.mark.parametrize("backend", ["postgres", "sqlite"])
def test_snapshot_reader_ignores_inactive_acceptance_evidence(request, backend):
    from forwin.book_state.repository import BookStateRepository
    from forwin.models.book_state import MapSnapshotRow, WorldSnapshotRow

    fixture, outcome = _accepted_fixture(request, backend)
    with fixture.Session.begin() as session:
        prior = session.get(CanonCommitRecord, outcome.commit_id)
        if not prior.world_snapshot_id:
            session.add(
                WorldSnapshotRow(
                    id="old-world", project_id=fixture.project_id, as_of_chapter=1
                )
            )
            session.add(
                MapSnapshotRow(
                    id="old-map", project_id=fixture.project_id, as_of_chapter=1
                )
            )
            prior.world_snapshot_id = "old-world"
            prior.map_snapshot_id = "old-map"
        replacement = CanonCommitRecord(
            id="new-snapshot-context",
            idempotency_key="new-snapshot-context",
            project_id=fixture.project_id,
            chapter_plan_id=fixture.chapter_plan_id,
            chapter_number=1,
            candidate_id=prior.candidate_id,
            acceptance_revision=2,
        )
        session.add(replacement)
        session.flush()
        session.get(
            ChapterPlan, fixture.chapter_plan_id
        ).active_commit_id = replacement.id
    with fixture.Session() as session:
        assert (
            BookStateRepository(session).latest_world_snapshot(fixture.project_id, 1)
            is None
        )
        assert (
            BookStateRepository(session).latest_map_snapshot(fixture.project_id, 1)
            is None
        )


def test_repair_cannot_edit_active_accepted_plan_title():

    from forwin.protocol.review import RepairInstruction
    from forwin.review.repair.plan_patch import (
        RepairPlanPatchRequest,
        RepairPlanPatchService,
    )
    from forwin.state.repo import StateRepository
    from tests.test_post_canon_maintenance import _durable_service

    _, sessions, _ = _durable_service()
    owner = RepairPlanPatchService(retrieval_broker=None, arc_envelope_manager=None)
    with sessions.begin() as session:
        chapter = session.get(ChapterPlan, "plan-1")
        original_title = chapter.title
        with pytest.raises(ValueError, match="revision"):
            owner.apply(RepairPlanPatchRequest(
                session=session,
                repo=StateRepository(session),
                project_id="project-1",
                chapter_plan=chapter,
                context=None,
                repair_scope="chapter_plan",
                instruction=RepairInstruction(
                    repair_scope="chapter_plan",
                    failure_type="continuity",
                    design_patch={"title": "Changed public title"},
                ),
            ))
        assert chapter.title == original_title


def test_public_mutation_requires_every_predecessor_confirmed_on_its_platform():
    from forwin.candidate_drafts import candidate_body_hash
    from forwin.models.canon import CanonPublicationProtection
    from forwin.models.draft import CandidateDraftRecord
    from forwin.models.publisher import PublisherUploadJob
    from forwin.publisher_runtime.protection import reserve_publication
    from tests.test_post_canon_maintenance import _durable_service

    _, sessions, _ = _durable_service()
    with sessions.begin() as session:
        chapter = session.get(ChapterPlan, "plan-1")
        chapter.chapter_number = 2
        commit = session.get(CanonCommitRecord, "canon-1")
        commit.chapter_number = 2
        candidate = session.get(CandidateDraftRecord, "candidate-1")
        candidate.chapter_number = 2
        candidate.body_hash = candidate_body_hash("body")
        job = PublisherUploadJob(
            id="public-job",
            project_id="project-1",
            chapter_number=2,
            canon_commit_id="canon-1",
            candidate_id="candidate-1",
            platform_id="qidian",
            body_text="body",
            body_sha256=candidate.body_hash,
            publish=True,
            chapter_title="",
        )
        session.add(job)
        session.flush()
        with pytest.raises(ValueError, match="publication prefix"):
            reserve_publication(session, job)
        # A confirmed chapter on another platform cannot authorize this action.
        session.add(
            CanonPublicationProtection(
                project_id="project-1",
                chapter_plan_id="prior-plan",
                chapter_number=1,
                canon_commit_id="prior-commit",
                platform_id="fanqie",
                upload_job_id="other-platform",
                state="published",
                content_sha256="a" * 64,
            )
        )
        session.flush()
        with pytest.raises(ValueError, match="publication prefix"):
            reserve_publication(session, job)
        prior = CanonPublicationProtection(
            project_id="project-1",
            chapter_plan_id="prior-plan",
            chapter_number=1,
            canon_commit_id="prior-commit",
            platform_id="qidian",
            upload_job_id="unknown-first",
            state="reserved",
            content_sha256="a" * 64,
        )
        session.add(prior)
        session.flush()
        with pytest.raises(ValueError, match="publication prefix"):
            reserve_publication(session, job)
        prior.state = "published"
        session.flush()
        reserve_publication(session, job)
        assert (
            session.scalar(
                select(CanonPublicationProtection.state).where(
                    CanonPublicationProtection.upload_job_id == job.id
                )
            )
            == "reserved"
        )


def test_prepared_acceptance_rejects_changed_book_revision(prepared_canon):
    fixture = prepared_canon
    with fixture.Session.begin() as session:
        session.get(Project, fixture.project_id).book_revision += 1
    outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        fixture.plan
    )
    assert outcome.stale
    assert "book revision" in outcome.failure_reason
    with fixture.Session() as session:
        assert (
            session.get(ChapterPlan, fixture.chapter_plan_id).active_commit_id is None
        )


def test_accepted_chapter_read_uses_active_draft_instead_of_new_candidate(
    prepared_canon,
):
    from forwin.application.projects.chapters import get_chapter
    from forwin.models.draft import CandidateDraftRecord, ChapterDraft
    from forwin.state.query_helpers import load_latest_drafts_by_plan_id

    fixture = prepared_canon
    outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        fixture.plan
    )
    with fixture.Session.begin() as session:
        commit = session.get(CanonCommitRecord, outcome.commit_id)
        candidate = session.get(CandidateDraftRecord, commit.candidate_id)
        accepted = session.get(ChapterDraft, candidate.candidate_draft_id)
        original_id, original_body = accepted.id, accepted.body_text
        session.add(
            ChapterDraft(
                chapter_plan_id=fixture.chapter_plan_id,
                version=accepted.version + 1,
                body_text="Unaccepted replacement must remain private",
                char_count=44,
            )
        )
    with fixture.Session() as session:
        assert (
            load_latest_drafts_by_plan_id(session, [fixture.chapter_plan_id])[
                fixture.chapter_plan_id
            ].id
            == original_id
        )
    detail = get_chapter(fixture.project_id, 1, get_session=fixture.Session)
    assert detail.status == "accepted"
    assert detail.body == original_body
