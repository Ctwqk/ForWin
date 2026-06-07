from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from forwin.config import Config
from forwin.generation.task_lease import (
    claim_generation_task,
    generation_task_resume_from_chapter,
    heartbeat_generation_task,
)
from forwin.generation.task_payload import (
    build_worker_config_from_payload,
    payload_from_json,
)
from forwin.generation.worker_observability import (
    generation_worker_span,
    record_worker_claim,
    record_worker_execution_failed,
    record_worker_heartbeat_failed,
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
        claim = claim_generation_task(
            session,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        if claim is None:
            return GenerationWorkerResult(message="no_claimable_generation_task")
        task = claim.task
        task_id = task.id
        project_id = str(task.project_id or "")
        resume_from_chapter = generation_task_resume_from_chapter(task)

    task_id = str(task_id or "")
    project_id = str(project_id or "")
    record_worker_claim(
        session_factory=session_factory,
        config=config,
        worker_id=worker_id,
        claim=claim,
        resume_from_chapter=resume_from_chapter,
        lease_seconds=lease_seconds,
    )
    with generation_worker_span(
        session_factory=session_factory,
        config=config,
        span_name="generation_worker.claim",
        task_id=task_id,
        project_id=project_id,
        worker_id=worker_id,
        tags={"claim_kind": claim.claim_kind},
        metrics={
            "claimed": 1,
            "lease_seconds": max(30, int(lease_seconds or 300)),
            "resume_from_chapter": max(0, int(resume_from_chapter or 0)),
        },
    ):
        pass

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
        with generation_worker_span(
            session_factory=session_factory,
            config=config,
            span_name="generation_worker.execute",
            task_id=task_id,
            project_id=project_id,
            worker_id=worker_id,
            tags={"execution_mode": "continue" if project_id else "initial"},
            metrics={"resume_from_chapter": max(0, int(resume_from_chapter or 0))},
        ):
            stop_periodic_heartbeat = _start_periodic_heartbeat(
                session_factory=session_factory,
                config=config,
                task_id=task_id,
                project_id=project_id,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            )
            try:
                executor(task, resume_from_chapter)
            finally:
                stop_periodic_heartbeat()
    except Exception as exc:
        logger.exception("Generation worker failed task %s", task_id)
        record_worker_execution_failed(
            session_factory=session_factory,
            config=config,
            task_id=task_id,
            project_id=project_id,
            worker_id=worker_id,
            exc=exc,
        )
        with session_factory.begin() as session:
            row = session.get(GenerationTask, task_id)
            if row is not None and row.lease_owner == worker_id:
                row.status = "failed"
                row.current_stage = "failed"
                row.error_message = "generation_worker_execution_failed"
                session.add(row)
        raise

    with session_factory.begin() as session:
        heartbeat_ok = heartbeat_generation_task(
            session,
            task_id=task_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
    if not heartbeat_ok:
        record_worker_heartbeat_failed(
            session_factory=session_factory,
            config=config,
            task_id=task_id,
            project_id=project_id,
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
        payload = payload_from_json(getattr(task, "execution_payload_json", "{}"))
        worker_config = build_worker_config_from_payload(
            config,
            payload,
            task_id=task.id,
        )
        completion_handler = _worker_completion_handler(
            session_factory=session_factory,
            task_id=task.id,
            payload=payload,
            worker_config=worker_config,
        )
        update_task = _db_task_updater(
            session_factory,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        run_continue_project_with_config(
            task.id,
            str(task.project_id or ""),
            worker_config,
            update_task,
            logger,
            should_abort=_db_task_flag(session_factory, task.id, "cancel_requested"),
            should_pause=_db_task_flag(session_factory, task.id, "pause_requested"),
            max_chapters=int(task.max_chapters or 0) or None,
            resume_from_chapter=resume_from_chapter,
            completion_handler=completion_handler,
            component="worker",
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
        payload = payload_from_json(getattr(task, "execution_payload_json", "{}"))
        worker_config = build_worker_config_from_payload(
            config,
            payload,
            task_id=task.id,
        )
        completion_handler = _worker_completion_handler(
            session_factory=session_factory,
            task_id=task.id,
            payload=payload,
            worker_config=worker_config,
        )
        update_task = _db_task_updater(
            session_factory,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        run_generation_with_config(
            task.id,
            payload.premise,
            payload.genre,
            int(payload.num_chapters or task.requested_chapters or 0),
            worker_config,
            update_task,
            logger,
            should_abort=_db_task_flag(session_factory, task.id, "cancel_requested"),
            should_pause=_db_task_flag(session_factory, task.id, "pause_requested"),
            completion_handler=completion_handler,
            component="worker",
        )
        with session_factory.begin() as session:
            heartbeat_generation_task(
                session,
                task_id=task.id,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            )

    return _execute


def _db_task_updater(
    session_factory: Callable[[], Any],
    *,
    worker_id: str = "",
    lease_seconds: int = 300,
) -> Callable[..., None]:
    def _update(task_id: str, **changes: Any) -> None:
        with session_factory.begin() as session:
            row = session.get(GenerationTask, task_id)
            if row is None:
                return
            _apply_task_changes(row, changes)
            if str(worker_id or "").strip():
                heartbeat_generation_task(
                    session,
                    task_id=task_id,
                    worker_id=worker_id,
                    lease_seconds=lease_seconds,
                )
            session.add(row)

    return _update


def _start_periodic_heartbeat(
    *,
    session_factory: Callable[[], Any],
    config: Config | None,
    task_id: str,
    project_id: str,
    worker_id: str,
    lease_seconds: int,
) -> Callable[[], None]:
    stop_event = threading.Event()
    interval_seconds = _heartbeat_interval_seconds(lease_seconds)

    def _loop() -> None:
        while not stop_event.wait(interval_seconds):
            try:
                with session_factory.begin() as session:
                    heartbeat_ok = heartbeat_generation_task(
                        session,
                        task_id=task_id,
                        worker_id=worker_id,
                        lease_seconds=lease_seconds,
                    )
            except Exception:
                logger.exception("Periodic heartbeat failed for generation task %s", task_id)
                continue
            if not heartbeat_ok:
                record_worker_heartbeat_failed(
                    session_factory=session_factory,
                    config=config,
                    task_id=task_id,
                    project_id=project_id,
                    worker_id=worker_id,
                    lease_seconds=lease_seconds,
                )
                return

    thread = threading.Thread(
        target=_loop,
        name=f"forwin-generation-heartbeat-{task_id}",
        daemon=True,
    )
    thread.start()

    def _stop() -> None:
        stop_event.set()
        thread.join(timeout=interval_seconds + 1.0)

    return _stop


def _heartbeat_interval_seconds(lease_seconds: int) -> float:
    normalized = max(1, int(lease_seconds or 300))
    return float(max(1, min(60, normalized // 3 or 1)))


def _db_task_flag(
    session_factory: Callable[[], Any],
    task_id: str,
    attr: str,
) -> Callable[[], bool]:
    def _read() -> bool:
        with session_factory() as session:
            row = session.get(GenerationTask, task_id)
            return bool(getattr(row, attr, False)) if row is not None else True

    return _read


def _worker_completion_handler(
    *,
    session_factory: Callable[[], Any],
    task_id: str,
    payload,
    worker_config: Config,
) -> Callable[[object], None]:
    from forwin.generation.auto_continue import GenerationAutoContinueController

    def _create_next_task(**kwargs: Any) -> str:
        from forwin.api_core.generation import _create_continue_generation_task

        return _create_continue_generation_task(**kwargs)

    def _handle(result: object) -> None:
        if not bool(getattr(payload, "auto_continue", True)):
            return
        controller = GenerationAutoContinueController(
            session_factory=session_factory,
            create_continue_generation_task=_create_next_task,
        )
        controller.after_task_completion(
            result,
            parent_task_id=task_id,
            run_until_chapter=int(getattr(payload, "run_until_chapter", 0) or 0) or None,
            max_chapters=int(getattr(payload, "max_chapters", 0) or 0) or None,
            auto_continue=bool(getattr(payload, "auto_continue", True)),
            runtime_config=worker_config,
        )

    return _handle


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
