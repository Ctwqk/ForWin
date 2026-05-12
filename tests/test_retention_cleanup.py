from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.genesis import PromptTrace
from forwin.models.observability import PerformanceSpan
from forwin.state.updater import StateUpdater


def test_retention_cleanup_prunes_old_observability_rows_and_candidate_drafts() -> None:
    from forwin.maintenance.retention import RetentionPolicy, run_retention_cleanup

    engine = get_engine(postgres_test_url("retention-cleanup"))
    init_db(engine)
    Session = get_session_factory(engine)
    now = datetime(2026, 5, 11, 12, 0, 0)
    try:
        with Session.begin() as session:
            updater = StateUpdater(session)
            project = updater.create_project(title="保留策略", premise="p", genre="g")
            arc = updater.create_arc_plan(project.id, "主线")
            plan = updater.create_chapter_plan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=1,
                title="第一章",
                one_line="开场",
                goals=["推进"],
            )
            session.add_all(
                [
                    PerformanceSpan(project_id=project.id, span_name="old", created_at=now - timedelta(days=40)),
                    PerformanceSpan(project_id=project.id, span_name="new", created_at=now - timedelta(days=3)),
                    PromptTrace(project_id=project.id, stage_key="old", created_at=now - timedelta(days=40)),
                    PromptTrace(project_id=project.id, stage_key="new", created_at=now - timedelta(days=3)),
                ]
            )
            for index in range(5):
                draft = ChapterDraft(
                    chapter_plan_id=plan.id,
                    version=index + 1,
                    body_text=f"正文{index}",
                    summary="",
                    char_count=10,
                    created_at=now - timedelta(days=5 - index),
                )
                session.add(draft)
                session.flush()
                review = ChapterReview(
                    draft_id=draft.id,
                    verdict="warn",
                    created_at=now - timedelta(days=5 - index),
                )
                session.add(review)
                session.flush()
                session.add(
                    CandidateDraftRecord(
                        project_id=project.id,
                        chapter_plan_id=plan.id,
                        chapter_number=1,
                        candidate_draft_id=draft.id,
                        review_id=review.id,
                        version=index + 1,
                        created_at=now - timedelta(days=5 - index),
                        updated_at=now - timedelta(days=5 - index),
                    )
                )

        with Session.begin() as session:
            result = run_retention_cleanup(
                session,
                RetentionPolicy(
                    performance_span_days=30,
                    prompt_trace_days=30,
                    candidate_drafts_keep_per_chapter=2,
                ),
                now=now,
            )

            span_names = session.execute(select(PerformanceSpan.span_name)).scalars().all()
            trace_stages = session.execute(select(PromptTrace.stage_key)).scalars().all()
            draft_versions = session.execute(
                select(CandidateDraftRecord.version).order_by(CandidateDraftRecord.version)
            ).scalars().all()

        assert result.performance_spans_deleted == 1
        assert result.prompt_traces_deleted == 1
        assert result.candidate_drafts_deleted == 3
        assert span_names == ["new"]
        assert trace_stages == ["new"]
        assert draft_versions == [4, 5]
    finally:
        engine.dispose()
