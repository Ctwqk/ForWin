from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select

from forwin.api_project_payloads import normalize_project_automation
from forwin.api_schemas import ProjectAutomationSettings
from forwin.models.project import Project

from .events import action_for_blocked_reason, message_for_action
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
        runtime_config_provider: Callable[[], Any],
        display_datetime: Callable[[datetime | None], str],
        persist_project_automation: Callable[..., ProjectAutomationSettings],
        create_generation_task: Callable[..., str],
        create_continue_generation_task: Callable[..., str],
        active_generation_task_error_cls: type[Exception],
        generation_terminal_statuses: set[str],
        upload_terminal_statuses: set[str],
        display_tz: Any = None,
        get_session: Callable[[], Any] | None = None,
        publisher_manager_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.config = config
        self.runtime_config_provider = runtime_config_provider
        self.display_datetime = display_datetime
        self.persist_project_automation = persist_project_automation
        self.create_generation_task = create_generation_task
        self.create_continue_generation_task = create_continue_generation_task
        self.active_generation_task_error_cls = active_generation_task_error_cls
        self.generation_terminal_statuses = generation_terminal_statuses
        self.upload_terminal_statuses = upload_terminal_statuses
        self.display_tz = display_tz
        self.get_session = get_session
        self.publisher_manager_factory = publisher_manager_factory
        self.planner = ProductionPlanner()

    def run_due_projects(self, *, now: datetime) -> list[ProductionRunResult]:
        if self.session_factory is None or self.config is None:
            return []
        runtime_config = self.runtime_config_provider()
        now_local = now.astimezone(self.display_tz) if self.display_tz is not None else now
        today = now_local.strftime("%Y-%m-%d")
        current_minutes = now_local.hour * 60 + now_local.minute
        session = self._session()
        try:
            ready_projects: list[tuple[Project, ProjectAutomationSettings]] = []
            projects = session.execute(select(Project).order_by(Project.updated_at.desc())).scalars().all()
            for project in projects:
                automation = normalize_project_automation(project.automation_json)
                if not automation.enabled:
                    continue
                if automation.last_scheduler_date == today:
                    continue
                if current_minutes < daily_start_minutes(automation.daily_start_time):
                    continue
                ready_projects.append((project, automation))

            backlogs = ProductionRepository(session).load_backlogs(
                [project.id for project, _automation in ready_projects],
                generation_terminal_statuses=self.generation_terminal_statuses,
                upload_terminal_statuses=self.upload_terminal_statuses,
            )
            executor = ProductionExecutor(
                create_generation_task=self.create_generation_task,
                create_continue_generation_task=self.create_continue_generation_task,
                active_generation_task_error_cls=self.active_generation_task_error_cls,
                publisher_manager_factory=self.publisher_manager_factory,
                session_factory=self.session_factory,
                config=self.config,
            )
            results: list[ProductionRunResult] = []
            for project, automation in ready_projects:
                policy = policy_from_automation(automation)
                backlog = backlogs.get(project.id)
                if backlog is None:
                    continue
                plan = self.planner.plan(policy=policy, backlog=backlog, now=now_local)
                updated = automation.model_copy(
                    update={
                        "last_scheduler_date": today,
                        "last_scheduler_at": self.display_datetime(now),
                    }
                )
                if plan.blocked_reason:
                    action = action_for_blocked_reason(plan.blocked_reason)
                    message = message_for_action(action, blocked_reason=plan.blocked_reason)
                    updated = updated.model_copy(
                        update={
                            "last_scheduler_action": action,
                            "last_scheduler_message": message,
                            "last_scheduler_task_id": "",
                        }
                    )
                    self.persist_project_automation(session, project, updated)
                    results.append(
                        ProductionRunResult(
                            project_id=project.id,
                            action=action,
                            message=message,
                            plan=plan,
                        )
                    )
                    continue

                execution = executor.execute(
                    plan=plan,
                    project=project,
                    policy=policy,
                    runtime_config=runtime_config,
                )
                updated = updated.model_copy(
                    update={
                        "last_scheduler_action": execution.action,
                        "last_scheduler_message": execution.message,
                        "last_scheduler_task_id": execution.task_id,
                    }
                )
                self.persist_project_automation(session, project, updated)
                results.append(
                    ProductionRunResult(
                        project_id=project.id,
                        action=execution.action,
                        message=execution.message,
                        task_id=execution.task_id,
                        plan=plan,
                        execution=execution,
                    )
                )
            session.commit()
            return results
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _session(self) -> Any:
        if self.get_session is not None:
            return self.get_session()
        if self.session_factory is None:
            raise RuntimeError("ProductionScheduler requires a session factory.")
        return self.session_factory()
