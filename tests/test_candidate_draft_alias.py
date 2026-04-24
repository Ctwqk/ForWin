from __future__ import annotations

import json

from fastapi import HTTPException

from forwin.api_project_ops import get_candidate_draft
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.state.updater import StateUpdater


def test_candidate_draft_alias_returns_latest_draft_review_and_canon_status() -> None:
    engine = get_engine(":memory:")
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
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
        chapter.status = "needs_review"
        draft = ChapterDraft(
            chapter_plan_id=chapter.id,
            version=1,
            body_text="正文",
            char_count=2,
            summary="摘要",
        )
        session.add(draft)
        session.flush()
        review = ChapterReview(
            draft_id=draft.id,
            verdict="warn",
            issues_json=json.dumps([], ensure_ascii=False),
            review_meta_json=json.dumps({"recommended_action": "repair"}, ensure_ascii=False),
        )
        session.add(review)
        project_id = project.id
        draft_id = draft.id

    detail = get_candidate_draft(
        project_id,
        1,
        get_session=Session,
        decision_refs_for_chapter_review=lambda *_args, **_kwargs: [],
    )

    assert detail.project_id == project_id
    assert detail.chapter_number == 1
    assert detail.candidate_draft_id == draft_id
    assert detail.review_verdict == "warn"
    assert detail.canon_ready is False
    assert detail.canon_status == "candidate"
    assert detail.body == "正文"


def test_candidate_draft_alias_404s_when_no_candidate_exists() -> None:
    engine = get_engine(":memory:")
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        updater = StateUpdater(session)
        project = updater.create_project(title="候选正文", premise="前提", genre="玄幻")
        arc = updater.create_arc_plan(project.id, "主线弧")
        updater.create_chapter_plan(
            project_id=project.id,
            arc_plan_id=arc.id,
            chapter_number=1,
            title="第一章",
            one_line="开场",
            goals=["推进主线"],
        )
        project_id = project.id

    try:
        get_candidate_draft(
            project_id,
            1,
            get_session=Session,
            decision_refs_for_chapter_review=lambda *_args, **_kwargs: [],
        )
    except HTTPException as exc:
        assert exc.status_code == 404
        assert "candidate draft" in str(exc.detail).lower()
    else:
        raise AssertionError("expected candidate draft lookup to fail")
