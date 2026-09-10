from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from time import monotonic
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text

from forwin.application.generation import (
    EnqueueGenerationCommand,
    GenerationApplicationService,
)
from forwin.api_schema import ProjectAutomationUpdateRequest
from forwin.application.projects.generation import update_project_automation
from forwin.config import InfrastructureConfig
from forwin.models.audit import DecisionEvent
from forwin.models.base import get_engine, get_session_factory, new_id
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.publisher import PublisherUploadJob
from forwin.models.task import GenerationTask
from forwin.production.scheduler import ProductionScheduler
from forwin.runtime.policy import RuntimePolicy
from forwin.runtime.policy_store import ProjectPolicyStore
from tests.postgres import postgres_test_url
from tests.test_canon_publisher_jobs import _fixture, _materialize


NOW = datetime(2026, 9, 4, 17, tzinfo=timezone.utc)
TODAY = NOW.date().isoformat()
TERMINAL = {
    "completed",
    "partial_failed",
    "failed",
    "needs_review",
    "cancelled",
    "paused",
}


@pytest.fixture
def sessions():
    engine = get_engine(postgres_test_url("scheduler-recovery"))
    try:
        yield get_session_factory(engine)
    finally:
        engine.dispose()


def _project(sessions, *, status="planned", automation=None):
    project_id = new_id()
    with sessions.begin() as session:
        project = Project(
            id=project_id,
            title="同日恢复",
            premise="调度回归",
            genre="悬疑",
            creation_status="writing",
            automation_json=json.dumps(
                {
                    "enabled": True,
                    "primary_publish_platform": "qidian",
                    "daily_start_time": "09:00",
                    "daily_chapter_quota": 1,
                    **(automation or {}),
                }
            ),
        )
        ProjectPolicyStore(session).initialize(
            project, RuntimePolicy.for_profile("standard")
        )
        arc = ArcPlanVersion(
            id=new_id(),
            project_id=project_id,
            version=1,
            arc_synopsis="开篇",
            status="active",
        )
        session.add(arc)
        session.flush()
        session.add(
            ChapterPlan(
                id=new_id(),
                project_id=project_id,
                arc_plan_id=arc.id,
                chapter_number=1,
                title="第一章",
                status=status,
            )
        )
    return project_id


def _persist(session, project, automation):
    project.automation_json = automation.model_dump_json()
    session.flush()
    return automation


def _application(sessions):
    return GenerationApplicationService(
        session_factory=sessions,
        infrastructure=InfrastructureConfig(database_url="sqlite+pysqlite:///:memory:"),
    )


def _scheduler(sessions, **kwargs):
    return ProductionScheduler(
        session_factory=sessions,
        config=InfrastructureConfig(database_url="sqlite+pysqlite:///:memory:"),
        generation_application=kwargs.pop(
            "generation_application", _application(sessions)
        ),
        display_datetime=lambda value: value.isoformat(),
        persist_project_automation=kwargs.pop("persist_project_automation", _persist),
        generation_terminal_statuses=TERMINAL,
        upload_terminal_statuses={"succeeded", "failed", "cancelled"},
        **kwargs,
    )


def _tasks(sessions):
    with sessions() as session:
        return list(
            session.scalars(select(GenerationTask).order_by(GenerationTask.created_at))
        )


def _automation(sessions, project_id):
    with sessions() as session:
        return json.loads(session.get(Project, project_id).automation_json)


@pytest.mark.parametrize("blocker", ["active_task", "waiting_review", "idle"])
def test_same_day_blocked_or_idle_project_recovers_once_after_restart(
    sessions, blocker
):
    status = {
        "active_task": "planned",
        "waiting_review": "needs_review",
        "idle": "accepted",
    }[blocker]
    project_id = _project(sessions, status=status)
    if blocker == "active_task":
        with sessions.begin() as session:
            session.add(
                GenerationTask(id="old-task", project_id=project_id, status="running")
            )
    first = _scheduler(sessions).run_due_projects(now=NOW)
    assert first[0].action == blocker
    assert _automation(sessions, project_id).get("last_scheduler_date") != TODAY
    with sessions.begin() as session:
        session.scalar(select(ChapterPlan)).status = "planned"
        old_task = session.get(GenerationTask, "old-task")
        if old_task is not None:
            old_task.status = "completed"
    second = _scheduler(sessions).run_due_projects(now=NOW + timedelta(minutes=1))
    assert len(second) == 1
    task_id = second[0].task_id
    assert task_id
    with sessions.begin() as session:
        session.get(GenerationTask, task_id).status = "completed"
    assert _scheduler(sessions).run_due_projects(now=NOW + timedelta(minutes=2)) == []
    assert len(_tasks(sessions)) == (2 if blocker == "active_task" else 1)
    next_day = _scheduler(sessions).run_due_projects(now=NOW + timedelta(days=1))
    assert len(next_day) == 1
    assert next_day[0].task_id != task_id


@pytest.mark.parametrize("action", ["active_task", "waiting_review", "idle", "blocked"])
def test_legacy_blocked_day_marker_does_not_prevent_recovery(sessions, action):
    project_id = _project(
        sessions,
        automation={
            "last_scheduler_date": TODAY,
            "last_scheduler_action": action,
        },
    )
    result = _scheduler(sessions).run_due_projects(now=NOW)
    assert len(result) == 1
    assert result[0].task_id
    assert _automation(sessions, project_id)["last_scheduler_date"] == TODAY


def test_daily_marker_failure_rolls_back_real_generation_task_and_root_event(sessions):
    project_id = _project(sessions)

    def fail_marker(session, project, automation):
        _persist(session, project, automation)
        raise RuntimeError("marker commit failed")

    with pytest.raises(RuntimeError, match="marker commit failed"):
        _scheduler(sessions, persist_project_automation=fail_marker).run_due_projects(
            now=NOW
        )
    assert _tasks(sessions) == []
    with sessions() as session:
        assert session.scalar(select(func.count(DecisionEvent.id))) == 0
    assert _automation(sessions, project_id).get("last_scheduler_date") != TODAY
    assert _scheduler(sessions).run_due_projects(now=NOW)[0].task_id


@pytest.mark.parametrize("commit", [False, True])
def test_enqueue_uses_callers_transaction_without_committing(sessions, commit):
    project_id = _project(sessions)
    command = EnqueueGenerationCommand(
        project_id=project_id,
        requested_chapters=1,
        max_chapters=1,
        run_until_chapter=1,
        auto_continue=False,
        title="测试",
        subtitle="测试",
        root_event_type="generation_requested",
    )
    with sessions() as session:
        handle = _application(sessions).enqueue(command, session=session)
        assert session.get(GenerationTask, handle.task_id) is not None
        assert _tasks(sessions) == []
        if commit:
            session.commit()
        else:
            session.rollback()
    assert len(_tasks(sessions)) == int(commit)


def test_concurrent_ticks_cannot_reserve_a_second_daily_batch(sessions):
    _project(sessions)
    entered, resume = Event(), Event()

    class PausingApplication(GenerationApplicationService):
        def enqueue(self, command, **kwargs):
            entered.set()
            assert resume.wait(10), "test did not release enqueue"
            return super().enqueue(command, **kwargs)

    application = PausingApplication(
        session_factory=sessions,
        infrastructure=InfrastructureConfig(database_url="sqlite+pysqlite:///:memory:"),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            _scheduler(sessions, generation_application=application).run_due_projects,
            now=NOW,
        )
        try:
            assert entered.wait(5)
            second = pool.submit(_scheduler(sessions).run_due_projects, now=NOW)
            assert second.result(timeout=3) == []
            assert _tasks(sessions) == []
        finally:
            resume.set()
        assert first.result(timeout=5)[0].task_id
    assert len(_tasks(sessions)) == 1


def test_settings_update_waits_for_dispatch_and_preserves_daily_reservation(sessions):
    project_id = _project(sessions)
    reserved, resume, settings_started = Event(), Event(), Event()
    settings_backend = []

    def pause_before_dispatch_commit(session, project, automation):
        stored = _persist(session, project, automation)
        reserved.set()
        assert resume.wait(10), "test did not release scheduler transaction"
        return stored

    def settings_session():
        session = sessions()
        settings_backend.append(session.scalar(text("SELECT pg_backend_pid()")))
        settings_started.set()
        return session

    with ThreadPoolExecutor(max_workers=2) as pool:
        dispatch = pool.submit(
            _scheduler(
                sessions, persist_project_automation=pause_before_dispatch_commit
            ).run_due_projects,
            now=NOW,
        )
        try:
            assert reserved.wait(5)
            settings = pool.submit(
                update_project_automation,
                project_id,
                ProjectAutomationUpdateRequest(
                    enabled=True, daily_start_time="09:00", daily_chapter_quota=2
                ),
                get_session=settings_session,
                persist_project_automation=_persist,
            )
            assert settings_started.wait(5)
            deadline = monotonic() + 5
            with sessions() as observer:
                while not observer.scalar(
                    text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"),
                    {"pid": settings_backend[0]},
                ):
                    assert monotonic() < deadline, "settings did not overlap dispatch"
                    assert not settings.done(), "settings unexpectedly finished"
                    resume.wait(0.01)
        finally:
            resume.set()
        result = dispatch.result(timeout=5)
        assert settings.result(timeout=5).ok

    automation = _automation(sessions, project_id)
    assert automation["last_scheduler_date"] == TODAY
    assert automation["daily_chapter_quota"] == 2
    with sessions.begin() as session:
        session.get(GenerationTask, result[0].task_id).status = "completed"
    assert _scheduler(sessions).run_due_projects(now=NOW + timedelta(minutes=1)) == []
    assert len(_tasks(sessions)) == 1


def test_review_callback_can_lock_project_and_refreshed_settings_control_dispatch(
    sessions,
):
    project_id = _project(
        sessions,
        status="drafted",
        automation={
            "daily_review_quota": 1,
            "stop_when_review_pending": False,
        },
    )

    with sessions.begin() as session:
        chapter = session.scalar(select(ChapterPlan))
        session.add(
            ChapterPlan(
                id=new_id(),
                project_id=project_id,
                arc_plan_id=chapter.arc_plan_id,
                chapter_number=2,
                title="第二章",
                status="planned",
            )
        )

    def accept_in_another_transaction(project_id, chapter_number):
        with sessions.begin() as session:
            session.execute(text("SET LOCAL lock_timeout = '500ms'"))
            project = session.scalar(
                select(Project).where(Project.id == project_id).with_for_update()
            )
            chapter = session.scalar(
                select(ChapterPlan).where(ChapterPlan.chapter_number == chapter_number)
            )
            chapter.status = "accepted"
            automation = json.loads(project.automation_json)
            automation["daily_chapter_quota"] = 2
            project.automation_json = json.dumps(automation)
            session.add(
                ChapterPlan(
                    id=new_id(),
                    project_id=project_id,
                    arc_plan_id=chapter.arc_plan_id,
                    chapter_number=3,
                    title="第三章",
                    status="planned",
                )
            )

    result = _scheduler(
        sessions, review_chapter=accept_in_another_transaction
    ).run_due_projects(now=NOW)
    assert result[0].task_id
    assert result[0].execution.review_job_count == 1
    assert _tasks(sessions)[0].run_until_chapter == 3
    assert _tasks(sessions)[0].requested_chapters == 2
    assert _automation(sessions, project_id)["daily_chapter_quota"] == 2


def test_review_commit_before_scheduler_crash_is_not_repeated(sessions):
    project_id = _project(
        sessions, status="drafted", automation={"daily_review_quota": 1}
    )
    accepted = []

    def accept_then_crash(project_id, chapter_number):
        with sessions.begin() as session:
            chapter = session.scalar(select(ChapterPlan))
            assert chapter.status == "drafted"
            chapter.status = "accepted"
        accepted.append(chapter_number)
        raise RuntimeError("crash after Canon")

    with pytest.raises(RuntimeError, match="crash after Canon"):
        _scheduler(sessions, review_chapter=accept_then_crash).run_due_projects(now=NOW)
    assert _automation(sessions, project_id).get("last_scheduler_date") != TODAY
    _scheduler(sessions, review_chapter=accept_then_crash).run_due_projects(now=NOW)
    assert accepted == [1]


def test_publish_release_and_day_reservation_rollback_and_retry_together():
    fixture = _fixture("scheduler-canon-atomic")
    sessions = fixture.runtime.session_factory
    try:
        job_id = _materialize(fixture)[0]["job_id"]
        with sessions.begin() as session:
            project = session.get(Project, fixture.project_id)
            automation = json.loads(project.automation_json)
            automation.update(enabled=True, auto_publish=True, daily_publish_quota=1)
            project.automation_json = json.dumps(automation)
        manager = SimpleNamespace(release_canon_jobs=fixture.runtime.canon_jobs.release)

        def fail_marker(session, project, automation):
            _persist(session, project, automation)
            raise RuntimeError("marker failure")

        with pytest.raises(RuntimeError, match="marker failure"):
            _scheduler(
                sessions,
                publisher_manager_factory=lambda: manager,
                persist_project_automation=fail_marker,
            ).run_due_projects(now=NOW)
        with sessions() as session:
            assert session.get(PublisherUploadJob, job_id).status == "scheduled"
        assert (
            _automation(sessions, fixture.project_id).get("last_scheduler_date")
            != TODAY
        )
        result = _scheduler(
            sessions, publisher_manager_factory=lambda: manager
        ).run_due_projects(now=NOW)
        assert result[0].execution.publish_job_count == 1
        with sessions.begin() as session:
            session.get(PublisherUploadJob, job_id).status = "succeeded"
        second_id = _materialize(
            fixture,
            bindings=[
                {
                    "platform": "fanqie",
                    "book_name": "另一平台",
                    "publisher_compliance_required": False,
                }
            ],
        )[0]["job_id"]
        with sessions.begin() as session:
            project = session.get(Project, fixture.project_id)
            automation = json.loads(project.automation_json)
            automation["publish_bindings"].append(
                {"platform": "fanqie", "book_name": "另一平台"}
            )
            project.automation_json = json.dumps(automation)
        assert (
            _scheduler(
                sessions, publisher_manager_factory=lambda: manager
            ).run_due_projects(now=NOW)
            == []
        )
        with sessions() as session:
            assert session.get(PublisherUploadJob, second_id).status == "scheduled"
        next_day = _scheduler(
            sessions, publisher_manager_factory=lambda: manager
        ).run_due_projects(now=NOW + timedelta(days=1))
        assert next_day[0].execution.publish_job_count == 1
    finally:
        fixture.engine.dispose()


def test_active_task_constraint_does_not_abort_callers_other_work(
    sessions, monkeypatch
):
    from forwin.application.errors import ActiveGenerationTaskError
    from forwin.generation.task_repository import GenerationTaskRepository

    project_id = _project(sessions)
    command = EnqueueGenerationCommand(
        project_id=project_id,
        requested_chapters=1,
        max_chapters=1,
        run_until_chapter=1,
        auto_continue=False,
        title="测试",
        subtitle="测试",
        root_event_type="generation_requested",
    )
    checked, resume = Event(), Event()
    original_has_active = GenerationTaskRepository.has_active
    caller_sessions = []

    def pause_after_active_check(repository, project_id):
        found = original_has_active(repository, project_id)
        if repository.session in caller_sessions:
            checked.set()
            assert resume.wait(10)
        return found

    monkeypatch.setattr(
        GenerationTaskRepository, "has_active", pause_after_active_check
    )

    def caller():
        with sessions.begin() as session:
            caller_sessions.append(session)
            with pytest.raises(ActiveGenerationTaskError):
                _application(sessions).enqueue(command, session=session)
            session.get(
                Project, project_id
            ).setting_summary = "caller transaction survived"

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(caller)
        try:
            assert checked.wait(5)
            _application(sessions).enqueue(command)
        finally:
            resume.set()
        pending.result(timeout=5)
    with sessions() as session:
        assert session.scalar(select(func.count(GenerationTask.id))) == 1
        assert session.scalar(select(func.count(DecisionEvent.id))) == 1
        assert (
            session.get(Project, project_id).setting_summary
            == "caller transaction survived"
        )


def test_review_callbacks_are_serialized_before_any_project_row_lock(sessions):
    _project(sessions, status="drafted", automation={"daily_review_quota": 1})
    entered, resume = Event(), Event()
    reviewed = []

    def review(project_id, chapter_number):
        entered.set()
        assert resume.wait(10)
        with sessions.begin() as session:
            session.execute(text("SET LOCAL lock_timeout = '500ms'"))
            session.scalar(
                select(Project).where(Project.id == project_id).with_for_update()
            )
            chapter = session.scalar(select(ChapterPlan))
            assert chapter.status == "drafted"
            chapter.status = "accepted"
        reviewed.append(chapter_number)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            _scheduler(sessions, review_chapter=review).run_due_projects, now=NOW
        )
        try:
            assert entered.wait(5)
            second = pool.submit(
                _scheduler(sessions, review_chapter=review).run_due_projects, now=NOW
            )
            assert second.result(timeout=3) == []
        finally:
            resume.set()
        assert first.result(timeout=5)[0].execution.review_job_count == 1
    assert reviewed == [1]


def test_no_publish_release_leaves_daily_slot_available_for_recovery():
    fixture = _fixture("scheduler-publish-preflight")
    sessions = fixture.runtime.session_factory
    try:
        job_id = _materialize(fixture)[0]["job_id"]
        with sessions.begin() as session:
            project = session.get(Project, fixture.project_id)
            automation = json.loads(project.automation_json)
            automation.update(enabled=True, auto_publish=True, daily_publish_quota=1)
            project.automation_json = json.dumps(automation)
        unavailable = SimpleNamespace(release_canon_jobs=lambda **_kwargs: [])
        first = _scheduler(
            sessions, publisher_manager_factory=lambda: unavailable
        ).run_due_projects(now=NOW)
        assert first[0].action == "idle"
        assert (
            _automation(sessions, fixture.project_id).get("last_scheduler_date")
            != TODAY
        )
        manager = SimpleNamespace(release_canon_jobs=fixture.runtime.canon_jobs.release)
        second = _scheduler(
            sessions, publisher_manager_factory=lambda: manager
        ).run_due_projects(now=NOW)
        assert second[0].execution.publish_job_count == 1
        with sessions() as session:
            assert session.get(PublisherUploadJob, job_id).status == "pending"
    finally:
        fixture.engine.dispose()
