from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING
from typing import Any

from pydantic import BaseModel

from forwin.config import InfrastructureConfig
from forwin.generation.task_lease import (
    claim_generation_task,
    generation_task_resume_from_chapter,
    heartbeat_generation_task,
)
from forwin.generation.worker_observability import (
    generation_worker_span,
    record_worker_claim,
    record_worker_execution_failed,
    record_worker_heartbeat_failed,
)
from forwin.models.task import GenerationTask

if TYPE_CHECKING:
    from forwin.application.generation import GenerationApplicationService


logger = logging.getLogger(__name__)


class GenerationWorkerResult(BaseModel):
    claimed: bool = False
    task_id: str = ""
    project_id: str = ""
    resume_from_chapter: int = 0
    executed: bool = False
    message: str = ""


def run_one_generation_task(
    *,
    application_service: GenerationApplicationService,
    worker_id: str,
    lease_seconds: int = 300,
) -> GenerationWorkerResult:
    session_factory = application_service.session_factory
    config = application_service.infrastructure
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
        with generation_worker_span(
            session_factory=session_factory,
            config=config,
            span_name="generation_worker.execute",
            task_id=task_id,
            project_id=project_id,
            worker_id=worker_id,
            tags={"execution_mode": "project_generation"},
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
                application_service.execute_claimed(
                    task,
                    resume_from_chapter=resume_from_chapter,
                    worker_id=worker_id,
                    claim_kind=claim.claim_kind,
                )
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


def _start_periodic_heartbeat(
    *,
    session_factory: Callable[[], Any],
    config: InfrastructureConfig | None,
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
                logger.exception(
                    "Periodic heartbeat failed for generation task %s", task_id
                )
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
