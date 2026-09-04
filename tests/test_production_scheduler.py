from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from forwin.application.generation import GenerationTaskHandle
from forwin.config import InfrastructureConfig
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.task import GenerationTask
from forwin.production.scheduler import ProductionScheduler
from forwin.production.planner import ProductionPlan
from tests.postgres import postgres_test_url


def test_scheduler_runs_due_projects_and_preserves_actions() -> None:
    engine = get_engine(postgres_test_url("production-scheduler"))
    init_db(engine)
    Session = get_session_factory(engine)
    commands = []

    class RecordingApplicationService:
        def enqueue(self, command, *, session=None):
            commands.append(command)
            suffix = (
                "initial"
                if command.root_event_type == "generation_requested"
                else "continue"
            )
            return GenerationTaskHandle(
                task_id=f"task-{suffix}",
                project_id=command.project_id,
            )

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
            config=InfrastructureConfig(
                database_url=postgres_test_url("unused-config")
            ),
            generation_application=RecordingApplicationService(),
            display_datetime=lambda value: (
                value.strftime("%Y-%m-%d %H:%M:%S UTC") if value else ""
            ),
            persist_project_automation=lambda session, project, automation: (
                setattr(
                    project,
                    "automation_json",
                    automation.model_dump_json(),
                )
                or automation
            ),
            generation_terminal_statuses={
                "completed",
                "partial_failed",
                "failed",
                "needs_review",
                "cancelled",
                "paused",
            },
            upload_terminal_statuses={"succeeded", "failed", "cancelled"},
        )

        results = scheduler.run_due_projects(
            now=datetime(2026, 5, 5, 17, 0, tzinfo=timezone.utc)
        )

        with Session() as session:
            projects = {
                project.title: json.loads(project.automation_json)
                for project in session.query(Project).all()
            }

        assert [result.project_id for result in results]
        initial_command = next(
            command
            for command in commands
            if command.root_event_type == "generation_requested"
        )
        continue_command = next(
            command
            for command in commands
            if command.root_event_type == "continue_requested"
        )
        assert initial_command.requested_chapters == 2
        assert continue_command.requested_chapters == 1
        assert (
            projects["自动调度-首批"]["last_scheduler_action"]
            == "started_initial_generation"
        )
        assert (
            projects["自动调度-续跑"]["last_scheduler_action"]
            == "started_continue_generation"
        )
        assert projects["自动调度-待审"]["last_scheduler_action"] == "waiting_review"
        assert projects["自动调度-运行中"]["last_scheduler_action"] == "active_task"
        assert projects["自动调度-未到点"].get("last_scheduler_action", "") == ""
    finally:
        engine.dispose()


def test_scheduler_rolls_back_publish_release_when_daily_marker_fails() -> None:
    engine = get_engine(postgres_test_url("production-scheduler-publish-atomic"))
    init_db(engine)
    Session = get_session_factory(engine)
    project_id = new_id()

    class NoopGenerationApplication:
        def enqueue(self, _command):
            raise AssertionError("generation must not run")

    class RecordingPublisherManager:
        def release_canon_jobs(
            self,
            *,
            project_id: str,
            job_ids: list[str],
            publish: bool,
            actor_type: str,
            session=None,
        ):
            owns_session = session is None
            active_session = session or Session()
            try:
                project = active_session.get(Project, project_id)
                assert project is not None
                project.setting_summary = "publisher release committed"
                active_session.flush()
                if owns_session:
                    active_session.commit()
            finally:
                if owns_session:
                    active_session.close()
            return [{"job_id": job_ids[0], "publish": publish, "actor": actor_type}]

    try:
        with Session.begin() as session:
            session.add(
                Project(
                    id=project_id,
                    title="发布原子调度",
                    premise="验证释放与日标记同事务",
                    genre="悬疑",
                    setting_summary="before release",
                    automation_json=json.dumps(
                        {
                            "enabled": True,
                            "daily_start_time": "09:00",
                            "daily_publish_quota": 1,
                            "auto_publish": True,
                            "publish_bindings": [
                                {"platform": "qidian", "book_name": "原子发布"}
                            ],
                        },
                        ensure_ascii=False,
                    ),
                )
            )

        def fail_marker_write(_session, _project, _automation):
            raise RuntimeError("marker write failed")

        scheduler = ProductionScheduler(
            session_factory=Session,
            config=InfrastructureConfig(
                database_url=postgres_test_url("unused-atomic-config")
            ),
            generation_application=NoopGenerationApplication(),
            display_datetime=lambda value: value.isoformat() if value else "",
            persist_project_automation=fail_marker_write,
            generation_terminal_statuses={"completed", "failed", "cancelled"},
            upload_terminal_statuses={"succeeded", "failed", "cancelled"},
            publisher_manager_factory=RecordingPublisherManager,
        )
        scheduler.planner.plan = lambda **_kwargs: ProductionPlan(
            project_id=project_id,
            date="2026-05-05",
            publish_chapters=[1],
            publish_jobs=[{"job_id": "publisher-job-1"}],
        )

        with pytest.raises(RuntimeError, match="marker write failed"):
            scheduler.run_due_projects(
                now=datetime(2026, 5, 5, 17, 0, tzinfo=timezone.utc)
            )

        with Session() as session:
            project = session.get(Project, project_id)
            assert project is not None
            assert project.setting_summary == "before release"
    finally:
        engine.dispose()
