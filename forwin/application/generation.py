from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError

from forwin.application.errors import (
    ActiveGenerationTaskError,
    PermanentConfigurationError,
    ProjectNotFound,
)
from forwin.config import InfrastructureConfig
from forwin.generation.task_payload import (
    GenerationExecutionContext,
    build_execution_context,
    execution_payload,
    payload_from_json,
)
from forwin.generation.task_repository import (
    GenerationTaskRepository,
    new_task_id,
)
from forwin.audit.events import (
    DecisionEventInfo,
    DecisionEventType,
    ensure_decision_event_type,
)
from forwin.models.project import Project
from forwin.models.task import GenerationTask
from forwin.runtime.policy_store import ProjectPolicyStore
from forwin.state.updater import StateUpdater


@dataclass(frozen=True, slots=True)
class EnqueueGenerationCommand:
    project_id: str
    requested_chapters: int
    max_chapters: int
    run_until_chapter: int
    auto_continue: bool
    title: str
    subtitle: str
    root_event_type: str
    message: str = ""


@dataclass(frozen=True, slots=True)
class GenerationTaskHandle:
    task_id: str
    project_id: str


GenerationRunner = Callable[
    [GenerationTask, GenerationExecutionContext, int, str],
    None,
]


class GenerationApplicationService:
    def __init__(
        self,
        *,
        session_factory,
        infrastructure: InfrastructureConfig,
        runner: GenerationRunner | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.infrastructure = infrastructure
        self.runner = runner or self._run_claimed

    def enqueue(self, command: EnqueueGenerationCommand) -> GenerationTaskHandle:
        project_id = str(command.project_id or "").strip()
        if not project_id:
            raise ProjectNotFound(project_id)
        requested_chapters = max(1, int(command.requested_chapters or 0))
        max_chapters = max(0, int(command.max_chapters or 0))
        run_until_chapter = max(0, int(command.run_until_chapter or 0))
        task_id = new_task_id()
        try:
            with self.session_factory.begin() as session:
                project = session.get(Project, project_id)
                if project is None:
                    raise ProjectNotFound(project_id)
                repository = GenerationTaskRepository(session)
                if repository.has_active(project_id):
                    raise ActiveGenerationTaskError(
                        f"active generation task exists for project: {project_id}"
                    )
                policy_record = ProjectPolicyStore(session).load(project)
                root_event = StateUpdater(session).save_decision_event(
                    DecisionEventInfo(
                        project_id=project_id,
                        task_id=task_id,
                        scope="task",
                        event_family="business_event",
                        event_type=ensure_decision_event_type(
                            str(command.root_event_type or "")
                        ),
                        actor_type="api",
                        summary="生成任务已创建。",
                        payload={
                            "requested_chapters": requested_chapters,
                            "max_chapters": max_chapters,
                            "run_until_chapter": run_until_chapter,
                        },
                        related_object_type="generation_task",
                        related_object_id=task_id,
                    )
                )
                payload = execution_payload(
                    mode="continue",
                    policy=policy_record.policy,
                    policy_version=policy_record.version,
                    root_event_id=root_event.id,
                    auto_continue=command.auto_continue,
                    run_until_chapter=run_until_chapter,
                    max_chapters=max_chapters,
                )
                task = repository.create(
                    task_id=task_id,
                    project_id=project_id,
                    title=str(command.title or ""),
                    subtitle=str(command.subtitle or ""),
                    message=str(command.message or ""),
                    requested_chapters=requested_chapters,
                    max_chapters=max_chapters,
                    run_until_chapter=run_until_chapter,
                    payload=payload,
                )
                return GenerationTaskHandle(
                    task_id=task.id,
                    project_id=project_id,
                )
        except IntegrityError as exc:
            if "ux_generation_tasks_one_active_per_project" in str(exc):
                raise ActiveGenerationTaskError(
                    f"active generation task exists for project: {project_id}"
                ) from exc
            raise

    def execute_claimed(
        self,
        task: GenerationTask,
        *,
        resume_from_chapter: int,
        worker_id: str,
    ) -> None:
        payload = payload_from_json(task.execution_payload_json)
        context = build_execution_context(
            self.infrastructure,
            payload,
            task_id=task.id,
        )
        self.runner(
            task,
            context,
            max(0, int(resume_from_chapter or 0)),
            str(worker_id or ""),
        )

    def _run_claimed(
        self,
        task: GenerationTask,
        context: GenerationExecutionContext,
        resume_from_chapter: int,
        _worker_id: str,
    ) -> None:
        from forwin.application.generation_execution import execute_continuation

        project_id = str(task.project_id or "").strip()
        payload = payload_from_json(task.execution_payload_json)
        if not project_id or payload.mode != "continue":
            raise PermanentConfigurationError(
                "v5 generation executes project-backed continuation tasks only"
            )
        execute_continuation(
            context,
            project_id,
            self._task_updater(),
            logging.getLogger(__name__),
            should_abort=self._task_flag(task.id, "cancel_requested"),
            should_pause=self._task_flag(task.id, "pause_requested"),
            max_chapters=int(task.max_chapters or payload.max_chapters or 0) or None,
            resume_from_chapter=resume_from_chapter,
            completion_handler=self._completion_handler(task.id, payload),
            component="worker",
        )

    def _task_updater(self):
        def update(task_id: str, **changes: object) -> None:
            with self.session_factory.begin() as session:
                GenerationTaskRepository(session).update(task_id, changes)

        return update

    def _task_flag(self, task_id: str, attribute: str):
        def read() -> bool:
            with self.session_factory() as session:
                task = session.get(GenerationTask, task_id)
                return (
                    bool(getattr(task, attribute, False)) if task is not None else True
                )

        return read

    def _completion_handler(self, task_id: str, payload):
        def handle(result: object) -> None:
            if not payload.auto_continue:
                return
            from forwin.generation.auto_continue import GenerationAutoContinueController

            GenerationAutoContinueController(
                session_factory=self.session_factory,
                create_continue_generation_task=self._enqueue_continue,
            ).after_task_completion(
                result,
                parent_task_id=task_id,
                run_until_chapter=int(payload.run_until_chapter or 0) or None,
                max_chapters=int(payload.max_chapters or 0) or None,
                auto_continue=True,
            )

        return handle

    def _enqueue_continue(self, **values: object) -> str:
        handle = self.enqueue(
            EnqueueGenerationCommand(
                project_id=str(values.get("project_id") or ""),
                requested_chapters=int(values.get("requested_chapters") or 0),
                max_chapters=int(values.get("max_chapters") or 0),
                run_until_chapter=int(values.get("run_until_chapter") or 0),
                auto_continue=bool(values.get("auto_continue", True)),
                title=str(values.get("title") or ""),
                subtitle=str(values.get("subtitle") or ""),
                message=str(values.get("message") or ""),
                root_event_type=DecisionEventType.CONTINUE_REQUESTED,
            )
        )
        return handle.task_id


__all__ = [
    "EnqueueGenerationCommand",
    "GenerationApplicationService",
    "GenerationRunner",
    "GenerationTaskHandle",
]
