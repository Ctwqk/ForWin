from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from fastapi import HTTPException
from sqlalchemy import case, func, select

from forwin.api_project_payloads import normalize_project_automation
from forwin.api_schemas import ProjectAutomationSettings
from forwin.models.project import ChapterPlan, Project
from forwin.models.task import GenerationTask


def automation_daily_start_minutes(automation: ProjectAutomationSettings) -> int:
    try:
        hour_text, minute_text = automation.daily_start_time.split(":", 1)
        return int(hour_text) * 60 + int(minute_text)
    except (TypeError, ValueError):
        return 9 * 60


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
) -> None:
    if session_factory is None or config is None:
        return
    try:
        runtime_config = saved_runtime_config_or_503()
    except HTTPException:
        return

    now = utcnow()
    now_local = now.astimezone(display_tz)
    today = now_local.strftime("%Y-%m-%d")
    current_minutes = now_local.hour * 60 + now_local.minute

    session = get_session()
    try:
        ready_projects: list[tuple[Project, ProjectAutomationSettings]] = []
        projects = session.execute(select(Project).order_by(Project.updated_at.desc())).scalars().all()
        for project in projects:
            automation = normalize_project_automation(project.automation_json)
            if not automation.enabled:
                continue
            if automation.last_scheduler_date == today:
                continue
            if current_minutes < automation_daily_start_minutes(automation):
                continue
            ready_projects.append((project, automation))

        (
            pending_review_counts,
            total_plan_counts,
            pending_numbers_by_project,
            active_generation_project_ids,
        ) = load_automation_scheduler_metrics(
            session,
            [project.id for project, _automation in ready_projects],
            terminal_statuses=terminal_statuses,
        )

        for project, automation in ready_projects:
            pending_review = int(pending_review_counts.get(project.id, 0) or 0)
            total_plans = int(total_plan_counts.get(project.id, 0) or 0)
            pending_numbers = list(pending_numbers_by_project.get(project.id, []))

            updated = automation.model_copy(
                update={
                    "last_scheduler_date": today,
                    "last_scheduler_at": display_datetime(now),
                }
            )
            if project.id in active_generation_project_ids:
                updated = updated.model_copy(
                    update={
                        "last_scheduler_action": "active_task",
                        "last_scheduler_message": "已有运行中的生成任务，今日不重复调度。",
                        "last_scheduler_task_id": "",
                    }
                )
                persist_project_automation(session, project, updated)
                continue
            if pending_review:
                updated = updated.model_copy(
                    update={
                        "last_scheduler_action": "waiting_review",
                        "last_scheduler_message": "仍有章节等待人工 review，今日暂停自动生成。",
                        "last_scheduler_task_id": "",
                    }
                )
                persist_project_automation(session, project, updated)
                continue

            quota = min(20, max(1, int(automation.daily_chapter_quota or 1)))
            task_id = ""
            if total_plans == 0:
                try:
                    task_id = create_generation_task(
                        premise=project.premise,
                        genre=project.genre,
                        num_chapters=quota,
                        runtime_config=runtime_config,
                        project_id=project.id,
                        title=project.title,
                        subtitle=f"自动调度 · 首批 {quota} 章",
                    )
                    updated = updated.model_copy(
                        update={
                            "last_scheduler_action": "started_initial_generation",
                            "last_scheduler_message": f"已按计划启动首批 {quota} 章生成。",
                            "last_scheduler_task_id": task_id,
                        }
                    )
                except active_generation_task_error_cls:
                    updated = updated.model_copy(
                        update={
                            "last_scheduler_action": "active_task",
                            "last_scheduler_message": "已有运行中的生成任务，今日不重复调度。",
                            "last_scheduler_task_id": "",
                        }
                    )
            elif pending_numbers:
                try:
                    task_id = create_continue_generation_task(
                        project_id=project.id,
                        runtime_config=runtime_config,
                        requested_chapters=total_plans,
                        max_chapters=quota,
                        title=project.title,
                        subtitle=f"自动调度 · 今日上限 {quota} 章",
                        message=f"按计划继续生成，今日最多处理 {quota} 章。",
                    )
                    updated = updated.model_copy(
                        update={
                            "last_scheduler_action": "started_continue_generation",
                            "last_scheduler_message": f"已按计划继续生成，今日最多处理 {quota} 章。",
                            "last_scheduler_task_id": task_id,
                        }
                    )
                except active_generation_task_error_cls:
                    updated = updated.model_copy(
                        update={
                            "last_scheduler_action": "active_task",
                            "last_scheduler_message": "已有运行中的生成任务，今日不重复调度。",
                            "last_scheduler_task_id": "",
                        }
                    )
            else:
                updated = updated.model_copy(
                    update={
                        "last_scheduler_action": "idle",
                        "last_scheduler_message": "没有待生成章节，今日无需调度。",
                        "last_scheduler_task_id": "",
                    }
                )

            persist_project_automation(session, project, updated)
        session.commit()
    except Exception:  # noqa: BLE001
        session.rollback()
        raise
    finally:
        session.close()
