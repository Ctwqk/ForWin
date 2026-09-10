"""Formal history follows accepted identity while candidate revisions remain private."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from forwin.canon.admission import CanonAdmissionService
from forwin.canon.revision_service import (
    RevisionValidationService,
    revision_model_identity,
    save_revision_proposal,
)
from forwin.context.providers.state_provider import StateContextProvider
from forwin.context.request import ContextDraft, ContextRequest
from forwin.genesis.arc_activation_review import build_arc_activation_review_pack
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.project import ChapterPlan
from forwin.review.query import ReviewQuery
from forwin.runtime.policy import RuntimePolicy
from forwin.state.query_helpers import load_latest_drafts_by_plan_id
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.writer.chapter_writer import ChapterWriter
from tests import test_canon_atomic_transaction as atomic
from tests import test_revision_full_suffix as revision
from tests.postgres import postgres_test_url

prepared_canon = atomic.prepared_canon


@pytest.fixture
def accepted_history():
    engine = get_engine(postgres_test_url("accepted-review-history"))
    init_db(engine)
    factory = get_session_factory(engine)
    with factory.begin() as session:
        updater = StateUpdater(session)
        project = updater.create_project(
            title="Formal history",
            premise="Accepted text is authoritative.",
            genre="thriller",
            runtime_policy=RuntimePolicy.for_profile("standard"),
            automation_json=json.dumps({"primary_publish_platform": "qidian"}),
        )
        arc = updater.create_arc_plan(project.id, "The archive")
        project_id, arc_id = project.id, arc.id
    ids = []
    owner = CanonAdmissionService(session_factory=factory)
    for number in range(1, 4):
        with factory.begin() as session:
            chapter = StateUpdater(session).create_chapter_plan(
                project_id=project_id,
                arc_plan_id=arc_id,
                chapter_number=number,
                title=f"Chapter {number}",
                one_line=f"Unrealized plan {number}",
                goals=[],
            )
            ids.append(chapter.id)
            plan = atomic._prepare_chapter_candidate(
                session,
                project_id=project_id,
                chapter=chapter,
                version=1,
                body=f"Archive {number} remains quiet.",
                summary=f"Accepted summary {number}",
                delta_id=f"archive-{number}",
                delta_summary=f"Archive {number}",
                node_id=f"archive-event-{number}",
            )
        outcome = owner.commit_plan(plan)
        assert not outcome.blocked, outcome
        with factory.begin() as session:
            atomic._complete_post_canon_barrier(
                session,
                commit_id=outcome.commit_id,
                project_id=project_id,
                chapter_number=number,
                candidate_id=plan.candidate_id,
            )
    try:
        yield SimpleNamespace(Session=factory, project_id=project_id, chapter_ids=ids)
    finally:
        engine.dispose()


def _pending_revision(book, *, number=3, suffix="new", summary="Pending summary"):
    with book.Session.begin() as session:
        candidate = save_revision_proposal(
            session,
            project_id=book.project_id,
            chapter_number=number,
            body=f"Unaccepted replacement {suffix}.",
            expected_book_revision=3,
        )
        session.get(ChapterDraft, candidate.candidate_draft_id).summary = summary
        review = session.get(ChapterReview, candidate.review_id)
        review.created_at = datetime(2100, 1, 1, tzinfo=UTC)
        review.review_meta_json = json.dumps(
            {"review_summary": f"Pending note {suffix}"}
        )
        return candidate.id


def test_pending_drafts_cannot_displace_other_accepted_chapters_in_summary_window(
    accepted_history,
):
    book = accepted_history
    for suffix in ("one", "two", "three"):
        _pending_revision(book, suffix=suffix, summary=f"Pending summary {suffix}")
    with book.Session() as session:
        assert ReviewQuery(session).chapter_summaries(
            book.project_id, before_chapter=4, limit=3
        ) == [
            "Accepted summary 1",
            "Accepted summary 2",
            "Accepted summary 3",
        ]


@pytest.mark.parametrize("same_draft", [False, True])
def test_recent_notes_use_the_review_bound_to_active_canon(
    accepted_history, same_draft
):
    book = accepted_history
    if same_draft:
        with book.Session.begin() as session:
            plan = session.get(ChapterPlan, book.chapter_ids[2])
            canon = session.get(CanonCommitRecord, plan.active_commit_id)
            candidate = session.get(CandidateDraftRecord, canon.candidate_id)
            session.add(
                ChapterReview(
                    draft_id=candidate.candidate_draft_id,
                    verdict="unknown",
                    review_meta_json='{"review_summary":"Unaccepted re-review"}',
                    created_at=datetime(2100, 1, 1, tzinfo=UTC),
                )
            )
    else:
        _pending_revision(book)
    with book.Session() as session:
        notes = ReviewQuery(session).recent_notes(
            book.project_id, before_chapter=4, limit=2
        )
        assert [
            (note.chapter_number, note.verdict, note.summary) for note in notes
        ] == [
            (3, "pass", "Accepted summary 3"),
            (2, "pass", "Accepted summary 2"),
        ]


def test_arc_history_reads_active_summary_instead_of_pending_draft_or_plan(
    accepted_history,
):
    book = accepted_history
    _pending_revision(book, summary="")
    with book.Session() as session:
        pack = build_arc_activation_review_pack(
            session,
            project_id=book.project_id,
            arc_number=2,
            chapter_start=4,
            summary_limit=2,
        )
        assert pack.accepted_chapter_summaries == [
            {
                "chapter_number": 2,
                "title": "Chapter 2",
                "summary": "Accepted summary 2",
            },
            {
                "chapter_number": 3,
                "title": "Chapter 3",
                "summary": "Accepted summary 3",
            },
        ]


def test_unknown_revision_validation_does_not_change_formal_history(accepted_history):
    book = accepted_history
    candidate_id = _pending_revision(book)
    result = RevisionValidationService(
        session_factory=book.Session,
        writer=SimpleNamespace(llm_client=None),
        policy=RuntimePolicy.for_profile("standard"),
    ).prepare(project_id=book.project_id, candidate_id=candidate_id)
    assert result.block_kind == "revision_unknown"
    with book.Session() as session:
        query = ReviewQuery(session)
        assert query.chapter_summaries(book.project_id, before_chapter=4, limit=1) == [
            "Accepted summary 3"
        ]
        assert (
            query.recent_notes(book.project_id, before_chapter=4, limit=1)[0].verdict
            == "pass"
        )


def test_accepted_status_without_canon_identity_cannot_supply_formal_history(
    accepted_history,
):
    book = accepted_history
    with book.Session.begin() as session:
        session.get(ChapterPlan, book.chapter_ids[2]).active_commit_id = None
    with book.Session() as session:
        query = ReviewQuery(session)
        assert query.chapter_summaries(book.project_id, before_chapter=4) == [
            "Accepted summary 1",
            "Accepted summary 2",
        ]
        assert [
            note.chapter_number
            for note in query.recent_notes(book.project_id, before_chapter=4)
        ] == [2, 1]
        pack = build_arc_activation_review_pack(
            session, project_id=book.project_id, arc_number=2, chapter_start=4
        )
        assert [row["chapter_number"] for row in pack.accepted_chapter_summaries] == [
            1,
            2,
        ]


def test_zero_note_window_and_band_bounds_are_respected(accepted_history):
    book = accepted_history
    with book.Session() as session:
        query = ReviewQuery(session)
        assert query.recent_notes(book.project_id, before_chapter=4, limit=0) == []
        notes = query.recent_notes(
            book.project_id, before_chapter=4, band_start=2, band_end=2
        )
        assert [note.chapter_number for note in notes] == [2]


def test_shared_draft_reader_does_not_fabricate_missing_accepted_identity(
    accepted_history,
):
    book = accepted_history
    _pending_revision(book)
    with book.Session.begin() as session:
        chapter = session.get(ChapterPlan, book.chapter_ids[2])
        chapter.active_commit_id = None
    with book.Session() as session:
        assert book.chapter_ids[2] not in load_latest_drafts_by_plan_id(
            session, book.chapter_ids
        )


def test_shared_draft_reader_preserves_latest_candidate_for_unaccepted_chapter(
    accepted_history,
):
    book = accepted_history
    with book.Session.begin() as session:
        chapter = ChapterPlan(
            project_id=book.project_id,
            arc_plan_id=session.get(ChapterPlan, book.chapter_ids[2]).arc_plan_id,
            chapter_number=4,
            status="draft",
        )
        session.add(chapter)
        session.flush()
        session.add(
            ChapterDraft(chapter_plan_id=chapter.id, version=1, body_text="First draft")
        )
        newest = ChapterDraft(
            chapter_plan_id=chapter.id, version=2, body_text="Repaired draft"
        )
        session.add(newest)
        session.flush()
        chapter_id, draft_id = chapter.id, newest.id
    with book.Session() as session:
        assert (
            load_latest_drafts_by_plan_id(session, [chapter_id])[chapter_id].id
            == draft_id
        )


def test_state_context_uses_full_accepted_chapter_window(accepted_history):
    book = accepted_history
    for suffix in ("one", "two", "three"):
        _pending_revision(book, suffix=suffix)
    with book.Session() as session:
        request = ContextRequest(
            project_id=book.project_id,
            chapter_plan=SimpleNamespace(chapter_number=4),
            repo=StateRepository(session),
            session=session,
        )
        draft = ContextDraft()
        StateContextProvider().contribute(request, draft)
        assert draft.data["summaries"] == [
            "Accepted summary 1",
            "Accepted summary 2",
            "Accepted summary 3",
        ]


@pytest.mark.parametrize("verdict", ["pass", "fail"])
def test_formal_reads_switch_together_only_after_real_revision_acceptance(
    prepared_canon, verdict
):
    fixture = prepared_canon
    ids, body, old_ids, _ = revision._book(fixture)

    def assert_history(summaries):
        with fixture.Session() as session:
            query = ReviewQuery(session)
            assert query.chapter_summaries(ids[0], before_chapter=3) == summaries
            assert [
                (note.verdict, note.summary)
                for note in query.recent_notes(ids[0], before_chapter=3)
            ] == [("pass", text) for text in reversed(summaries)]
            pack = build_arc_activation_review_pack(
                session,
                project_id=ids[0],
                arc_number=2,
                chapter_start=3,
            )
            assert [
                row["summary"] for row in pack.accepted_chapter_summaries
            ] == summaries

    assert_history(["Archive", "Shelves"])
    with fixture.Session.begin() as session:
        candidate = save_revision_proposal(
            session,
            project_id=ids[0],
            chapter_number=1,
            body=body.replace("安静地", "静静地"),
            expected_book_revision=2,
        )
        session.get(
            ChapterDraft, candidate.candidate_draft_id
        ).summary = "Replacement archive"
        candidate_id = candidate.id
    assert_history(["Archive", "Shelves"])

    class Model(revision.BodyModel):
        def chat(self, messages, **kwargs):
            result = json.loads(super().chat(messages, **kwargs))
            if verdict == "fail" and "coverage" in result:
                result["coverage"][0]["status"] = "fail"
                result["coverage"][0]["explanation"] = (
                    "The replacement conflicts with the prefix."
                )
            return json.dumps(result)

    writer = ChapterWriter(Model())
    prepared = RevisionValidationService(
        session_factory=fixture.Session,
        writer=writer,
        policy=RuntimePolicy.for_profile("standard"),
    ).prepare(project_id=ids[0], candidate_id=candidate_id)
    assert_history(["Archive", "Shelves"])
    if verdict == "pass":
        assert not prepared.blocked, prepared
        outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
            prepared.plan,
            revision_model_identity=revision_model_identity(writer),
        )
        assert not outcome.blocked, outcome
        assert_history(["Replacement archive", "Shelves"])
    else:
        assert prepared.block_kind == "revision_fail"
    with fixture.Session() as session:
        assert all(
            session.get(CanonCommitRecord, commit_id) is not None
            for commit_id in old_ids
        )


def test_empty_accepted_summary_is_not_replaced_with_unrealized_plan(accepted_history):
    book = accepted_history
    with book.Session.begin() as session:
        draft = load_latest_drafts_by_plan_id(session, [book.chapter_ids[2]])[
            book.chapter_ids[2]
        ]
        draft.summary = ""
    with book.Session() as session:
        pack = build_arc_activation_review_pack(
            session,
            project_id=book.project_id,
            arc_number=2,
            chapter_start=4,
            summary_limit=1,
        )
        assert pack.accepted_chapter_summaries[0]["summary"] == ""
