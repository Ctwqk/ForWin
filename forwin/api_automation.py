from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from fastapi import HTTPException
from sqlalchemy import case, func, select

from forwin.api_schemas import ProjectAutomationSettings
from forwin.models.project import ChapterPlan
from forwin.models.task import GenerationTask
from forwin.production.scheduler import ProductionScheduler, daily_start_minutes


def automation_daily_start_minutes(automation: ProjectAutomationSettings) -> int:
    return daily_start_minutes(automation.daily_start_time)


def load_automation_scheduler_metrics(
    session,
    project_ids: list[str],
    *,
    terminal_statuses: set[str],
) -> tuple[dict[str, int], dict[str, int], dict[str, list[int]], set[str]]:
    normalized_project_ids = [
        str(project_id or "").strip()
        for project_id in project_ids
        if str(project_id or "").strip()
    ]
    pending_review_counts = {project_id: 0 for project_id in normalized_project_ids}
    total_plan_counts = {project_id: 0 for project_id in normalized_project_ids}
    pending_numbers_by_project: dict[str, list[int]] = {
        project_id: []
        for project_id in normalized_project_ids
    }
    if not normalized_project_ids:
        return pending_review_counts, total_plan_counts, pending_numbers_by_project, set()

    plan_count_rows = session.execute(
        select(
            ChapterPlan.project_id,
            func.count(ChapterPlan.id),
            func.sum(
                case(
                    (ChapterPlan.status == "needs_review", 1),
                    else_=0,
                )
            ),
        )
        .where(ChapterPlan.project_id.in_(normalized_project_ids))
        .group_by(ChapterPlan.project_id)
    ).all()
    for project_id, total_count, pending_review_count in plan_count_rows:
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id:
            continue
        total_plan_counts[normalized_project_id] = int(total_count or 0)
        pending_review_counts[normalized_project_id] = int(pending_review_count or 0)

    pending_number_rows = session.execute(
        select(ChapterPlan.project_id, ChapterPlan.chapter_number)
        .where(
            ChapterPlan.project_id.in_(normalized_project_ids),
            ChapterPlan.status.in_(["planned", "failed"]),
        )
        .order_by(ChapterPlan.project_id.asc(), ChapterPlan.chapter_number.asc())
    ).all()
    for project_id, chapter_number in pending_number_rows:
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id:
            continue
        try:
            pending_numbers_by_project.setdefault(normalized_project_id, []).append(int(chapter_number))
        except (TypeError, ValueError):
            continue

    active_generation_project_ids = {
        str(project_id or "").strip()
        for project_id in session.execute(
            select(GenerationTask.project_id)
            .where(
                GenerationTask.deleted_at.is_(None),
                GenerationTask.project_id.in_(normalized_project_ids),
                GenerationTask.status.notin_(tuple(terminal_statuses)),
            )
            .distinct()
        ).scalars().all()
        if str(project_id or "").strip()
    }
    return (
        pending_review_counts,
        total_plan_counts,
        pending_numbers_by_project,
        active_generation_project_ids,
    )


def run_automation_scheduler_pass(
    *,
    session_factory,
    config,
    saved_runtime_config_or_503: Callable[[], Any],
    utcnow: Callable[[], datetime],
    display_tz,
    display_datetime: Callable[[datetime | None], str],
    get_session: Callable[[], Any],
    persist_project_automation: Callable[..., ProjectAutomationSettings],
    create_generation_task: Callable[..., str],
    create_continue_generation_task: Callable[..., str],
    active_generation_task_error_cls: type[Exception],
    terminal_statuses: set[str],
    review_chapter: Callable[[str, int], Any] | None = None,
    approve_chapter_review: Callable[[str, int], Any] | None = None,
    production_scheduler_factory: Any = None,
) -> None:
    if session_factory is None or config is None:
        return
    try:
        scheduler_kwargs = dict(
            runtime_config_provider=saved_runtime_config_or_503,
            display_datetime=display_datetime,
            persist_project_automation=persist_project_automation,
            create_generation_task=create_generation_task,
            create_continue_generation_task=create_continue_generation_task,
            active_generation_task_error_cls=active_generation_task_error_cls,
            generation_terminal_statuses=terminal_statuses,
            upload_terminal_statuses={"succeeded", "failed", "cancelled"},
            display_tz=display_tz,
            get_session=get_session,
            review_chapter=review_chapter,
            approve_chapter_review=approve_chapter_review,
        )
        scheduler = (
            production_scheduler_factory.build(**scheduler_kwargs)
            if production_scheduler_factory is not None
            else ProductionScheduler(
                session_factory=session_factory,
                config=config,
                **scheduler_kwargs,
            )
        )
        scheduler.run_due_projects(now=utcnow())
    except HTTPException:
        return
