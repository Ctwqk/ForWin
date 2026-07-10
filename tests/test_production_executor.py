from __future__ import annotations

from types import SimpleNamespace

from forwin.application.errors import ActiveGenerationTaskError
from forwin.application.generation import GenerationTaskHandle
from forwin.api_project_payloads import normalize_project_automation
from forwin.models.project import Project
from forwin.production.executor import ProductionExecutor
from forwin.production.planner import ProductionPlan
from forwin.production.policy import policy_from_automation


class RecordingApplicationService:
    def __init__(self, *, task_id: str = "task-1", error: Exception | None = None) -> None:
        self.task_id = task_id
        self.error = error
        self.commands = []

    def enqueue(self, command):
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        return GenerationTaskHandle(task_id=self.task_id, project_id=command.project_id)


def test_executor_starts_initial_generation_task() -> None:
    application = RecordingApplicationService(task_id="task-initial")
    project = Project(id="project-1", title="测试书", premise="前提", genre="玄幻")
    plan = ProductionPlan(
        project_id=project.id,
        date="2026-05-05",
        write_chapters=[1, 2],
        generation_mode="initial",
        requested_chapters=2,
    )

    result = ProductionExecutor(
        generation_application=application,
    ).execute(
        plan=plan,
        project=project,
        policy=policy_from_automation(normalize_project_automation({"daily_chapter_quota": 2})),
    )

    assert result.action == "started_initial_generation"
    assert result.task_id == "task-initial"
    assert application.commands[0].requested_chapters == 2
    assert application.commands[0].project_id == project.id
    assert application.commands[0].root_event_type == "generation_requested"


def test_executor_starts_continue_generation_task() -> None:
    application = RecordingApplicationService(task_id="task-continue")
    project = Project(id="project-1", title="测试书", premise="前提", genre="玄幻")
    plan = ProductionPlan(
        project_id=project.id,
        date="2026-05-05",
        write_chapters=[2, 4, 1],
        generation_mode="continue",
        requested_chapters=3,
    )

    result = ProductionExecutor(
        generation_application=application,
    ).execute(
        plan=plan,
        project=project,
        policy=policy_from_automation(normalize_project_automation({"daily_chapter_quota": 3})),
    )

    assert result.action == "started_continue_generation"
    assert result.task_id == "task-continue"
    assert application.commands[0].requested_chapters == 3
    assert application.commands[0].max_chapters == 3
    assert application.commands[0].root_event_type == "continue_requested"


def test_executor_maps_active_generation_conflict_to_action() -> None:
    project = Project(id="project-1", title="测试书", premise="前提", genre="玄幻")
    plan = ProductionPlan(
        project_id=project.id,
        date="2026-05-05",
        write_chapters=[1],
        generation_mode="continue",
        requested_chapters=1,
    )

    result = ProductionExecutor(
        generation_application=RecordingApplicationService(
            error=ActiveGenerationTaskError("already active")
        ),
    ).execute(
        plan=plan,
        project=project,
        policy=policy_from_automation(normalize_project_automation({})),
    )

    assert result.action == "active_task"
    assert result.task_id == ""


def test_executor_enqueues_publish_jobs_without_running_browser_worker() -> None:
    publish_calls: list[dict] = []
    project = Project(id="project-1", title="测试书", premise="前提", genre="玄幻")
    plan = ProductionPlan(
        project_id=project.id,
        date="2026-05-05",
        publish_chapters=[3],
        publish_jobs=[
            {
                "chapter_title": "第3章",
                "body": "正文",
            }
        ],
    )
    policy = policy_from_automation(
        normalize_project_automation(
            {
                "auto_publish": True,
                "daily_publish_quota": 1,
                "publish": {
                    "platform": "fanqie",
                    "book_name": "番茄版",
                    "create_if_missing": True,
                },
            }
        )
    )

    result = ProductionExecutor(
        generation_application=RecordingApplicationService(),
        publisher_manager_factory=lambda: SimpleNamespace(
            create_upload_jobs_batch=lambda **kwargs: publish_calls.append(kwargs) or 1
        ),
    ).execute(
        plan=plan,
        project=project,
        policy=policy,
    )

    assert result.action == "started_publish_jobs"
    assert result.publish_job_count == 1
    assert publish_calls[0]["platform"] == "fanqie"
    assert publish_calls[0]["jobs"] == [{"chapter_title": "第3章", "body": "正文"}]


def test_executor_consumes_review_quota_jobs_before_reporting_idle() -> None:
    review_calls: list[tuple[str, int]] = []
    approve_calls: list[tuple[str, int]] = []
    project = Project(id="project-1", title="测试书", premise="前提", genre="玄幻")
    plan = ProductionPlan(
        project_id=project.id,
        date="2026-05-05",
        review_chapters=[2, 3],
        review_chapter_statuses={2: "needs_review", 3: "drafted"},
    )

    result = ProductionExecutor(
        generation_application=RecordingApplicationService(),
        review_chapter=lambda project_id, chapter_number: review_calls.append((project_id, chapter_number)),
        approve_chapter_review=lambda project_id, chapter_number: approve_calls.append((project_id, chapter_number)),
    ).execute(
        plan=plan,
        project=project,
        policy=policy_from_automation(
            normalize_project_automation({"daily_chapter_quota": 1, "daily_review_quota": 2})
        ),
    )

    assert result.action == "ran_review_jobs"
    assert result.review_job_count == 2
    assert approve_calls == [(project.id, 2)]
    assert review_calls == [(project.id, 3)]
