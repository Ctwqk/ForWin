"""The manual CQ reader consumes the same accepted identity as normal readers."""

import hashlib

import pytest

from forwin.canon.admission import CanonAdmissionService
from forwin.canon_quality.chapter_review_form.replay import (
    ChapterDraftNotFound,
    latest_accepted_chapter,
    load_accepted_draft_ref,
)
from forwin.models import CandidateDraftRecord, ChapterDraft, ChapterPlan, ChapterReview
from forwin.models.canon import CanonCommitRecord
from tests import test_canon_atomic_transaction as atomic_tests

prepared_canon = atomic_tests.prepared_canon


def _accept(fixture):
    outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        fixture.plan
    )
    assert outcome.commit_id
    return outcome.commit_id


def _legacy_candidate(session, chapter, *, version=2):
    draft = ChapterDraft(
        chapter_plan_id=chapter.id,
        version=version,
        body_text="仅有旧状态标签的候选正文。",
        summary="不属于正式版本",
        char_count=14,
    )
    session.add(draft)
    session.flush()
    review = ChapterReview(draft_id=draft.id, verdict="pass")
    session.add(review)
    session.flush()
    session.add(
        CandidateDraftRecord(
            project_id=chapter.project_id,
            chapter_plan_id=chapter.id,
            chapter_number=chapter.chapter_number,
            candidate_draft_id=draft.id,
            review_id=review.id,
            version=version,
            status="canon_committed",
            canon_status="canon",
            body_hash=hashlib.sha256(draft.body_text.encode()).hexdigest(),
        )
    )
    session.flush()


def test_replay_reads_a_real_current_canon_acceptance(prepared_canon):
    fixture = prepared_canon
    commit_id = _accept(fixture)
    with fixture.Session() as session:
        commit = session.get(CanonCommitRecord, commit_id)
        candidate = session.get(CandidateDraftRecord, commit.candidate_id)
        assert candidate.status == "accepted"
        ref = load_accepted_draft_ref(
            session=session, project_id=fixture.project_id, chapter_number=1
        )
        assert ref.draft_id == candidate.candidate_draft_id
        assert hashlib.sha256(ref.body.encode()).hexdigest() == candidate.body_hash
        assert ref.title == commit.chapter_title
        assert (
            latest_accepted_chapter(session=session, project_id=fixture.project_id) == 1
        )


def test_replay_ignores_newer_legacy_candidate_status(prepared_canon):
    fixture = prepared_canon
    commit_id = _accept(fixture)
    with fixture.Session.begin() as session:
        _legacy_candidate(session, session.get(ChapterPlan, fixture.chapter_plan_id))
    with fixture.Session() as session:
        candidate = session.get(
            CandidateDraftRecord, session.get(CanonCommitRecord, commit_id).candidate_id
        )
        ref = load_accepted_draft_ref(
            session=session, project_id=fixture.project_id, chapter_number=1
        )
        assert ref.draft_id == candidate.candidate_draft_id


def test_replay_range_tail_does_not_count_an_unowned_accepted_label(prepared_canon):
    fixture = prepared_canon
    _accept(fixture)
    with fixture.Session.begin() as session:
        accepted = session.get(ChapterPlan, fixture.chapter_plan_id)
        orphan = ChapterPlan(
            project_id=fixture.project_id,
            arc_plan_id=accepted.arc_plan_id,
            chapter_number=2,
            title="仅有标签的第二章",
            status="accepted",
        )
        session.add(orphan)
        session.flush()
        _legacy_candidate(session, orphan, version=1)
    with fixture.Session() as session:
        assert (
            latest_accepted_chapter(session=session, project_id=fixture.project_id) == 1
        )
        with pytest.raises(ChapterDraftNotFound, match="accepted draft not found"):
            load_accepted_draft_ref(
                session=session, project_id=fixture.project_id, chapter_number=2
            )


def test_replay_missing_active_identity_never_falls_back_to_legacy_candidate(
    prepared_canon,
):
    fixture = prepared_canon
    _accept(fixture)
    with fixture.Session.begin() as session:
        chapter = session.get(ChapterPlan, fixture.chapter_plan_id)
        chapter.active_commit_id = None
        _legacy_candidate(session, chapter)
    with fixture.Session() as session:
        with pytest.raises(ChapterDraftNotFound):
            load_accepted_draft_ref(
                session=session, project_id=fixture.project_id, chapter_number=1
            )
        with pytest.raises(ChapterDraftNotFound):
            latest_accepted_chapter(session=session, project_id=fixture.project_id)
