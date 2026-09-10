from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from forwin.canon_quality.cache import persist_quality_projection
from forwin.canon_quality.gate import evaluate_canon_admission
from forwin.canon_quality.repository import CanonQualityRepository
from forwin.canon_quality.signals import (
    CanonQualitySignal,
    CharacterStateTransition,
    CountdownLedgerEntry,
)
from forwin.canon_quality.types import (
    CanonQualityAnalysisResult,
    QualityAnalysisCachePayload,
)
from forwin.models import (
    ArcPlanVersion,
    CandidateDraftRecord,
    ChapterDraft,
    ChapterPlan,
    ChapterReview,
    Project,
)
from forwin.models.base import Base
from forwin.models.canon import CanonCommitRecord
from forwin.models.canon_quality import CanonQualitySignalRow, CountdownLedgerRow


@pytest.fixture
def quality_session():
    # This is an isolated candidate replica, never a deployed data source.
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Project(id="book", title="Book", premise="Fixture"))
        session.add(
            ArcPlanVersion(
                id="arc",
                project_id="book",
                arc_number=1,
                chapter_start=1,
                chapter_end=3,
                arc_synopsis="Fixture",
            )
        )
        session.flush()
        for number in (1, 2, 3):
            session.add(
                ChapterPlan(
                    id=f"chapter-{number}",
                    project_id="book",
                    arc_plan_id="arc",
                    chapter_number=number,
                    title=f"Chapter {number}",
                )
            )
            session.flush()
            session.add(
                ChapterDraft(
                    id=f"draft-{number}",
                    chapter_plan_id=f"chapter-{number}",
                    body_text=f"Body {number}",
                )
            )
            session.flush()
            session.add(
                ChapterReview(
                    id=f"review-{number}", draft_id=f"draft-{number}", verdict="pass"
                )
            )
            session.flush()
            session.add(
                CandidateDraftRecord(
                    id=f"candidate-{number}",
                    project_id="book",
                    chapter_plan_id=f"chapter-{number}",
                    chapter_number=number,
                    candidate_draft_id=f"draft-{number}",
                    review_id=f"review-{number}",
                    body_hash=hashlib.sha256(f"Body {number}".encode()).hexdigest(),
                    status="accepted",
                    canon_status="canon",
                )
            )
        session.flush()
        yield session
    engine.dispose()


def _commit(session, number=1, revision=1):
    row = CanonCommitRecord(
        id=f"accept-{number}-{revision}",
        idempotency_key=f"key-{number}-{revision}",
        project_id="book",
        chapter_plan_id=f"chapter-{number}",
        chapter_number=number,
        candidate_id=f"candidate-{number}",
        acceptance_revision=revision,
    )
    session.add(row)
    session.flush()
    session.get(ChapterPlan, f"chapter-{number}").active_commit_id = row.id
    session.flush()
    return row


def _projection(number=1, minutes=10, life="alive", description="Accepted warning"):
    signal = CanonQualitySignal(
        signal_id=f"signal-{number}",
        project_id="book",
        chapter_number=number,
        signal_type="fixture",
        severity="warning",
        target_scope="chapter",
        subject_key="person",
        description=description,
        payload={"draft_id": f"draft-{number}"},
    )
    return QualityAnalysisCachePayload(
        analysis=CanonQualityAnalysisResult(
            project_id="book",
            chapter_number=number,
            draft_id=f"draft-{number}",
            signals=[signal],
        ),
        character_transitions=[
            CharacterStateTransition(
                project_id="book",
                character_name="Lin",
                chapter_number=number,
                transition_type="life_state",
                to_state=life,
                payload={"draft_id": f"draft-{number}"},
            )
        ],
        countdown_entries=[
            CountdownLedgerEntry(
                project_id="book",
                chapter_number=number,
                countdown_key="clock",
                normalized_remaining_minutes=minutes,
                payload={"draft_id": f"draft-{number}"},
            )
        ],
    )


def _run(session, payload):
    repo = CanonQualityRepository(session)
    persist_quality_projection(
        repo,
        project_id="book",
        chapter_number=payload.analysis.chapter_number,
        payload=payload,
    )
    gate = evaluate_canon_admission(
        project_id="book",
        chapter_number=payload.analysis.chapter_number,
        draft_id=payload.analysis.draft_id,
        review_id=f"review-{payload.analysis.chapter_number}",
        review_verdict="pass",
        signals=payload.analysis.signals,
    )
    return repo.save_admission_run(gate, signals=payload.analysis.signals)


def _bind(session, commit, run):
    return CanonQualityRepository(session).bind_acceptance(
        project_id="book",
        chapter_number=commit.chapter_number,
        draft_id=f"draft-{commit.chapter_number}",
        acceptance_id=commit.id,
        quality_admission_run_id=run.id,
    )


def test_acceptance_uses_the_exact_run_not_latest_rows_for_the_same_draft(
    quality_session,
):
    session = quality_session
    first = _run(session, _projection(minutes=10))
    second = _run(session, _projection(minutes=3, description="Later draft analysis"))
    commit = _commit(session)
    _bind(session, commit, first)
    repo = CanonQualityRepository(session)
    assert (
        repo.list_countdown_entries("book", before_chapter=2)[0][
            "normalized_remaining_minutes"
        ]
        == 10
    )
    assert (
        repo.list_open_signals("book", before_chapter=2)[0].description
        == "Accepted warning"
    )
    assert (
        session.scalar(
            select(CountdownLedgerRow).where(
                CountdownLedgerRow.normalized_remaining_minutes == 3
            )
        )
        is not None
    )
    assert first.id != second.id


def test_same_draft_two_acceptances_have_distinct_immutable_quality_evidence(
    quality_session,
):
    session = quality_session
    first = _run(session, _projection(minutes=10, life="alive"))
    old = _commit(session)
    _bind(session, old, first)
    second = _run(session, _projection(minutes=3, life="dead"))
    new = _commit(session, revision=2)
    _bind(session, new, second)
    # Candidate status and updated_at can no longer choose which review is active.
    session.get(CandidateDraftRecord, "candidate-1").status = "failed"
    repo = CanonQualityRepository(session)
    assert (
        repo.list_character_transitions("book", before_chapter=2)[0]["to_state"]
        == "dead"
    )
    assert (
        repo.list_countdown_entries("book", before_chapter=2)[0][
            "normalized_remaining_minutes"
        ]
        == 3
    )
    session.get(ChapterPlan, "chapter-1").active_commit_id = old.id
    assert (
        repo.list_character_transitions("book", before_chapter=2)[0]["to_state"]
        == "alive"
    )
    assert (
        repo.list_countdown_entries("book", before_chapter=2)[0][
            "normalized_remaining_minutes"
        ]
        == 10
    )


def test_later_mutable_signal_upsert_does_not_rewrite_accepted_signal_evidence(
    quality_session,
):
    session = quality_session
    run = _run(session, _projection())
    commit = _commit(session)
    _bind(session, commit, run)
    repo = CanonQualityRepository(session)
    repo.supersede_chapter_signals("book", 1)
    repo.save_signals(_projection(description="Unaccepted rewrite").analysis.signals)
    assert (
        repo.list_open_signals("book", before_chapter=2)[0].description
        == "Accepted warning"
    )
    assert (
        session.scalar(select(CanonQualitySignalRow)).description
        == "Unaccepted rewrite"
    )


def test_rebinding_existing_acceptance_to_different_run_refuses_without_overwrite(
    quality_session,
):
    session = quality_session
    first = _run(session, _projection())
    commit = _commit(session)
    _bind(session, commit, first)
    second = _run(session, _projection(minutes=2))
    with pytest.raises(ValueError, match="immutable|different"):
        _bind(session, commit, second)
    assert (
        CanonQualityRepository(session).list_countdown_entries("book")[0][
            "normalized_remaining_minutes"
        ]
        == 10
    )


def test_unproven_legacy_acceptance_stays_unknown_and_strict_prefix_refuses(
    quality_session,
):
    session = quality_session
    _run(session, _projection())
    commit = _commit(session)
    repo = CanonQualityRepository(session)
    assert (
        repo.bind_acceptance(
            project_id="book",
            chapter_number=1,
            draft_id="draft-1",
            acceptance_id=commit.id,
        )
        is None
    )
    with pytest.raises(ValueError, match="provenance|evidence"):
        repo.restore_prefix(
            project_id="book", active_commit_ids={1: commit.id}, from_chapter=2
        )


def test_explicit_fresh_replica_projection_survives_no_active_chapter_pointer(
    quality_session,
):
    from forwin.canon_quality.chapter_review_form.service import ChapterReviewFormResult

    session = quality_session
    payload = _projection(minutes=7)
    form = ChapterReviewFormResult(
        project_id="book",
        chapter_number=1,
        draft_id="draft-1",
        character_transitions=payload.character_transitions,
        countdown_entries=payload.countdown_entries,
        signals=payload.analysis.signals,
    )
    repo = CanonQualityRepository(session)
    repo.activate_candidate_projection(
        project_id="book",
        chapter_number=1,
        draft_id="draft-1",
        body_sha256=hashlib.sha256(b"Body 1").hexdigest(),
        historical_form=form,
        projection_id="validation-1",
    )
    assert session.get(ChapterPlan, "chapter-1").active_commit_id is None
    assert (
        repo.list_countdown_entries("book", before_chapter=2)[0][
            "normalized_remaining_minutes"
        ]
        == 7
    )


def test_snapshot_tamper_or_wrong_draft_cannot_be_bound(quality_session):
    session = quality_session
    run = _run(session, _projection())
    commit = _commit(session)
    run.projection_json = json.dumps({"forged": True})
    with pytest.raises(ValueError, match="fingerprint|snapshot"):
        _bind(session, commit, run)


def test_unrelated_saved_rows_are_not_in_the_next_complete_snapshot(quality_session):
    session = quality_session
    repo = CanonQualityRepository(session)
    repo.save_countdown_entries(
        [
            CountdownLedgerEntry(
                project_id="book",
                chapter_number=1,
                countdown_key="stray",
                normalized_remaining_minutes=999,
                payload={"draft_id": "draft-1"},
            )
        ]
    )
    run = _run(session, _projection(minutes=10))
    _bind(session, _commit(session), run)
    assert [row["countdown_key"] for row in repo.list_countdown_entries("book")] == [
        "clock"
    ]


@pytest.mark.parametrize(
    "field", ["quality_admission_run_id", "revision_validation_id"]
)
def test_frozen_quality_and_revision_evidence_identity_change_canon_idempotency(field):
    from forwin.canon.plan import CanonCommitPlan
    from forwin.naming import EntityAdmissionPlan
    from forwin.protocol.book_state import ApprovedGraphDeltaSet

    arguments = {
        "project_id": "book",
        "chapter_number": 1,
        "candidate_id": "candidate",
        "candidate_body_hash": "a" * 64,
        "plan_revision": "plan",
        "policy_version": 1,
        "expected_previous_accepted_chapter": 0,
        "expected_book_state_chapter": 0,
        "approved_book_state_changes": ApprovedGraphDeltaSet(
            project_id="book", chapter_number=1
        ),
        "entity_admission_plan": EntityAdmissionPlan(
            project_id="book", chapter_number=1, candidate_fingerprint="entity"
        ),
        "chapter_title": "Title",
    }
    first = CanonCommitPlan.build(**arguments, **{field: "evidence-1"})
    second = CanonCommitPlan.build(**arguments, **{field: "evidence-2"})
    assert getattr(first, field) == "evidence-1"
    assert first.idempotency_key != second.idempotency_key
    assert first.canon_commit_id != second.canon_commit_id


def test_strict_candidate_prefix_never_reads_unaccepted_legacy_projection_rows(
    quality_session,
):
    session = quality_session
    _run(session, _projection())
    repo = CanonQualityRepository(session)
    # No accepted chapter identity exists; saved analysis cannot fill that gap.
    repo.restore_prefix(project_id="book", active_commit_ids={}, from_chapter=1)
    assert repo.list_character_transitions("book", before_chapter=2) == []
    assert repo.list_countdown_entries("book", before_chapter=2) == []
    assert repo.list_open_signals("book", before_chapter=2) == []


def test_candidate_quality_form_cannot_borrow_body_from_another_chapter(
    quality_session,
):
    from forwin.canon_quality.chapter_review_form.service import ChapterReviewFormResult

    repo = CanonQualityRepository(quality_session)
    form = ChapterReviewFormResult(
        project_id="book", chapter_number=2, draft_id="draft-1"
    )
    with pytest.raises(ValueError, match="ownership|draft"):
        repo.activate_candidate_projection(
            project_id="book",
            chapter_number=2,
            draft_id="draft-1",
            body_sha256=hashlib.sha256(b"Body 1").hexdigest(),
            historical_form=form,
            projection_id="bad-owner",
        )


def test_rolled_back_projection_cannot_be_claimed_by_a_later_admission_run(
    quality_session,
):
    session = quality_session
    session.commit()
    payload = _projection()
    repo = CanonQualityRepository(session)
    persist_quality_projection(
        repo, project_id="book", chapter_number=1, payload=payload
    )
    session.rollback()
    gate = evaluate_canon_admission(
        project_id="book",
        chapter_number=1,
        draft_id="draft-1",
        review_verdict="pass",
        signals=payload.analysis.signals,
    )
    run = repo.save_admission_run(gate, signals=payload.analysis.signals)
    assert run.projection_json == ""
    assert run.projection_fingerprint == ""


def test_historical_acceptance_requires_real_in_bounds_body_evidence(quality_session):
    from forwin.canon_quality.chapter_review_form.service import ChapterReviewFormResult

    session = quality_session
    commit = _commit(session)
    body_hash = hashlib.sha256(b"Body 1").hexdigest()
    review = ChapterReviewFormResult(
        project_id="book", chapter_number=1, draft_id="draft-1"
    )
    form = {
        "review": review.model_dump(mode="json"),
        "checks": [
            {
                "dimension": dimension,
                "status": "pass",
                "evidence_refs": [f"body:{body_hash}#999:1000"],
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
    }
    with pytest.raises(ValueError, match="grounded|evidence|body"):
        CanonQualityRepository(session).bind_acceptance(
            project_id="book",
            chapter_number=1,
            draft_id="draft-1",
            acceptance_id=commit.id,
            historical_form=form,
        )


def test_legacy_projection_reads_active_commit_draft_without_fabricating_snapshot(quality_session):
    session = quality_session
    _commit(session)
    repo = CanonQualityRepository(session)
    repo.save_countdown_entries(_projection(minutes=10).countdown_entries)
    session.add(ChapterDraft(id="inactive-draft", chapter_plan_id="chapter-1", version=2, body_text="Other draft"))
    session.flush()
    session.add(CandidateDraftRecord(
        id="inactive-candidate", project_id="book", chapter_plan_id="chapter-1",
        chapter_number=1, version=2, candidate_draft_id="inactive-draft",
        status="canon_committed", canon_status="canon",
        body_hash=hashlib.sha256(b"Other draft").hexdigest(),
    ))
    repo.save_countdown_entries([CountdownLedgerEntry(
        project_id="book", chapter_number=1, countdown_key="clock",
        normalized_remaining_minutes=999, payload={"draft_id": "inactive-draft"},
    )])
    session.flush()
    assert repo.list_countdown_entries("book", before_chapter=2) == [{
        "countdown_key": "clock", "chapter_number": 1,
        "normalized_remaining_minutes": 10, "status": "consistent",
    }]
    from forwin.canon_quality.acceptance import QualityProvenanceUnknown
    from forwin.models.canon_quality import CanonQualityAcceptanceEvidenceRow
    assert session.get(CanonQualityAcceptanceEvidenceRow, "accept-1-1") is None
    with pytest.raises(QualityProvenanceUnknown, match="missing"):
        repo.restore_prefix(project_id="book", active_commit_ids={1: "accept-1-1"}, from_chapter=2)
