from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from forwin.config import Config
from forwin.generation.task_lease import GenerationTaskClaimResult
from forwin.governance import DecisionEventType
from forwin.observability import NullObservability, ObservabilityService, OperationContext
from forwin.observability.ports import ObservabilityPort, SpanHandle


logger = logging.getLogger(__name__)

ObservabilityFactory = Callable[..., ObservabilityPort]


def record_worker_claim(
    *,
    session_factory: Callable[[], Any],
    config: Config | None,
    worker_id: str,
    claim: GenerationTaskClaimResult,
    resume_from_chapter: int,
    lease_seconds: int,
    observability_factory: ObservabilityFactory | None = None,
) -> None:
    task = claim.task
    event_type = (
        DecisionEventType.GENERATION_WORKER_RECLAIMED
        if claim.claim_kind == "expired_running"
        else DecisionEventType.GENERATION_WORKER_CLAIMED
    )
    summary = (
        "生成 worker 已接管过期 generation task lease。"
        if claim.claim_kind == "expired_running"
        else "生成 worker 已认领 generation task。"
    )
    _record_worker_event(
        session_factory=session_factory,
        config=config,
        task_id=str(task.id or ""),
        project_id=str(task.project_id or ""),
        worker_id=worker_id,
        event_type=event_type,
        summary=summary,
        payload=_claim_payload(
            worker_id=worker_id,
            claim=claim,
            resume_from_chapter=resume_from_chapter,
            lease_seconds=lease_seconds,
        ),
        observability_factory=observability_factory,
    )


def record_worker_heartbeat_failed(
    *,
    session_factory: Callable[[], Any],
    config: Config | None,
    task_id: str,
    project_id: str,
    worker_id: str,
    lease_seconds: int,
    observability_factory: ObservabilityFactory | None = None,
) -> None:
    _record_worker_event(
        session_factory=session_factory,
        config=config,
        task_id=task_id,
        project_id=project_id,
        worker_id=worker_id,
        event_type=DecisionEventType.GENERATION_WORKER_HEARTBEAT_FAILED,
        summary="生成 worker heartbeat 失败，task lease ownership 已变化。",
        payload={
            "worker_id": str(worker_id or ""),
            "lease_seconds": max(30, int(lease_seconds or 300)),
        },
        observability_factory=observability_factory,
    )


def record_worker_execution_failed(
    *,
    session_factory: Callable[[], Any],
    config: Config | None,
    task_id: str,
    project_id: str,
    worker_id: str,
    exc: BaseException,
    observability_factory: ObservabilityFactory | None = None,
) -> None:
    if not str(project_id or "").strip():
        logger.warning(
            "generation_worker_execution_failed task_id=%s worker_id=%s error_class=%s",
            task_id,
            worker_id,
            exc.__class__.__name__,
        )
        return
    obs = _build_observability(
        session_factory=session_factory,
        config=config,
        observability_factory=observability_factory,
    )
    ctx = _context(task_id=task_id, project_id=project_id, worker_id=worker_id)
    try:
        obs.error(
            ctx,
            event_type=DecisionEventType.GENERATION_WORKER_EXECUTION_FAILED,
            summary="生成 worker 执行 generation task 失败。",
            scope="task",
            payload={"worker_id": str(worker_id or "")},
            related_object_type="generation_task",
            related_object_id=str(task_id or ""),
        )
    except Exception:
        logger.debug("Ignoring generation worker execution failure observation.", exc_info=True)


@contextmanager
def generation_worker_span(
    *,
    session_factory: Callable[[], Any],
    config: Config | None,
    span_name: str,
    task_id: str,
    project_id: str,
    worker_id: str,
    tags: dict[str, Any] | None = None,
    metrics: dict[str, int | float] | None = None,
    observability_factory: ObservabilityFactory | None = None,
) -> Iterator[SpanHandle]:
    obs = _build_observability(
        session_factory=session_factory,
        config=config,
        observability_factory=observability_factory,
    )
    ctx = _context(task_id=task_id, project_id=project_id, worker_id=worker_id, stage=span_name)
    span_tags = {"worker_id": str(worker_id or ""), **(tags or {})}
    try:
        span_cm = obs.span(
            ctx,
            span_name,
            span_kind="worker",
            component="worker",
            tags=span_tags,
            metrics=metrics or {},
        )
    except Exception:
        logger.debug("Ignoring generation worker span setup failure.", exc_info=True)
        span_cm = NullObservability().span(ctx, span_name)
    with span_cm as span:
        yield span


def _record_worker_event(
    *,
    session_factory: Callable[[], Any],
    config: Config | None,
    task_id: str,
    project_id: str,
    worker_id: str,
    event_type: str,
    summary: str,
    payload: dict[str, Any],
    observability_factory: ObservabilityFactory | None = None,
) -> None:
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id:
        logger.info(
            "generation_worker_event event_type=%s task_id=%s worker_id=%s",
            event_type,
            task_id,
            worker_id,
        )
        return
    obs = _build_observability(
        session_factory=session_factory,
        config=config,
        observability_factory=observability_factory,
    )
    ctx = _context(task_id=task_id, project_id=normalized_project_id, worker_id=worker_id)
    try:
        obs.event(
            ctx,
            event_family="runtime_observation",
            event_type=event_type,
            summary=summary,
            scope="task",
            payload=payload,
            related_object_type="generation_task",
            related_object_id=str(task_id or ""),
        )
    except Exception:
        logger.debug("Ignoring generation worker event observation.", exc_info=True)


def _claim_payload(
    *,
    worker_id: str,
    claim: GenerationTaskClaimResult,
    resume_from_chapter: int,
    lease_seconds: int,
) -> dict[str, Any]:
    task = claim.task
    payload: dict[str, Any] = {
        "worker_id": str(worker_id or ""),
        "lease_expires_at": _isoformat(task.lease_expires_at),
        "resume_from_chapter": max(0, int(resume_from_chapter or 0)),
        "claim_kind": str(claim.claim_kind or ""),
        "lease_seconds": max(30, int(lease_seconds or 300)),
        "execution_mode": "continue" if str(task.project_id or "").strip() else "initial",
    }
    if claim.claim_kind == "expired_running":
        payload["previous_lease_owner"] = str(claim.previous_lease_owner or "")
        payload["previous_lease_expires_at"] = _isoformat(claim.previous_lease_expires_at)
    return {key: value for key, value in payload.items() if value not in ("", None)}


def _context(
    *,
    task_id: str,
    project_id: str,
    worker_id: str,
    stage: str = "generation_worker",
) -> OperationContext:
    return OperationContext(
        project_id=str(project_id or ""),
        task_id=str(task_id or ""),
        stage=stage,
        actor_type="worker",
        actor_id=str(worker_id or ""),
        operation_id=str(task_id or ""),
    )


def _build_observability(
    *,
    session_factory: Callable[[], Any],
    config: Config | None,
    observability_factory: ObservabilityFactory | None,
) -> ObservabilityPort:
    try:
        if observability_factory is not None:
            return observability_factory(session_factory=session_factory, config=config)
        return ObservabilityService(session_factory=session_factory, config=config)
    except Exception:
        logger.debug("Falling back to NullObservability for generation worker.", exc_info=True)
        return NullObservability()


def _isoformat(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""
