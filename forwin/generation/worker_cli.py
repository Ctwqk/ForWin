from __future__ import annotations

import logging
import os
import socket
import time
from collections.abc import Callable
from typing import Any

from forwin.config import Config
from forwin.generation.ports import CreateContinueGenerationTask
from forwin.generation.worker import GenerationWorkerResult, run_one_generation_task


logger = logging.getLogger(__name__)


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def run_generation_worker_loop(
    *,
    session_factory: Callable[[], Any],
    config: Config,
    worker_id: str = "",
    lease_seconds: int = 300,
    poll_interval: float = 2.0,
    once: bool = False,
    max_loops: int = 0,
    run_once: Callable[..., GenerationWorkerResult] = run_one_generation_task,
    create_continue_generation_task: CreateContinueGenerationTask | None = None,
) -> int:
    normalized_worker_id = str(worker_id or "").strip() or default_worker_id()
    loops = 0
    logger.info(
        "Generation worker starting worker_id=%s lease_seconds=%s poll_interval=%s once=%s",
        normalized_worker_id,
        lease_seconds,
        poll_interval,
        once,
    )
    try:
        while True:
            loops += 1
            result = run_once(
                session_factory=session_factory,
                worker_id=normalized_worker_id,
                config=config,
                lease_seconds=lease_seconds,
                create_continue_generation_task=create_continue_generation_task,
            )
            if result.claimed:
                logger.info(
                    "Generation worker executed task %s project_id=%s resume_from_chapter=%s",
                    result.task_id,
                    result.project_id,
                    result.resume_from_chapter,
                )
            else:
                logger.debug(
                    "No claimable generation task worker_id=%s message=%s",
                    normalized_worker_id,
                    result.message,
                )
            if once:
                return 0
            if max_loops > 0 and loops >= max_loops:
                return 0
            if not result.claimed:
                time.sleep(max(0.0, float(poll_interval or 0.0)))
    except Exception:
        logger.exception("Generation worker loop failed worker_id=%s", normalized_worker_id)
        raise
    finally:
        logger.info("Generation worker stopping worker_id=%s loops=%s", normalized_worker_id, loops)
