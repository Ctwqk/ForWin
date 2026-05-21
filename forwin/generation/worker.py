from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from forwin.config import Config
from forwin.generation.task_lease import (
    claim_generation_task,
    generation_task_resume_from_chapter,
    heartbeat_generation_task,
)
from forwin.models.task import GenerationTask


logger = logging.getLogger(__name__)


ExecuteGenerationTask = Callable[[GenerationTask, int], None]


class GenerationWorkerResult(BaseModel):
    claimed: bool = False
    task_id: str = ""
    project_id: str = ""
    resume_from_chapter: int = 0
    executed: bool = False
    message: str = ""


def run_one_generation_task(
    *,
    session_factory: Callable[[], Any],
    worker_id: str,
    config: Config | None = None,
    lease_seconds: int = 300,
    execute_continue: ExecuteGenerationTask | None = None,
    execute_new: ExecuteGenerationTask | None = None,
) -> GenerationWorkerResult:
    with session_factory.begin() as session:
        task = claim_generation_task(
            session,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        if task is None:
            return GenerationWorkerResult(message="no_claimable_generation_task")
        task_id = task.id
        project_id = str(task.project_id or "")
        resume_from_chapter = generation_task_resume_from_chapter(task)

    try:
        if project_id:
            executor = execute_continue or _default_continue_executor(
                session_factory=session_factory,
                config=config or Config.from_env(),
                lease_seconds=lease_seconds,
                worker_id=worker_id,
            )
        else:
            executor = execute_new or _default_new_executor(
                session_factory=session_factory,
                config=config or Config.from_env(),
                lease_seconds=lease_seconds,
                worker_id=worker_id,
            )
        executor(task, resume_from_chapter)
    except Exception:
        logger.exception("Generation worker failed task %s", task_id)
        with session_factory.begin() as session:
            row = session.get(GenerationTask, task_id)
            if row is not None and row.lease_owner == worker_id:
                row.status = "failed"
                row.current_stage = "failed"
                row.error_message = "generation_worker_execution_failed"
                session.add(row)
        raise

    with session_factory.begin() as session:
        heartbeat_generation_task(
            session,
            task_id=task_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )

    return GenerationWorkerResult(
        claimed=True,
        task_id=task_id,
        project_id=project_id,
        resume_from_chapter=resume_from_chapter,
        executed=True,
        message="executed",
    )


def _default_continue_executor(
    *,
    session_factory: Callable[[], Any],
    config: Config,
    lease_seconds: int,
    worker_id: str,
) -> ExecuteGenerationTask:
    from forwin.api_runtime import run_continue_project_with_config

    def _execute(task: GenerationTask, resume_from_chapter: int) -> None:
        _ = resume_from_chapter
        update_task = _db_task_updater(session_factory)
        run_continue_project_with_config(
            task.id,
            str(task.project_id or ""),
            config,
            update_task,
            logger,
            max_chapters=int(task.max_chapters or 0) or None,
        )
        with session_factory.begin() as session:
            heartbeat_generation_task(
                session,
                task_id=task.id,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            )

    return _execute


def _default_new_executor(
    *,
    session_factory: Callable[[], Any],
    config: Config,
    lease_seconds: int,
    worker_id: str,
) -> ExecuteGenerationTask:
    from forwin.api_runtime import run_generation_with_config

    def _execute(task: GenerationTask, resume_from_chapter: int) -> None:
        _ = resume_from_chapter
        update_task = _db_task_updater(session_factory)
        run_generation_with_config(
            task.id,
            str(task.message or ""),
            "",
            int(task.requested_chapters or 0),
            config,
            update_task,
            logger,
        )
        with session_factory.begin() as session:
            heartbeat_generation_task(
                session,
                task_id=task.id,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            )

    return _execute


def _db_task_updater(session_factory: Callable[[], Any]) -> Callable[..., None]:
    def _update(task_id: str, **changes: Any) -> None:
        with session_factory.begin() as session:
            row = session.get(GenerationTask, task_id)
            if row is None:
                return
            _apply_task_changes(row, changes)
            session.add(row)

    return _update


def _apply_task_changes(row: GenerationTask, changes: dict[str, Any]) -> None:
    scalar_fields = {
        "status": "status",
        "current_stage": "current_stage",
        "project_id": "project_id",
        "message": "message",
        "error": "error_message",
        "current_chapter": "current_chapter",
    }
    list_fields = {
        "completed_chapters": "completed_chapters_json",
        "failed_chapters": "failed_chapters_json",
        "paused_chapters": "paused_chapters_json",
        "frozen_artifacts": "frozen_artifacts_json",
    }
    for key, attr in scalar_fields.items():
        if key in changes:
            setattr(row, attr, changes[key] if key != "error" else str(changes[key] or ""))
    for key, attr in list_fields.items():
        if key in changes:
            setattr(row, attr, json.dumps(changes[key] or [], ensure_ascii=False))
