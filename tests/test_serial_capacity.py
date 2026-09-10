from datetime import UTC, datetime

import pytest

from forwin.production.backlog import ProductionBacklog, ProductionPublishJob
from forwin.production.planner import ProductionPlanner
from forwin.production.policy import ProductionPolicy, ProductionQuota


@pytest.mark.parametrize(
    "total,expected",
    [(1, 1), (2, 2), (60, 3), (100, 5), (200, 10), (500, 25), (1000, 50)],
)
def test_serial_buffer_formula(total, expected):
    from forwin.production import policy

    assert hasattr(policy, "serial_buffer_limit"), "missing 5% serial capacity policy"
    assert policy.serial_buffer_limit(total) == expected


@pytest.mark.parametrize("blocked", ["active", "review", "capacity"])
def test_generation_wait_does_not_starve_confirmable_publish_jobs(blocked):
    backlog = ProductionBacklog(
        project_id="p",
        planned_unwritten=[6],
        chapter_plan_count=6,
        reviewed_unpublished=[1],
        has_active_generation_task=blocked == "active",
        needs_review=[6] if blocked == "review" else [],
        scheduled_publish_jobs=[
            ProductionPublishJob(
                job_id="j",
                idempotency_key="j",
                canon_commit_id="c",
                candidate_id="d",
                chapter_number=1,
                platform="fanqie",
            )
        ],
    )
    if blocked == "capacity":
        # Existing backlog models must expose the authoritative capacity read view.
        assert "capacity_available" in ProductionBacklog.model_fields
        backlog.capacity_available = 0
        backlog.capacity_wait_reason = "serial_buffer_full"
    plan = ProductionPlanner().plan(
        policy=ProductionPolicy(
            enabled=True,
            auto_publish=True,
            quota=ProductionQuota(write=1, publish=1),
            publish_bindings=[{"platform": "fanqie"}],
        ),
        backlog=backlog,
        now=datetime.now(UTC),
    )
    assert plan.write_chapters == []
    assert plan.publish_chapters == [1]
    assert plan.publish_jobs[0]["job_id"] == "j"


def test_executor_enqueue_race_still_releases_ready_publication():
    from types import SimpleNamespace

    from forwin.application.errors import ActiveGenerationTaskError
    from forwin.models.project import Project
    from forwin.production.executor import ProductionExecutor
    from forwin.production.planner import ProductionPlan

    def enqueue(*args, **kwargs):
        raise ActiveGenerationTaskError("racing active task")

    calls = []
    executor = ProductionExecutor(
        generation_application=SimpleNamespace(enqueue=enqueue),
        publisher_manager_factory=lambda: SimpleNamespace(
            release_canon_jobs=lambda **kwargs: calls.append(kwargs) or ["job"]
        ),
    )
    result = executor.execute(
        plan=ProductionPlan(
            project_id="p",
            date="2026-09-09",
            generation_mode="continue",
            write_chapters=[2],
            publish_jobs=[{"job_id": "job"}],
        ),
        project=Project(id="p", title="book", premise="story"),
        policy=ProductionPolicy(
            enabled=True, auto_publish=True, publish_bindings=[{"platform": "fanqie"}]
        ),
    )
    assert result.publish_job_count == 1
    assert calls[0]["job_ids"] == ["job"]


def test_chapter_loop_capacity_wait_returns_without_repair_or_failure():
    from types import SimpleNamespace

    from forwin.generation.pipeline_core.project_chapters import ChapterExecutionStage
    from forwin.production.capacity import CapacityWait

    def reserve(*args):
        raise CapacityWait("serial_buffer_full", chapter_number=6)

    stage = SimpleNamespace(
        _project_policy=lambda *args: None,
        _abort_requested=lambda: False,
        _pause_requested=lambda: False,
        capacity_reserver=reserve,
    )
    result = ChapterExecutionStage._run_project_chapters(
        stage,
        session=None,
        repo=SimpleNamespace(get_project=lambda _: object()),
        updater=None,
        checker=None,
        project_id="p",
        chapter_numbers=[6],
        requested_chapters=1,
    )
    assert result.status == "capacity_wait"
    assert result.capacity_wait_chapter == 6
    assert result.failed_chapters == []
    assert result.paused_chapters == []


def test_primary_capacity_setting_survives_public_normalization():
    from forwin.api_schema import ProjectAutomationUpdateRequest, ProjectCreateRequest
    from forwin.application.read_models import normalize_project_automation

    created = ProjectCreateRequest(
        title="book", premise="story", primary_publish_platform="qidian"
    )
    updated = ProjectAutomationUpdateRequest(
        primary_publish_platform="fanqie", target_total_chapters=60
    )
    assert created.primary_publish_platform == "qidian"
    assert updated.target_total_chapters == 60
    assert (
        normalize_project_automation(
            {"primary_publish_platform": "fanqie"}
        ).primary_publish_platform
        == "fanqie"
    )
    with pytest.raises(ValueError):
        ProjectAutomationUpdateRequest(primary_publish_platform="unsupported")


def test_wait_task_controls_and_mode_are_visible_without_false_failure():
    from forwin.http.tasks import (
        _serialize_task,
        _task_is_pausable,
        _task_is_terminable,
    )

    task = {
        "status": "capacity_wait",
        "current_stage": "capacity_wait",
        "project_id": "p",
        "message": "serial_buffer_full",
        "execution_payload": {
            "long_run_mode": "daily_serial",
            "isolated": False,
            "capacity_config_version": 2,
        },
    }
    assert _task_is_pausable(task)
    assert _task_is_terminable(task)
    response = _serialize_task("task", task)
    assert response.status == "capacity_wait"
    assert response.capacity_config_version == 2
    assert response.long_run_mode == "daily_serial"
    assert response.error is None
