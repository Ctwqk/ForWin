from __future__ import annotations

from types import SimpleNamespace

from forwin.api_project_payloads import normalize_project_automation
from forwin.models.project import Project
from forwin.production.executor import ProductionExecutor
from forwin.production.planner import ProductionPlan
from forwin.production.policy import policy_from_automation


class ActiveGenerationTaskError(RuntimeError):
    pass


def test_executor_starts_initial_generation_task() -> None:
    calls: list[dict] = []
    project = Project(id="project-1", title="测试书", premise="前提", genre="玄幻")
    plan = ProductionPlan(
        project_id=project.id,
        date="2026-05-05",
        write_chapters=[1, 2],
        generation_mode="initial",
        requested_chapters=2,
    )

    result = ProductionExecutor(
        create_generation_task=lambda **kwargs: calls.append(kwargs) or "task-initial",
        create_continue_generation_task=lambda **_kwargs: "unexpected",
        active_generation_task_error_cls=ActiveGenerationTaskError,
    ).execute(
        plan=plan,
        project=project,
        policy=policy_from_automation(normalize_project_automation({"daily_chapter_quota": 2})),
        runtime_config=SimpleNamespace(),
    )

    assert result.action == "started_initial_generation"
    assert result.task_id == "task-initial"
    assert calls[0]["num_chapters"] == 2
    assert calls[0]["project_id"] == project.id


def test_executor_starts_continue_generation_task() -> None:
    calls: list[dict] = []
    project = Project(id="project-1", title="测试书", premise="前提", genre="玄幻")
    plan = ProductionPlan(
        project_id=project.id,
        date="2026-05-05",
        write_chapters=[2, 4, 1],
        generation_mode="continue",
        requested_chapters=3,
    )

    result = ProductionExecutor(
        create_generation_task=lambda **_kwargs: "unexpected",
        create_continue_generation_task=lambda **kwargs: calls.append(kwargs) or "task-continue",
        active_generation_task_error_cls=ActiveGenerationTaskError,
    ).execute(
        plan=plan,
        project=project,
        policy=policy_from_automation(normalize_project_automation({"daily_chapter_quota": 3})),
        runtime_config=SimpleNamespace(),
    )

    assert result.action == "started_continue_generation"
    assert result.task_id == "task-continue"
    assert calls[0]["requested_chapters"] == 3
    assert calls[0]["max_chapters"] == 3


def test_executor_maps_active_generation_conflict_to_action() -> None:
    project = Project(id="project-1", title="测试书", premise="前提", genre="玄幻")
    plan = ProductionPlan(
        project_id=project.id,
        date="2026-05-05",
        write_chapters=[1],
        generation_mode="continue",
        requested_chapters=1,
    )

    def raise_active(**_kwargs):
        raise ActiveGenerationTaskError("already active")

    result = ProductionExecutor(
        create_generation_task=lambda **_kwargs: "unexpected",
        create_continue_generation_task=raise_active,
        active_generation_task_error_cls=ActiveGenerationTaskError,
    ).execute(
        plan=plan,
        project=project,
        policy=policy_from_automation(normalize_project_automation({})),
        runtime_config=SimpleNamespace(),
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
        create_generation_task=lambda **_kwargs: "unexpected",
        create_continue_generation_task=lambda **_kwargs: "unexpected",
        active_generation_task_error_cls=ActiveGenerationTaskError,
        publisher_manager_factory=lambda: SimpleNamespace(
            create_upload_jobs_batch=lambda **kwargs: publish_calls.append(kwargs) or 1
        ),
    ).execute(
        plan=plan,
        project=project,
        policy=policy,
        runtime_config=SimpleNamespace(),
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
        create_generation_task=lambda **_kwargs: "unexpected",
        create_continue_generation_task=lambda **_kwargs: "unexpected",
        active_generation_task_error_cls=ActiveGenerationTaskError,
        review_chapter=lambda project_id, chapter_number: review_calls.append((project_id, chapter_number)),
        approve_chapter_review=lambda project_id, chapter_number: approve_calls.append((project_id, chapter_number)),
    ).execute(
        plan=plan,
        project=project,
        policy=policy_from_automation(
            normalize_project_automation({"daily_chapter_quota": 1, "daily_review_quota": 2})
        ),
        runtime_config=SimpleNamespace(),
    )

    assert result.action == "ran_review_jobs"
    assert result.review_job_count == 2
    assert approve_calls == [(project.id, 2)]
    assert review_calls == [(project.id, 3)]
