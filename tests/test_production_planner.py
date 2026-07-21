from __future__ import annotations

from datetime import datetime, timezone

from forwin.production.backlog import ProductionBacklog, ProductionPublishJob
from forwin.production.planner import ProductionPlanner
from forwin.production.policy import ProductionPolicy, ProductionQuota


def test_planner_blocks_when_review_is_pending() -> None:
    plan = ProductionPlanner().plan(
        policy=ProductionPolicy(enabled=True, quota=ProductionQuota(write=2)),
        backlog=ProductionBacklog(
            project_id="project-1",
            planned_unwritten=[1],
            needs_review=[2],
        ),
        now=datetime(2026, 5, 5, tzinfo=timezone.utc),
    )

    assert plan.blocked_reason == "waiting_review"
    assert plan.write_chapters == []


def test_planner_blocks_when_generation_task_is_active() -> None:
    plan = ProductionPlanner().plan(
        policy=ProductionPolicy(enabled=True, quota=ProductionQuota(write=2)),
        backlog=ProductionBacklog(
            project_id="project-1",
            planned_unwritten=[1],
            has_active_generation_task=True,
        ),
        now=datetime(2026, 5, 5, tzinfo=timezone.utc),
    )

    assert plan.blocked_reason == "active_generation_task"
    assert plan.write_chapters == []


def test_planner_records_review_candidate_statuses() -> None:
    plan = ProductionPlanner().plan(
        policy=ProductionPolicy(
            enabled=True,
            quota=ProductionQuota(write=0, review=3),
            stop_when_review_pending=False,
        ),
        backlog=ProductionBacklog(
            project_id="project-1",
            needs_review=[2],
            drafted_unreviewed=[3, 4],
            chapter_plan_count=4,
        ),
        now=datetime(2026, 5, 5, tzinfo=timezone.utc),
    )

    assert plan.review_chapters == [2, 3, 4]
    assert plan.review_chapter_statuses == {
        2: "needs_review",
        3: "drafted",
        4: "drafted",
    }


def test_planner_prioritizes_planned_chapters_before_failed_chapters() -> None:
    plan = ProductionPlanner().plan(
        policy=ProductionPolicy(enabled=True, quota=ProductionQuota(write=3)),
        backlog=ProductionBacklog(
            project_id="project-1",
            planned_unwritten=[2, 4],
            failed=[1, 3],
            chapter_plan_count=4,
        ),
        now=datetime(2026, 5, 5, tzinfo=timezone.utc),
    )

    assert plan.generation_mode == "continue"
    assert plan.requested_chapters == 3
    assert plan.write_chapters == [2, 4, 1]


def test_planner_marks_initial_generation_when_no_chapter_plans_exist() -> None:
    plan = ProductionPlanner().plan(
        policy=ProductionPolicy(enabled=True, quota=ProductionQuota(write=2)),
        backlog=ProductionBacklog(project_id="project-1"),
        now=datetime(2026, 5, 5, tzinfo=timezone.utc),
    )

    assert plan.generation_mode == "initial"
    assert plan.requested_chapters == 2
    assert plan.write_chapters == [1, 2]


def test_planner_publishes_only_with_auto_publish_and_binding() -> None:
    plan = ProductionPlanner().plan(
        policy=ProductionPolicy(
            enabled=True,
            auto_publish=True,
            quota=ProductionQuota(write=0, publish=2),
            publish_bindings=[{"platform": "fanqie", "book_name": "番茄版"}],
        ),
        backlog=ProductionBacklog(
            project_id="project-1",
            reviewed_unpublished=[5, 6, 7],
            scheduled_publish_jobs=[
                ProductionPublishJob(
                    job_id="job-qidian-5",
                    idempotency_key="key-qidian-5",
                    canon_commit_id="canon-5",
                    candidate_id="candidate-5",
                    chapter_number=5,
                    platform="fanqie",
                ),
                ProductionPublishJob(
                    job_id="job-qidian-6",
                    idempotency_key="key-qidian-6",
                    canon_commit_id="canon-6",
                    candidate_id="candidate-6",
                    chapter_number=6,
                    platform="fanqie",
                ),
            ],
        ),
        now=datetime(2026, 5, 5, tzinfo=timezone.utc),
    )

    assert plan.publish_chapters == [5, 6]
    assert [job["job_id"] for job in plan.publish_jobs] == [
        "job-qidian-5",
        "job-qidian-6",
    ]
    assert plan.generation_mode == ""


def test_planner_releases_only_snapshotted_jobs_for_current_platform_policy() -> None:
    plan = ProductionPlanner().plan(
        policy=ProductionPolicy(
            enabled=True,
            auto_publish=True,
            quota=ProductionQuota(write=0, publish=1),
            publish_bindings=[{"platform": "qidian", "book_name": "当前书名"}],
        ),
        backlog=ProductionBacklog(
            project_id="project-1",
            reviewed_unpublished=[8],
            scheduled_publish_jobs=[
                ProductionPublishJob(
                    job_id="job-qidian-8",
                    idempotency_key="key-qidian-8",
                    canon_commit_id="canon-8",
                    candidate_id="candidate-8",
                    chapter_number=8,
                    platform="qidian",
                ),
                ProductionPublishJob(
                    job_id="job-fanqie-8",
                    idempotency_key="key-fanqie-8",
                    canon_commit_id="canon-8",
                    candidate_id="candidate-8",
                    chapter_number=8,
                    platform="fanqie",
                ),
            ],
        ),
        now=datetime(2026, 5, 5, tzinfo=timezone.utc),
    )

    assert plan.publish_chapters == [8]
    assert [job["job_id"] for job in plan.publish_jobs] == ["job-qidian-8"]
