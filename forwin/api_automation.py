from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from fastapi import HTTPException

from forwin.api_schema import ProjectAutomationSettings
from forwin.production.scheduler import ProductionScheduler


def run_automation_scheduler_pass(
    *,
    session_factory,
    config,
    generation_application,
    utcnow: Callable[[], datetime],
    display_tz,
    display_datetime: Callable[[datetime | None], str],
    get_session: Callable[[], Any],
    persist_project_automation: Callable[..., ProjectAutomationSettings],
    terminal_statuses: set[str],
    review_chapter: Callable[[str, int], Any] | None = None,
    approve_chapter_review: Callable[[str, int], Any] | None = None,
    production_scheduler_factory: Any = None,
) -> None:
    if session_factory is None or config is None:
        return
    try:
        scheduler_kwargs = dict(
            display_datetime=display_datetime,
            persist_project_automation=persist_project_automation,
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
                generation_application=generation_application,
                **scheduler_kwargs,
            )
        )
        scheduler.run_due_projects(now=utcnow())
    except HTTPException:
        return
