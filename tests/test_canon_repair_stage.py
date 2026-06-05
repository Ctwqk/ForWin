from __future__ import annotations

import json

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from forwin.models.base import Base
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.phase import ChapterRewriteAttempt
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.project_ops.reviews import get_chapter_review


def _session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _noop_decision_refs(*args, **kwargs):
    return []


def test_rewrite_attempt_phase_fields_are_serialized_in_review_detail():
    Session = _session_factory()
    session = Session()
    try:
        project = Project(id="p", title="测试项目", genre="玄幻", premise="premise")
        arc = ArcPlanVersion(id="arc1", project_id="p", arc_synopsis="arc")
        plan = ChapterPlan(
            id="cp1",
            project_id="p",
            arc_plan_id="arc1",
            chapter_number=1,
            title="第一章",
            one_line="开场",
            goals_json="[]",
            status="needs_review",
            repair_attempt_count=2,
        )
        draft = ChapterDraft(
            id="d1",
            chapter_plan_id="cp1",
            version=1,
            body_text="正文" * 100,
            summary="summary",
            char_count=200,
        )
        review = ChapterReview(
            id="r1",
            draft_id="d1",
            verdict="warn",
            issues_json="[]",
            review_meta_json=json.dumps({"review_summary": "warn"}, ensure_ascii=False),
        )
        attempt = ChapterRewriteAttempt(
            id="a1",
            project_id="p",
            chapter_number=1,
            attempt_no=2,
            repair_phase="canon_repair",
            phase_attempt_no=1,
            trigger_review_id="r1",
            repair_scope="draft",
            design_patch_json="{}",
            source_draft_id="d1",
            result_draft_id="d1",
            result_verdict="warn",
            result_review_id="r1",
        )
        session.add_all([project, arc, plan, draft, review, attempt])
        session.commit()
    finally:
        session.close()

    detail = get_chapter_review(
        "p",
        1,
        get_session=Session,
        decision_refs_for_chapter_review=_noop_decision_refs,
    )

    assert detail.rewrite_attempts[0].attempt_no == 2
    assert detail.rewrite_attempts[0].repair_phase == "canon_repair"
    assert detail.rewrite_attempts[0].phase_attempt_no == 1
