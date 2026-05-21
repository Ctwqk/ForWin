from __future__ import annotations

import json
from datetime import datetime, timezone

from forwin.config import Config
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.task import GenerationTask
from forwin.production.scheduler import ProductionScheduler


class ActiveGenerationTaskError(RuntimeError):
    pass


def test_scheduler_runs_due_projects_and_preserves_actions() -> None:
    engine = get_engine(postgres_test_url("production-scheduler"))
    init_db(engine)
    Session = get_session_factory(engine)
    initial_calls: list[dict] = []
    continue_calls: list[dict] = []
    try:
        with Session.begin() as session:
            ready_payload = json.dumps(
                {
                    "enabled": True,
                    "daily_start_time": "09:00",
                    "daily_chapter_quota": 2,
                },
                ensure_ascii=False,
            )
            future_payload = json.dumps(
                {
                    "enabled": True,
                    "daily_start_time": "23:00",
                    "daily_chapter_quota": 2,
                },
                ensure_ascii=False,
            )
            project_initial = Project(
                id=new_id(),
                title="自动调度-首批",
                premise="前提",
                genre="玄幻",
                automation_json=ready_payload,
            )
            project_continue = Project(
                id=new_id(),
                title="自动调度-续跑",
                premise="前提",
                genre="玄幻",
                automation_json=ready_payload,
            )
            project_waiting = Project(
                id=new_id(),
                title="自动调度-待审",
                premise="前提",
                genre="玄幻",
                automation_json=ready_payload,
            )
            project_active = Project(
                id=new_id(),
                title="自动调度-运行中",
                premise="前提",
                genre="玄幻",
                automation_json=ready_payload,
            )
            project_future = Project(
                id=new_id(),
                title="自动调度-未到点",
                premise="前提",
                genre="玄幻",
                automation_json=future_payload,
            )
            session.add_all(
                [
                    project_initial,
                    project_continue,
                    project_waiting,
                    project_active,
                    project_future,
                ]
            )
            session.flush()
            for project, status in [
                (project_continue, "planned"),
                (project_waiting, "needs_review"),
                (project_active, "planned"),
            ]:
                arc = ArcPlanVersion(
                    id=new_id(),
                    project_id=project.id,
                    version=1,
                    arc_synopsis="弧线",
                    status="active",
                )
                session.add(arc)
                session.flush()
                session.add(
                    ChapterPlan(
                        id=new_id(),
                        project_id=project.id,
                        arc_plan_id=arc.id,
                        chapter_number=1,
                        title="第一章",
                        status=status,
                    )
                )
                if project is project_continue:
                    future_arc = ArcPlanVersion(
                        id=new_id(),
                        project_id=project.id,
                        version=1,
                        arc_synopsis="后续弧线",
                        status="planned",
                        arc_number=2,
                        chapter_start=2,
                        chapter_end=2,
                    )
                    session.add(future_arc)
                    session.flush()
                    session.add(
                        ChapterPlan(
                            id=new_id(),
                            project_id=project.id,
                            arc_plan_id=future_arc.id,
                            chapter_number=2,
                            title="第二章",
                            status="planned",
                        )
                    )
            session.add(
                GenerationTask(
                    id="task-active",
                    task_kind="generation",
                    project_id=project_active.id,
                    status="running",
                    title="running",
                )
            )

        scheduler = ProductionScheduler(
            session_factory=Session,
            config=Config(database_url=postgres_test_url("unused-config")),
            runtime_config_provider=lambda: Config(database_url=postgres_test_url("unused-runtime")),
            display_datetime=lambda value: value.strftime("%Y-%m-%d %H:%M:%S UTC") if value else "",
            persist_project_automation=lambda session, project, automation: setattr(
                project,
                "automation_json",
                automation.model_dump_json(),
            )
            or automation,
            create_generation_task=lambda **kwargs: initial_calls.append(kwargs) or "task-initial",
            create_continue_generation_task=lambda **kwargs: continue_calls.append(kwargs) or "task-continue",
            active_generation_task_error_cls=ActiveGenerationTaskError,
            generation_terminal_statuses={"completed", "partial_failed", "failed", "needs_review", "cancelled", "paused"},
            upload_terminal_statuses={"succeeded", "failed", "cancelled"},
        )

        results = scheduler.run_due_projects(now=datetime(2026, 5, 5, 17, 0, tzinfo=timezone.utc))

        with Session() as session:
            projects = {
                project.title: json.loads(project.automation_json)
                for project in session.query(Project).all()
            }

        assert [result.project_id for result in results]
        assert initial_calls[0]["num_chapters"] == 2
        assert continue_calls[0]["requested_chapters"] == 1
        assert projects["自动调度-首批"]["last_scheduler_action"] == "started_initial_generation"
        assert projects["自动调度-续跑"]["last_scheduler_action"] == "started_continue_generation"
        assert projects["自动调度-待审"]["last_scheduler_action"] == "waiting_review"
        assert projects["自动调度-运行中"]["last_scheduler_action"] == "active_task"
        assert projects["自动调度-未到点"].get("last_scheduler_action", "") == ""
    finally:
        engine.dispose()
