from __future__ import annotations

from sqlalchemy import select

from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.draft import ChapterDraft
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.publisher import PublisherUploadJob
from forwin.models.task import GenerationTask
from forwin.production.repository import ProductionRepository


def test_repository_loads_backlog_statuses_and_active_tasks() -> None:
    engine = get_engine(postgres_test_url("production-backlog"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            project = Project(
                id=new_id(),
                title="生产队列书",
                premise="前提",
                genre="玄幻",
            )
            session.add(project)
            session.flush()
            arc = ArcPlanVersion(
                id=new_id(),
                project_id=project.id,
                version=1,
                arc_synopsis="弧线",
                status="active",
            )
            session.add(arc)
            session.flush()
            plans = []
            for chapter_number, status in [
                (1, "planned"),
                (2, "failed"),
                (3, "drafted"),
                (4, "needs_review"),
                (5, "accepted"),
                (6, "accepted"),
            ]:
                plan = ChapterPlan(
                    id=new_id(),
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=chapter_number,
                    title=f"第{chapter_number}章",
                    one_line="推进",
                    goals_json="[]",
                    status=status,
                )
                plans.append(plan)
            session.add_all(plans)
            session.flush()
            session.add_all(
                [
                    ChapterDraft(
                        id=new_id(),
                        chapter_plan_id=plans[4].id,
                        version=1,
                        body_text="第五章正文",
                        summary="摘要",
                        char_count=100,
                    ),
                    ChapterDraft(
                        id=new_id(),
                        chapter_plan_id=plans[5].id,
                        version=1,
                        body_text="第六章正文",
                        summary="摘要",
                        char_count=100,
                    ),
                ]
            )
            session.add(
                PublisherUploadJob(
                    id="upload-existing",
                    project_id=project.id,
                    platform_id="fanqie",
                    status="pending",
                    book_name=project.title,
                    chapter_title="第6章",
                    body_text="第六章正文",
                )
            )
            session.add(
                GenerationTask(
                    id="task-running",
                    task_kind="generation",
                    project_id=project.id,
                    status="running",
                    title="running",
                )
            )
            project_id = project.id

        with Session() as session:
            backlog = ProductionRepository(session).load_backlogs(
                [project_id],
                generation_terminal_statuses={"completed", "failed", "cancelled"},
                upload_terminal_statuses={"succeeded", "failed", "cancelled"},
            )[project_id]

        assert backlog.chapter_plan_count == 6
        assert backlog.has_existing_chapter_plans is True
        assert backlog.planned_unwritten == [1]
        assert backlog.failed == [2]
        assert backlog.drafted_unreviewed == [3]
        assert backlog.needs_review == [4]
        assert backlog.reviewed_unpublished == [5]
        assert backlog.has_active_generation_task is True
        assert backlog.has_active_upload_task is True

        with Session() as session:
            assert session.execute(select(PublisherUploadJob)).scalar_one().status == "pending"
    finally:
        engine.dispose()
