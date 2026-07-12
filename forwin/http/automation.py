"""ForWin Web API – FastAPI interface for the novel generation system."""

from __future__ import annotations

import logging
import threading
from typing import Any

from forwin import (
    api_automation,
)
from forwin.api_schema import (
    ChapterReviewApproveRequest,
    ProjectAutomationSettings,
)
import forwin.models.phase  # noqa: F401
from forwin.application.generation import GenerationApplicationService
from forwin.http.request_support import (
    _display_datetime,
    _get_session,
    _utcnow,
)
from forwin.http.project_support import (
    _persist_project_automation,
)
from forwin.http.runtime import GENERATION_TERMINAL_STATUSES, HttpRuntime
from forwin.http.tasks import _get_task_center_service, _prune_tasks


logger = logging.getLogger(__name__)


def _automation_daily_start_minutes(automation: ProjectAutomationSettings) -> int:
    return api_automation.automation_daily_start_minutes(automation)


def _load_automation_scheduler_metrics(
    runtime: HttpRuntime,
    session,
    project_ids: list[str],
) -> tuple[dict[str, int], dict[str, int], dict[str, list[int]], set[str]]:
    return api_automation.load_automation_scheduler_metrics(
        session,
        project_ids,
        terminal_statuses=GENERATION_TERMINAL_STATUSES,
    )


def _run_scheduled_review_action(
    runtime: HttpRuntime,
    project_id: str,
    chapter_number: int,
) -> Any:
    if runtime.project_application is None:
        return None
    return runtime.project_application.approve_chapter_review(
        project_id,
        int(chapter_number),
        ChapterReviewApproveRequest(reason="production_scheduler_review_quota"),
    )


def _run_automation_scheduler_pass(runtime: HttpRuntime) -> None:
    _prune_tasks(runtime)
    production_scheduler_factory = None
    runtime_services = None
    if runtime.container is not None:
        runtime_services = runtime.container.services()
        production_scheduler_factory = runtime_services.production_scheduler
        generation_application = runtime_services.generation_application
    else:
        generation_application = GenerationApplicationService(
            session_factory=runtime.session_factory,
            infrastructure=runtime.config,
        )
    result = api_automation.run_automation_scheduler_pass(
        session_factory=runtime.session_factory,
        config=runtime.config,
        generation_application=generation_application,
        utcnow=_utcnow,
        display_tz=runtime.display_timezone,
        display_datetime=lambda value: _display_datetime(
            value,
            display_timezone=runtime.display_timezone,
        ),
        get_session=lambda: _get_session(runtime),
        persist_project_automation=_persist_project_automation,
        terminal_statuses=GENERATION_TERMINAL_STATUSES,
        review_chapter=lambda project_id, chapter_number: _run_scheduled_review_action(
            runtime, project_id, chapter_number
        ),
        approve_chapter_review=lambda project_id, chapter_number: (
            _run_scheduled_review_action(runtime, project_id, chapter_number)
        ),
        production_scheduler_factory=production_scheduler_factory,
    )
    if runtime_services is not None:
        try:
            runtime_services.publisher_runtime.backend_jobs.run_pending_once(limit=1)
        except Exception:  # noqa: BLE001
            logger.exception("Publisher backend job pass failed.")
    return result


def _automation_scheduler_loop(runtime: HttpRuntime) -> None:
    while not runtime.automation_stop.wait(30.0):
        try:
            _run_automation_scheduler_pass(runtime)
        except Exception:  # noqa: BLE001
            logger.exception("Automation scheduler loop failed.")


def start_automation_scheduler(runtime: HttpRuntime) -> None:
    if (
        runtime.automation_thread is not None
        and runtime.automation_thread.is_alive()
    ):
        return
    runtime.automation_stop.clear()
    runtime.automation_thread = threading.Thread(
        target=_automation_scheduler_loop,
        args=(runtime,),
        name="forwin-automation-scheduler",
        daemon=True,
    )
    runtime.automation_thread.start()


def stop_automation_scheduler(runtime: HttpRuntime) -> None:
    runtime.automation_stop.set()
    thread = runtime.automation_thread
    runtime.automation_thread = None
    if thread is not None and thread.is_alive():
        thread.join(timeout=2.0)


def _list_generation_tasks(
    runtime: HttpRuntime,
    limit: int,
) -> list[tuple[str, dict[str, Any]]]:
    return _get_task_center_service(runtime).list_generation_tasks(limit)


__all__ = [name for name in globals() if not name.startswith("__")]
