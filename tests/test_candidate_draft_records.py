from __future__ import annotations

import json

from forwin.api_project_ops import get_candidate_draft
from forwin.candidate_drafts import CandidateDraftRepository
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.draft import CandidateDraftRecord
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.state.updater import StateUpdater


def _setup_reviewed_draft(session):
    updater = StateUpdater(session)
    project = updater.create_project(title="候选正文", premise="前提", genre="玄幻")
    arc = updater.create_arc_plan(project.id, "主线弧")
    chapter = updater.create_chapter_plan(
        project_id=project.id,
        arc_plan_id=arc.id,
        chapter_number=1,
        title="第一章",
        one_line="开场",
        goals=["推进主线"],
    )
    writer_output = WriterOutput(
        project_id=project.id,
        chapter_number=1,
        title="第一章",
        body="正文",
        char_count=2,
        end_of_chapter_summary="摘要",
    )
    draft = ChapterDraft(
        chapter_plan_id=chapter.id,
        version=1,
        body_text=writer_output.body,
        char_count=writer_output.char_count,
        summary=writer_output.end_of_chapter_summary,
    )
    session.add(draft)
    session.flush()
    review = ChapterReview(
        draft_id=draft.id,
        verdict="warn",
        issues_json="[]",
        review_meta_json=json.dumps(ReviewVerdict(verdict="warn", review_summary="需修").model_dump(mode="json"), ensure_ascii=False),
    )
    session.add(review)
    session.flush()
    return project, chapter, draft, review, writer_output


def test_candidate_draft_record_tracks_review_and_canon_lifecycle() -> None:
    engine = get_engine(":memory:")
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project, chapter, draft, review, writer_output = _setup_reviewed_draft(session)
        repository = CandidateDraftRepository(session)

        record = repository.upsert_from_review(
            project_id=project.id,
            chapter_plan=chapter,
            draft=draft,
            review=review,
            writer_output=writer_output,
            repair_attempt_count=0,
        )

        assert record.project_id == project.id
        assert record.chapter_number == 1
        assert record.candidate_draft_id == draft.id
        assert record.review_id == review.id
        assert record.status == "reviewed"
        assert record.canon_status == "candidate"
        assert json.loads(record.scene_outputs_json) == []

        committed = repository.mark_canon_committed(
            project_id=project.id,
            chapter_number=1,
            canon_artifact_path="artifacts/canon/1.json",
        )

        assert committed is not None
        assert committed.status == "canon_committed"
        assert committed.canon_status == "canon"
        assert committed.canon_artifact_path == "artifacts/canon/1.json"


def test_candidate_draft_api_reads_record_and_preserves_legacy_fallback() -> None:
    engine = get_engine(":memory:")
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project, chapter, draft, review, writer_output = _setup_reviewed_draft(session)
        CandidateDraftRepository(session).upsert_from_review(
            project_id=project.id,
            chapter_plan=chapter,
            draft=draft,
            review=review,
            writer_output=writer_output,
            repair_attempt_count=2,
        )
        project_id = project.id
        draft_id = draft.id

    detail = get_candidate_draft(
        project_id,
        1,
        get_session=Session,
        decision_refs_for_chapter_review=lambda *_args, **_kwargs: [],
    )

    assert detail.candidate_draft_id == draft_id
    assert detail.status == "reviewed"
    assert detail.canon_status == "candidate"
    assert detail.repair_attempt_count == 2
    assert detail.scene_outputs == []

    with Session.begin() as session:
        session.query(CandidateDraftRecord).delete()

    fallback = get_candidate_draft(
        project_id,
        1,
        get_session=Session,
        decision_refs_for_chapter_review=lambda *_args, **_kwargs: [],
    )

    assert fallback.candidate_draft_id == draft_id
    assert fallback.canon_status == "candidate"
