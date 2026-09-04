from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from forwin.application.errors import ActiveGenerationTaskError
from forwin.application.generation import (
    EnqueueGenerationCommand,
    GenerationApplicationService,
)
from forwin.audit.events import DecisionEventType
from forwin.models.project import Project

from .events import (
    ACTION_ACTIVE_TASK,
    ACTION_IDLE,
    ACTION_RAN_REVIEW_JOBS,
    ACTION_STARTED_CONTINUE_GENERATION,
    ACTION_STARTED_INITIAL_GENERATION,
    ACTION_STARTED_PUBLISH_JOBS,
    message_for_action,
)
from .planner import ProductionPlan
from .policy import ProductionPolicy


class ProductionExecutionResult(BaseModel):
    action: str = ACTION_IDLE
    message: str = ""
    task_id: str = ""
    review_job_count: int = 0
    publish_job_count: int = 0


class ProductionExecutor:
    def __init__(
        self,
        *,
        generation_application: GenerationApplicationService,
        publisher_manager_factory: Callable[[], Any] | None = None,
        session: Any | None = None,
        session_factory: Callable[[], Any] | None = None,
        config: Any = None,
        review_chapter: Callable[[str, int], Any] | None = None,
        approve_chapter_review: Callable[[str, int], Any] | None = None,
    ) -> None:
        self.generation_application = generation_application
        self.publisher_manager_factory = publisher_manager_factory
        self.session = session
        self.session_factory = session_factory
        self.config = config
        self.review_chapter = review_chapter
        self.approve_chapter_review = approve_chapter_review

    def execute(
        self,
        *,
        plan: ProductionPlan,
        project: Project,
        policy: ProductionPolicy,
        review_job_count: int | None = None,
    ) -> ProductionExecutionResult:
        action = ACTION_IDLE
        task_id = ""
        enqueue_options = {"session": self.session} if self.session is not None else {}
        try:
            if plan.generation_mode == "initial" and plan.write_chapters:
                requested = max(
                    1,
                    int(plan.requested_chapters or len(plan.write_chapters)),
                )
                task_id = self.generation_application.enqueue(
                    EnqueueGenerationCommand(
                        project_id=project.id,
                        requested_chapters=requested,
                        max_chapters=requested,
                        run_until_chapter=max(plan.write_chapters),
                        auto_continue=False,
                        title=project.title,
                        subtitle=f"自动调度 · 首批 {len(plan.write_chapters)} 章",
                        message=f"按计划开始首批 {requested} 章。",
                        root_event_type=DecisionEventType.GENERATION_REQUESTED,
                    ),
                    **enqueue_options,
                ).task_id
                action = ACTION_STARTED_INITIAL_GENERATION
            elif plan.generation_mode == "continue" and plan.write_chapters:
                requested = max(
                    1,
                    int(plan.requested_chapters or len(plan.write_chapters)),
                )
                maximum = max(1, int(policy.quota.write or len(plan.write_chapters)))
                task_id = self.generation_application.enqueue(
                    EnqueueGenerationCommand(
                        project_id=project.id,
                        requested_chapters=requested,
                        max_chapters=maximum,
                        run_until_chapter=max(plan.write_chapters),
                        auto_continue=True,
                        title=project.title,
                        subtitle=f"自动调度 · 今日上限 {maximum} 章",
                        message=f"按计划继续生成，今日最多处理 {maximum} 章。",
                        root_event_type=DecisionEventType.CONTINUE_REQUESTED,
                    ),
                    **enqueue_options,
                ).task_id
                action = ACTION_STARTED_CONTINUE_GENERATION
        except ActiveGenerationTaskError:
            return ProductionExecutionResult(
                action=ACTION_ACTIVE_TASK,
                message=message_for_action(ACTION_ACTIVE_TASK),
            )

        if review_job_count is None:
            review_job_count = self.execute_review_jobs(plan=plan, project=project)
        if action == ACTION_IDLE and review_job_count > 0:
            action = ACTION_RAN_REVIEW_JOBS

        publish_job_count = self._enqueue_publish_jobs(
            plan=plan,
            project=project,
            policy=policy,
        )
        if action == ACTION_IDLE and publish_job_count > 0:
            action = ACTION_STARTED_PUBLISH_JOBS
        message_chapter_count = (
            max(1, int(policy.quota.write or len(plan.write_chapters)))
            if action == ACTION_STARTED_CONTINUE_GENERATION
            else len(plan.write_chapters)
        )
        return ProductionExecutionResult(
            action=action,
            message=message_for_action(
                action,
                chapter_count=review_job_count
                if action == ACTION_RAN_REVIEW_JOBS
                else message_chapter_count,
                publish_job_count=publish_job_count,
            ),
            task_id=task_id,
            review_job_count=review_job_count,
            publish_job_count=publish_job_count,
        )

    def execute_review_jobs(
        self,
        *,
        plan: ProductionPlan,
        project: Project,
    ) -> int:
        total = 0
        for chapter_number in plan.review_chapters:
            normalized_chapter = int(chapter_number or 0)
            if normalized_chapter <= 0:
                continue
            status = str(
                plan.review_chapter_statuses.get(normalized_chapter, "") or ""
            ).strip()
            if status == "needs_review":
                callback = self.approve_chapter_review
            else:
                callback = self.review_chapter
            if callback is None:
                continue
            callback(str(project.id), normalized_chapter)
            total += 1
        return total

    def _enqueue_publish_jobs(
        self,
        *,
        plan: ProductionPlan,
        project: Project,
        policy: ProductionPolicy,
    ) -> int:
        if (
            not plan.publish_jobs
            or not policy.auto_publish
            or not policy.publish_bindings
        ):
            return 0
        manager = self._publisher_manager()
        if manager is None:
            return 0
        release_request = {
            "project_id": project.id,
            "job_ids": [str(item.get("job_id") or "") for item in plan.publish_jobs],
            "publish": True,
            "actor_type": "scheduler",
        }
        if self.session is not None:
            release_request["session"] = self.session
        released = manager.release_canon_jobs(
            **release_request,
        )
        return len(released or [])

    def _publisher_manager(self) -> Any:
        if self.publisher_manager_factory is not None:
            return self.publisher_manager_factory()
        if self.session_factory is None or self.config is None:
            return None
        from forwin.publishers.manager import PublisherManager

        return PublisherManager(
            self.session_factory,
            extension_api_key=str(
                getattr(self.config, "publisher_extension_api_key", "") or ""
            ),
            preferred_client_id=str(
                getattr(self.config, "publisher_preferred_client_id", "") or ""
            ),
            strict_preferred_client=bool(
                getattr(self.config, "publisher_strict_preferred_client", False)
            ),
            publisher_session_secret=str(
                getattr(self.config, "publisher_session_secret", "") or ""
            ),
            publisher_session_encryption_required=bool(
                getattr(self.config, "publisher_session_encryption_required", False)
            ),
            publisher_login_discord_webhook_url=str(
                getattr(self.config, "publisher_login_discord_webhook_url", "") or ""
            ),
        )
