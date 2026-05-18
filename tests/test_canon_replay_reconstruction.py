from __future__ import annotations

import pytest

from forwin.canon_quality.chapter_review_form.replay import (
    ChapterDraftNotFound,
    find_missing_accepted_chapters,
    reconstruct_writer_output,
)
from forwin.models import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.base import get_engine, get_session_factory, init_db
from tests.helpers.canon_replay import seed_accepted_chapter, seed_project_with_accepted_chapter
from tests.postgres import postgres_test_url


def test_reconstruct_writer_output_uses_accepted_draft_body() -> None:
    engine = get_engine(postgres_test_url("canon-replay-reconstruct"))
    init_db(engine)
    try:
        session_factory = get_session_factory(engine)
        with session_factory() as session:
            project, _arc, _plan, draft = seed_project_with_accepted_chapter(
                session,
                chapter_number=2,
                body="主倒计时还有59分钟。",
            )
            session.commit()

        with session_factory() as session:
            output = reconstruct_writer_output(session=session, project_id=project.id, chapter_number=2)

        assert output.project_id == project.id
        assert output.chapter_number == 2
        assert output.title == "第2章"
        assert output.body == draft.body_text
        assert output.char_count == len(draft.body_text)
        assert output.end_of_chapter_summary == draft.summary
        assert output.prompt_revision_hash == "replay"
        assert output.generation_meta["source"] == "canon_replay"
    finally:
        engine.dispose()


def test_reconstruct_writer_output_raises_when_no_committed_draft_exists() -> None:
    engine = get_engine(postgres_test_url("canon-replay-missing"))
    init_db(engine)
    try:
        session_factory = get_session_factory(engine)
        with session_factory() as session:
            project, _arc, _plan, _draft = seed_project_with_accepted_chapter(session, chapter_number=1)
            session.query(CandidateDraftRecord).delete()
            session.commit()

        with session_factory() as session:
            with pytest.raises(ChapterDraftNotFound) as exc:
                reconstruct_writer_output(session=session, project_id=project.id, chapter_number=1)

        assert "accepted draft not found" in str(exc.value)
    finally:
        engine.dispose()


def test_reconstruct_writer_output_uses_latest_committed_candidate_for_chapter() -> None:
    engine = get_engine(postgres_test_url("canon-replay-latest"))
    init_db(engine)
    try:
        session_factory = get_session_factory(engine)
        with session_factory() as session:
            project, _arc, plan, _draft = seed_project_with_accepted_chapter(session, chapter_number=1, body="旧正文")
            newer = ChapterDraft(
                id="draft-newer",
                chapter_plan_id=plan.id,
                version=2,
                body_text="新正文，主倒计时还有58分钟。",
                summary="新摘要",
                char_count=15,
                llm_raw_response="{}",
            )
            session.add(newer)
            session.flush()
            review = ChapterReview(id="review-newer", draft_id=newer.id, verdict="pass")
            session.add(review)
            session.flush()
            session.add(
                CandidateDraftRecord(
                    project_id=project.id,
                    chapter_plan_id=plan.id,
                    chapter_number=1,
                    candidate_draft_id=newer.id,
                    review_id=review.id,
                    status="canon_committed",
                    canon_status="canon",
                    version=2,
                )
            )
            session.commit()

        with session_factory() as session:
            output = reconstruct_writer_output(session=session, project_id=project.id, chapter_number=1)

        assert output.body == "新正文，主倒计时还有58分钟。"
        assert output.end_of_chapter_summary == "新摘要"
    finally:
        engine.dispose()


def test_find_missing_accepted_chapters_reports_range_holes() -> None:
    engine = get_engine(postgres_test_url("canon-replay-missing-range"))
    init_db(engine)
    try:
        session_factory = get_session_factory(engine)
        with session_factory() as session:
            project, arc, _plan, _draft = seed_project_with_accepted_chapter(session, chapter_number=1)
            seed_accepted_chapter(session, project=project, arc=arc, chapter_number=3)
            session.commit()

        with session_factory() as session:
            missing = find_missing_accepted_chapters(
                session=session,
                project_id=project.id,
                from_chapter=1,
                to_chapter=3,
            )

        assert missing == [2]
    finally:
        engine.dispose()
