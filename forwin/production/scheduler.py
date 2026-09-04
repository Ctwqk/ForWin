from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from hashlib import sha256
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from forwin.application.generation import GenerationApplicationService
from forwin.application.read_models import normalize_project_automation
from forwin.api_schema import ProjectAutomationSettings
from forwin.models.project import Project
from forwin.observability.context import OperationContext
from forwin.observability.ports import NullObservability

from .events import (
    ACTION_ACTIVE_TASK,
    ACTION_BLOCKED,
    ACTION_IDLE,
    ACTION_RAN_REVIEW_JOBS,
    ACTION_WAITING_REVIEW,
    action_for_blocked_reason,
    message_for_action,
)
from .executor import ProductionExecutionResult, ProductionExecutor
from .planner import ProductionPlan, ProductionPlanner
from .policy import policy_from_automation
from .repository import ProductionRepository


class ProductionRunResult(BaseModel):
    project_id: str
    action: str
    message: str = ""
    task_id: str = ""
    plan: ProductionPlan | None = None
    execution: ProductionExecutionResult | None = None


def daily_start_minutes(daily_start_time: str) -> int:
    try:
        hour_text, minute_text = str(daily_start_time or "").split(":", 1)
        return int(hour_text) * 60 + int(minute_text)
    except (TypeError, ValueError):
        return 9 * 60


class ProductionScheduler:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Any] | None,
        config: Any,
        generation_application: GenerationApplicationService,
        display_datetime: Callable[[datetime | None], str],
        persist_project_automation: Callable[..., ProjectAutomationSettings],
        generation_terminal_statuses: set[str],
        upload_terminal_statuses: set[str],
        display_tz: Any = None,
        get_session: Callable[[], Any] | None = None,
        publisher_manager_factory: Callable[[], Any] | None = None,
        review_chapter: Callable[[str, int], Any] | None = None,
        approve_chapter_review: Callable[[str, int], Any] | None = None,
        observability: Any | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.config = config
        self.generation_application = generation_application
        self.display_datetime = display_datetime
        self.persist_project_automation = persist_project_automation
        self.generation_terminal_statuses = generation_terminal_statuses
        self.upload_terminal_statuses = upload_terminal_statuses
        self.display_tz = display_tz
        self.get_session = get_session
        self.publisher_manager_factory = publisher_manager_factory
        self.review_chapter = review_chapter
        self.approve_chapter_review = approve_chapter_review
        self.planner = ProductionPlanner()
        self.observability = observability or NullObservability()

    def run_due_projects(self, *, now: datetime) -> list[ProductionRunResult]:
        if self.session_factory is None or self.config is None:
            return []
        now_local = (
            now.astimezone(self.display_tz) if self.display_tz is not None else now
        )
        with self._session() as session:
            project_ids = list(
                session.scalars(select(Project.id).order_by(Project.updated_at.desc()))
            )
        results: list[ProductionRunResult] = []
        for project_id in project_ids:
            # Every project commits independently; later failures cannot undo a dispatch.
            with self._session() as session, session.begin():
                lock_key = int.from_bytes(
                    sha256(f"forwin:production:{project_id}".encode()).digest()[:8],
                    "big",
                    signed=True,
                )
                if not session.scalar(
                    text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": lock_key}
                ):
                    continue
                result = self._run_project(session, project_id, now, now_local)
                if result is not None:
                    results.append(result)
        return results

    @staticmethod
    def _is_due(automation: ProjectAutomationSettings, now_local: datetime) -> bool:
        if not automation.enabled:
            return False
        if now_local.hour * 60 + now_local.minute < daily_start_minutes(
            automation.daily_start_time
        ):
            return False
        # Older schedulers also stamped blocked/idle checks as a completed day.
        retryable_actions = {
            ACTION_ACTIVE_TASK,
            ACTION_WAITING_REVIEW,
            ACTION_IDLE,
            ACTION_BLOCKED,
        }
        return (
            automation.last_scheduler_date != now_local.date().isoformat()
            or automation.last_scheduler_action in retryable_actions
        )

    def _plan(
        self,
        session: Session,
        project_id: str,
        automation: ProjectAutomationSettings,
        now_local: datetime,
    ) -> ProductionPlan:
        backlog = ProductionRepository(session).load_backlogs(
            [project_id],
            generation_terminal_statuses=self.generation_terminal_statuses,
            upload_terminal_statuses=self.upload_terminal_statuses,
        )[project_id]
        return self.planner.plan(
            policy=policy_from_automation(automation), backlog=backlog, now=now_local
        )

    def _run_project(
        self,
        session: Session,
        project_id: str,
        now: datetime,
        now_local: datetime,
    ) -> ProductionRunResult | None:
        project = session.get(Project, project_id)
        if project is None:
            return None
        automation = normalize_project_automation(project.automation_json)
        if not self._is_due(automation, now_local):
            return None
        context = OperationContext(
            project_id=project_id, stage="production.scheduler.run_due_projects"
        )
        with self.observability.span(
            context,
            "production.scheduler.project",
            span_kind="scheduler",
            component="production",
        ) as span:
            executor = ProductionExecutor(
                generation_application=self.generation_application,
                publisher_manager_factory=self.publisher_manager_factory,
                session=session,
                session_factory=self.session_factory,
                config=self.config,
                review_chapter=self.review_chapter,
                approve_chapter_review=self.approve_chapter_review,
            )
            plan = self._plan(session, project_id, automation, now_local)
            # Review owns separate Canon transactions. Until callbacks return, this
            # transaction holds only the advisory lock and has made no writes.
            reviewed = (
                executor.execute_review_jobs(plan=plan, project=project)
                if not plan.blocked_reason
                else 0
            )
            session.expire_all()
            project = session.scalar(
                select(Project).where(Project.id == project_id).with_for_update()
            )
            if project is None:
                return None
            automation = normalize_project_automation(project.automation_json)
            if not self._is_due(automation, now_local):
                return None
            plan = self._plan(session, project_id, automation, now_local)
            if plan.blocked_reason:
                action = action_for_blocked_reason(plan.blocked_reason)
                execution = ProductionExecutionResult(
                    action=action,
                    message=message_for_action(
                        action, blocked_reason=plan.blocked_reason
                    ),
                    review_job_count=reviewed,
                )
            else:
                execution = executor.execute(
                    plan=plan,
                    project=project,
                    policy=policy_from_automation(automation),
                    review_job_count=reviewed,
                )
            if reviewed and not execution.task_id and not execution.publish_job_count:
                execution = execution.model_copy(
                    update={
                        "action": ACTION_RAN_REVIEW_JOBS,
                        "message": message_for_action(
                            ACTION_RAN_REVIEW_JOBS, chapter_count=reviewed
                        ),
                        "review_job_count": reviewed,
                    }
                )
            reserved = bool(
                execution.task_id or execution.publish_job_count or reviewed
            )
            updated = automation.model_copy(
                update={
                    "last_scheduler_date": now_local.date().isoformat()
                    if reserved
                    else "",
                    "last_scheduler_at": self.display_datetime(now),
                    "last_scheduler_action": execution.action,
                    "last_scheduler_message": execution.message,
                    "last_scheduler_task_id": execution.task_id,
                }
            )
            self.persist_project_automation(session, project, updated)
            span.tag("action", execution.action)
            return ProductionRunResult(
                project_id=project_id,
                action=execution.action,
                message=execution.message,
                task_id=execution.task_id,
                plan=plan,
                execution=execution,
            )

    def _session(self) -> Any:
        if self.get_session is not None:
            return self.get_session()
        if self.session_factory is None:
            raise RuntimeError("ProductionScheduler requires a session factory.")
        return self.session_factory()
