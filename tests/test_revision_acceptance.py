"""Durable validation is consumed atomically by the sole live Canon owner."""

import json

import pytest
from sqlalchemy import select

from forwin.canon.admission import CanonAdmissionService
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord
from forwin.models.project import ChapterPlan, Project
from tests import test_canon_atomic_transaction as atomic_tests

prepared_canon = atomic_tests.prepared_canon


def test_revision_proposal_saves_distinct_unreviewed_body_and_binds_baseline(
    prepared_canon,
):
    from forwin.canon.revision_service import save_revision_proposal
    from forwin.models.draft import CandidateDraftRecord, ChapterDraft

    fixture = prepared_canon
    outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        fixture.plan
    )
    with fixture.Session.begin() as session:
        proposal = save_revision_proposal(
            session,
            project_id=fixture.project_id,
            chapter_number=1,
            body="A careful wording change.",
            title="Revised title",
            expected_book_revision=1,
        )
        proposal_id = proposal.id
        assert proposal.status == "drafted"
        assert proposal.id != fixture.plan.candidate_id
        assert proposal.state_change_candidates_json == "[]"
        assert proposal.canon_commit_plan_json == "{}"
        assert json.loads(proposal.metadata_json)["revision_base_book_revision"] == 1
        again = save_revision_proposal(
            session,
            project_id=fixture.project_id,
            chapter_number=1,
            body="A careful wording change.",
            title="Revised title",
            expected_book_revision=1,
        )
        assert again.id == proposal.id
    with fixture.Session() as session:
        assert (
            session.get(ChapterPlan, fixture.chapter_plan_id).active_commit_id
            == outcome.commit_id
        )
        assert session.get(ChapterPlan, fixture.chapter_plan_id).status == "accepted"
        proposal = session.get(CandidateDraftRecord, proposal_id)
        assert (
            session.get(ChapterDraft, proposal.candidate_draft_id).body_text
            == "A careful wording change."
        )


def test_revision_proposal_rejects_explicit_stale_book_revision(prepared_canon):
    from forwin.canon.revision_service import save_revision_proposal

    fixture = prepared_canon
    CanonAdmissionService(session_factory=fixture.Session).commit_plan(fixture.plan)
    with (
        fixture.Session.begin() as session,
        pytest.raises(ValueError, match="book revision"),
    ):
        save_revision_proposal(
            session,
            project_id=fixture.project_id,
            chapter_number=1,
            body="Revised body.",
            expected_book_revision=0,
        )


def test_validation_model_has_durable_identity_and_result():
    from forwin.models.canon import CanonRevisionValidationRecord

    assert {
        "id",
        "project_id",
        "candidate_id",
        "base_book_revision",
        "status",
        "result_json",
        "accepted_book_revision",
    }.issubset(CanonRevisionValidationRecord.__table__.columns.keys())


def test_full_body_validation_persists_unknown_without_model_and_keeps_mainline(
    prepared_canon,
):
    from types import SimpleNamespace

    from forwin.canon.revision_service import (
        RevisionValidationService,
        save_revision_proposal,
    )
    from forwin.models.canon import CanonRevisionValidationRecord
    from forwin.runtime.policy import RuntimePolicy

    fixture = prepared_canon
    before = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        fixture.plan
    )
    with fixture.Session.begin() as session:
        proposal = save_revision_proposal(
            session,
            project_id=fixture.project_id,
            chapter_number=1,
            body="Revised body.",
        )
        proposal_id = proposal.id
    service = RevisionValidationService(
        session_factory=fixture.Session,
        writer=SimpleNamespace(llm_client=None),
        policy=RuntimePolicy.for_profile("standard"),
    )
    outcome = service.prepare(project_id=fixture.project_id, candidate_id=proposal_id)
    assert outcome.blocked
    with fixture.Session() as session:
        record = session.scalar(select(CanonRevisionValidationRecord))
        assert record.status == "unknown"
        assert (
            session.get(ChapterPlan, fixture.chapter_plan_id).active_commit_id
            == before.commit_id
        )


def test_revision_atomic_owner_refuses_unreviewed_origin_obligation_disposition(
    prepared_canon,
):
    from forwin.canon.revision_replica import capture_revision
    from forwin.canon.revision_service import (
        revision_commit_plan,
        revision_policy_fingerprint,
        save_revision_proposal,
    )
    from forwin.canon.revision_validation import (
        REQUIRED_REVISION_CHECKS,
        RevisionChapterAssessment,
        RevisionCheck,
        assess_revision,
    )
    from forwin.models.book_state import GraphDeltaRow, WorldSnapshotRow
    from forwin.models.canon import CanonRevisionValidationRecord

    fixture = prepared_canon
    service = CanonAdmissionService(session_factory=fixture.Session)
    old = service.commit_plan(fixture.plan)
    model = {"provider": "fixture", "model": "frozen"}
    with fixture.Session.begin() as session:
        proposal = save_revision_proposal(
            session,
            project_id=fixture.project_id,
            chapter_number=1,
            body="Shen Linchuan quietly enters the archive.",
        )
        snapshot = capture_revision(
            session,
            project_id=fixture.project_id,
            candidate_id=proposal.id,
            model_identity=model,
            policy_fingerprint=revision_policy_fingerprint(
                session.get(Project, fixture.project_id)
            ),
        )
        approved = fixture.plan.approved_book_state_changes.model_dump(mode="json")
        for delta in approved["graph_deltas"]:
            delta["id"] = "fresh-" + delta["id"]
        chapter = snapshot.manifest.chapters[0]
        result = assess_revision(
            snapshot.manifest,
            (
                RevisionChapterAssessment(
                    chapter_number=1,
                    body_sha256=chapter.body_sha256,
                    checks=tuple(
                        RevisionCheck(
                            dimension=d,
                            status="pass",
                            explanation="Prepared owner evidence",
                            evidence_refs=(f"body:{chapter.body_sha256}",),
                        )
                        for d in REQUIRED_REVISION_CHECKS
                    ),
                    prepared_changes={
                        "approved_changes": approved,
                        "entity_plan": fixture.plan.entity_admission_plan.model_dump(
                            mode="json"
                        ),
                        "writer_output": {
                            "end_of_chapter_summary": "Shen Linchuan enters the archive."
                        },
                        "review": {},
                        "historical_form": {
                            "review": {
                                "project_id": fixture.project_id,
                                "chapter_number": 1,
                                "draft_id": chapter.draft_id,
                                "blocking": False,
                                "character_transitions": [],
                                "countdown_entries": [],
                                "signals": [],
                            },
                            "checks": [
                                {
                                    "dimension": dimension,
                                    "status": "pass",
                                    "evidence_refs": [
                                        f"body:{chapter.body_sha256}#0:{len(chapter.body)}"
                                    ],
                                }
                                for dimension in (
                                    "possession",
                                    "knowledge",
                                    "life_state",
                                    "time",
                                    "place",
                                    "obligations",
                                )
                            ],
                        },
                    },
                ),
            ),
        )
        proposal.status = "reviewed"
        proposal.review_result_json = json.dumps(
            {"revision_validation_id": result.validation_id, "verdict": "pass"}
        )
        payload = result.model_dump(mode="json")
        for c in payload["manifest"]["chapters"]:
            c.pop("body")
        session.add(
            CanonRevisionValidationRecord(
                id=result.validation_id,
                project_id=fixture.project_id,
                candidate_id=proposal.id,
                base_book_revision=1,
                status="pass",
                result_json=json.dumps(payload),
            )
        )
        plan = revision_commit_plan(session, result, 0)
        old_record = session.get(CanonCommitRecord, old.commit_id)
        old_payload = old_record.result_json
        old_snapshot = old_record.world_snapshot_id
        old_delta = json.loads(old_record.graph_delta_ids_json)[0]
    committed = service.commit_plan(plan, revision_model_identity=model)
    assert committed.blocked
    assert "origin obligation" in committed.blocked_path
    with fixture.Session() as session:
        from forwin.models.narrative_obligation import NarrativeObligationRow

        assert (
            session.get(ChapterPlan, fixture.chapter_plan_id).active_commit_id
            == old.commit_id
        )
        assert session.get(Project, fixture.project_id).book_revision == 1
        assert session.get(CanonCommitRecord, old.commit_id).result_json == old_payload
        assert session.get(WorldSnapshotRow, old_snapshot) is not None
        assert session.get(GraphDeltaRow, old_delta) is not None
        obligation = session.get(NarrativeObligationRow, fixture.obligation_id)
        assert obligation.status == "active"
        assert (
            obligation.origin_draft_id
            == session.get(
                CandidateDraftRecord, fixture.candidate_id
            ).candidate_draft_id
        )


def test_retry_api_creates_real_revision_proposal_and_retains_accepted(prepared_canon):
    from forwin.api_schema import ChapterReviewRetryRequest
    from forwin.application.projects.reviews import retry_chapter_review
    from forwin.models.draft import CandidateDraftRecord, ChapterDraft

    fixture = prepared_canon
    old = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        fixture.plan
    )
    response = retry_chapter_review(
        fixture.project_id,
        1,
        ChapterReviewRetryRequest(
            reason="wording",
            allow_accepted=True,
            replacement_body="The accepted story, carefully reworded.",
            replacement_title="Reworded",
            expected_book_revision=1,
        ),
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
    with fixture.Session() as session:
        proposals = list(
            session.scalars(
                select(CandidateDraftRecord).where(
                    CandidateDraftRecord.id != fixture.plan.candidate_id
                )
            )
        )
        assert len(proposals) == 1
        assert response.candidate_id == proposals[0].id
        assert response.book_revision == 1
        assert (
            session.get(ChapterDraft, proposals[0].candidate_draft_id).body_text
            == "The accepted story, carefully reworded."
        )
        assert (
            session.get(ChapterPlan, fixture.chapter_plan_id).active_commit_id
            == old.commit_id
        )


@pytest.mark.parametrize("revision_status", ["accepted", "blocked"])
def test_approve_continue_uses_revision_result_instead_of_retained_mainline_status(
    prepared_canon, monkeypatch, revision_status
):
    from types import SimpleNamespace

    from forwin.api_schema import ChapterReviewApproveRequest
    from forwin.application.projects import reviews

    fixture = prepared_canon
    CanonAdmissionService(session_factory=fixture.Session).commit_plan(fixture.plan)
    monkeypatch.setattr(
        reviews,
        "build_project_detail",
        lambda **kwargs: SimpleNamespace(blocking_reason=SimpleNamespace(code="")),
    )
    monkeypatch.setattr(
        reviews,
        "build_continue_generation_workset",
        lambda *args, **kwargs: SimpleNamespace(requested_chapters=1),
    )

    class Pipeline:
        def accept_review(self, *args, **kwargs):
            return {
                "status": "accepted",
                "revision_status": revision_status,
                "message": "revision result",
            }

    tasks = []
    response = reviews.approve_chapter_review(
        fixture.project_id,
        1,
        ChapterReviewApproveRequest(reason="review", continue_generation=True),
        config=object(),
        pipeline=Pipeline(),
        get_session=fixture.Session,
        display_datetime=lambda x: x,
        active_generation_task_error_cls=RuntimeError,
        require_reason=lambda reason, **kwargs: reason,
        project_has_active_generation_task=lambda *args, **kwargs: False,
        generation_task_conflict_message=lambda x: "busy",
        log_decision_event=lambda *args, **kwargs: None,
        create_continue_generation_task=lambda **kwargs: (
            tasks.append(kwargs) or "new-task"
        ),
        update_task=lambda *args, **kwargs: None,
    )
    assert response.status == "accepted"
    assert response.candidate_id == fixture.plan.candidate_id
    assert response.book_revision == 1
    assert bool(tasks) is (revision_status == "accepted")


def test_missing_accepted_tail_identity_is_durable_unknown_and_never_shortens_validation(
    prepared_canon,
):
    from forwin.canon.revision_service import (
        RevisionValidationService,
        save_revision_proposal,
    )
    from forwin.models.canon import CanonRevisionValidationRecord
    from forwin.runtime.policy import RuntimePolicy
    from forwin.writer import ChapterWriter
    from tests.test_revision_full_suffix import BodyModel, _book

    fixture = prepared_canon
    ids, body, old_ids, _ = _book(fixture)
    with fixture.Session.begin() as session:
        proposal = save_revision_proposal(
            session,
            project_id=ids[0],
            chapter_number=1,
            body=body.replace("安静地", "静静地"),
        )
        candidate_id = proposal.id
        session.get(ChapterPlan, ids[2]).active_commit_id = None
    model = BodyModel()
    prepared = RevisionValidationService(
        session_factory=fixture.Session,
        writer=ChapterWriter(model),
        policy=RuntimePolicy.for_profile("standard"),
    ).prepare(project_id=ids[0], candidate_id=candidate_id)
    assert prepared.blocked, "accepted tail with missing identity must never be omitted"
    assert not model.calls, "unproven prefix cannot enter semantic validation"
    with fixture.Session() as session:
        record = session.get(CanonRevisionValidationRecord, prepared.blocked_path)
        assert record.status == "unknown"
        assert "ownership" in record.result_json or "identity" in record.result_json
        assert session.get(ChapterPlan, ids[1]).active_commit_id == old_ids[0]


def test_wording_revision_with_old_origin_promise_keeps_full_ledger_unknown(
    prepared_canon,
):
    from forwin.canon.revision_service import (
        RevisionValidationService,
        save_revision_proposal,
    )
    from forwin.models.audit import DecisionEvent
    from forwin.models.canon import CanonRevisionValidationRecord
    from forwin.models.narrative_obligation import NarrativeObligationRow
    from forwin.runtime.policy import RuntimePolicy
    from forwin.writer import ChapterWriter
    from tests.test_revision_full_suffix import BodyModel, _book

    fixture = prepared_canon
    ids, body, old_ids, _ = _book(fixture, origin_obligation=True)
    assert "承诺" in body

    def ledger(session):
        return [
            (
                row.id,
                row.status,
                row.origin_draft_id,
                row.metadata_json,
                row.resolution_evidence_refs_json,
            )
            for row in session.scalars(
                select(NarrativeObligationRow).where(
                    NarrativeObligationRow.project_id == ids[0]
                )
            )
        ]

    with fixture.Session.begin() as session:
        before = ledger(session)
        proposal = save_revision_proposal(
            session,
            project_id=ids[0],
            chapter_number=1,
            body=body.replace("安静地", "静静地"),
        )
        candidate_id = proposal.id
        old_events = [
            (row.id, row.payload_json)
            for row in session.scalars(
                select(DecisionEvent).where(
                    DecisionEvent.project_id == ids[0],
                    DecisionEvent.event_type == "canon_obligation_before_image",
                )
            )
        ]
    model = BodyModel()
    outcome = RevisionValidationService(
        session_factory=fixture.Session,
        writer=ChapterWriter(model),
        policy=RuntimePolicy.for_profile("standard"),
    ).prepare(project_id=ids[0], candidate_id=candidate_id)
    assert outcome.blocked
    with fixture.Session() as session:
        result = session.get(CanonRevisionValidationRecord, outcome.blocked_path)
        assert result.status == "unknown", result.result_json
        assert "origin obligation" in result.result_json
        assert ledger(session) == before
        assert [
            (row.id, row.payload_json)
            for row in session.scalars(
                select(DecisionEvent).where(
                    DecisionEvent.project_id == ids[0],
                    DecisionEvent.event_type == "canon_obligation_before_image",
                )
            )
        ] == old_events
        assert [
            session.get(ChapterPlan, p).active_commit_id for p in ids[1:]
        ] == old_ids
    assert not model.calls
