from __future__ import annotations

import logging
import time
from typing import Any, Callable

from forwin.generation.task_payload import GenerationExecutionContext
from forwin.audit.events import DecisionEventType
from forwin.observability import LogRecorder, OperationContext
from forwin.observability.ports import NullObservability
from forwin.generation.pipeline import ChapterPipeline
from forwin.runtime.container import RuntimeContainer
from forwin.state.updater import StateUpdater


TaskUpdater = Callable[..., None]

_PROGRESS_PAYLOAD_KEYS = (
    "project_id",
    "requested_chapters",
    "current_chapter",
    "completed_chapters",
    "failed_chapters",
    "paused_chapters",
    "frozen_artifacts",
)


def _build_task_progress_changes(
    event: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    stage = str(payload.get("stage", "")).strip()
    if stage:
        changes["current_stage"] = stage

    for key in _PROGRESS_PAYLOAD_KEYS:
        if key in payload:
            changes[key] = payload.get(key)
    return changes


def _paused_chapters_message(result, *, prefix: str = "") -> str:
    system_block_chapters = [
        int(chapter) for chapter in getattr(result, "system_block_chapters", []) or []
    ]
    if system_block_chapters:
        chapter_str = ", ".join(str(chapter) for chapter in system_block_chapters)
        return (
            f"{prefix}章节 {chapter_str} 遇到 canon system block，需处理系统阻断后重试"
        )
    paused_str = ", ".join(
        str(chapter) for chapter in getattr(result, "paused_chapters", []) or []
    )
    if prefix:
        return f"{prefix}遇到质量门阻断，需自动修复或重试章节: {paused_str}"
    return f"质量门阻断，需自动修复或重试章节: {paused_str}"


def _record_task_observability_event(
    pipeline: ChapterPipeline,
    *,
    task_id: str,
    project_id: str | None,
    event_type: str,
    summary: str,
    payload: dict[str, Any] | None = None,
    exc: BaseException | None = None,
) -> None:
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id:
        return
    session_factory = getattr(pipeline, "_SessionFactory", None)
    if session_factory is None:
        return
    session = session_factory()
    try:
        updater = StateUpdater(session)
        recorder = LogRecorder(updater=updater)
        context = OperationContext(
            project_id=normalized_project_id,
            task_id=task_id,
            stage=str((payload or {}).get("stage") or ""),
        )
        if exc is not None:
            recorder.record_error(
                context,
                event_type=event_type,
                summary=summary,
                exc=exc,
                payload=payload or {},
                scope="task",
                related_object_type="generation_task",
                related_object_id=task_id,
            )
        else:
            recorder.record_event(
                context,
                event_family="runtime_observation",
                event_type=event_type,
                summary=summary,
                scope="task",
                payload=payload or {},
                related_object_type="generation_task",
                related_object_id=task_id,
            )
        session.commit()
    except Exception:  # noqa: BLE001
        session.rollback()
        logging.getLogger(__name__).debug(
            "Ignoring task observability event failure.", exc_info=True
        )
    finally:
        session.close()


def _task_observability(pipeline: ChapterPipeline):
    observability = getattr(pipeline, "observability", None)
    return observability if observability is not None else NullObservability()


def _build_chapter_pipeline_for_task(
    context: GenerationExecutionContext,
    *,
    progress_callback=None,
    should_abort=None,
    should_pause=None,
    canon_transaction_guard=None,
    reserve_chapter=None,
) -> ChapterPipeline:
    container = RuntimeContainer.from_config(
        context.infrastructure,
        policy=context.policy,
        role="generation_worker",
    )
    try:
        pipeline = container.build_chapter_pipeline(
            progress_callback=progress_callback,
            should_abort=should_abort,
            should_pause=should_pause,
            task_id=context.task_id,
            root_event_id=context.root_event_id,
        )
        pipeline.capacity_reserver = reserve_chapter
        if canon_transaction_guard is not None:
            from forwin.canon.admission import CanonAdmissionService

            pipeline.canon_admission = CanonAdmissionService(
                session_factory=pipeline._SessionFactory,
                transaction_guard=canon_transaction_guard,
            )
        return pipeline
    except Exception:
        container.close()
        raise


def execute_pipeline_task(
    task_id: str,
    pipeline: ChapterPipeline,
    operation,
    *,
    update_task: TaskUpdater,
    logger: logging.Logger,
    error_message: str,
    default_project_id: str | None = None,
    progress_handler=None,
    finish_task=None,
    should_abort: Callable[[], bool] | None = None,
    should_pause: Callable[[], bool] | None = None,
    component: str = "api",
) -> None:
    started_at = time.perf_counter()
    finalized = False
    finishing = False
    observed_project_id = default_project_id
    observability = _task_observability(pipeline)
    span_component = str(component or "api").strip() or "api"
    try:
        operation_ctx = OperationContext(
            project_id=str(observed_project_id or "").strip(),
            task_id=task_id,
            stage="task.operation",
            operation_id=task_id,
        )
        with observability.span(
            operation_ctx,
            "task.operation",
            span_kind="task",
            component=span_component,
        ) as span:
            if not (should_abort and should_abort()):
                update_task(task_id, status="running")
            _record_task_observability_event(
                pipeline,
                task_id=task_id,
                project_id=observed_project_id,
                event_type=DecisionEventType.TASK_OPERATION_STARTED,
                summary="生成任务 operation 已开始。",
                payload={"status_after": "running"},
            )
            result = operation()
            observed_project_id = str(
                getattr(result, "project_id", "") or observed_project_id or ""
            ).strip()
            if observed_project_id and hasattr(span, "context"):
                span.context = OperationContext(
                    project_id=observed_project_id,
                    task_id=task_id,
                    stage="task.operation",
                    operation_id=task_id,
                )
            span.tag("status_after", str(result.status or ""))
            span.metric(
                "failed_chapters", len(getattr(result, "failed_chapters", []) or [])
            )
            span.metric(
                "paused_chapters", len(getattr(result, "paused_chapters", []) or [])
            )
            if finish_task is None:
                raise RuntimeError("generation task requires atomic finalization")
            finishing = True
            finish_task(result)
            finalized = True
            _record_task_observability_event(
                pipeline,
                task_id=task_id,
                project_id=observed_project_id,
                event_type=DecisionEventType.TASK_OPERATION_SUCCEEDED,
                summary="生成任务 operation 已完成。",
                payload={
                    "status_after": result.status,
                    "duration_ms": max(
                        0, int((time.perf_counter() - started_at) * 1000)
                    ),
                    "failed_chapters": list(
                        getattr(result, "failed_chapters", []) or []
                    ),
                    "paused_chapters": list(
                        getattr(result, "paused_chapters", []) or []
                    ),
                },
            )
        if progress_handler is not None:
            progress_handler(result)
    except Exception as exc:
        if finishing and not finalized:
            # Leave the lease recoverable when the completion transaction failed.
            raise
        if finalized:
            logger.exception("Post-finalization display failed for task %s", task_id)
            return
        logger.exception("%s for task %s", error_message, task_id)
        observed_project_id = str(
            getattr(exc, "project_id", observed_project_id) or observed_project_id or ""
        ).strip()
        _record_task_observability_event(
            pipeline,
            task_id=task_id,
            project_id=observed_project_id,
            event_type=DecisionEventType.TASK_OPERATION_FAILED,
            summary=error_message,
            payload={
                "status_after": "failed",
                "duration_ms": max(0, int((time.perf_counter() - started_at) * 1000)),
            },
            exc=exc,
        )
        if finish_task is None:
            raise
        from forwin.generation.continuation_events import GenerationCompletionResult

        finish_task(
            GenerationCompletionResult(
                project_id=str(observed_project_id or ""),
                failure_reason=str(exc) or type(exc).__name__,
            )
        )
    finally:
        cleanup_ctx = OperationContext(
            project_id=str(observed_project_id or "").strip(),
            task_id=task_id,
            stage="task.cleanup",
            operation_id=task_id,
        )
        _record_task_observability_event(
            pipeline,
            task_id=task_id,
            project_id=observed_project_id,
            event_type=DecisionEventType.TASK_CLEANUP_STARTED,
            summary="生成任务 cleanup 已开始。",
        )
        try:
            with observability.span(
                cleanup_ctx,
                "task.cleanup",
                span_kind="task",
                component=span_component,
            ):
                _record_task_observability_event(
                    pipeline,
                    task_id=task_id,
                    project_id=observed_project_id,
                    event_type=DecisionEventType.TASK_CLEANUP_FINISHED,
                    summary="生成任务 cleanup 已结束。",
                )
        finally:
            pipeline.close()


def execute_continuation(
    context: GenerationExecutionContext,
    project_id: str,
    update_task: TaskUpdater,
    logger: logging.Logger,
    *,
    should_abort: Callable[[], bool] | None = None,
    should_pause: Callable[[], bool] | None = None,
    max_chapters: int | None = None,
    resume_from_chapter: int | None = None,
    finish_task: Callable[[object], None] | None = None,
    canon_transaction_guard: Callable[[Any], bool] | None = None,
    reserve_chapter: Callable[[str, int], None] | None = None,
    component: str = "api",
) -> None:
    task_id = context.task_id

    def _handle_progress(event: str, payload: dict[str, Any]) -> None:
        changes = _build_task_progress_changes(event, payload)
        if changes:
            update_task(task_id, **changes)

    pipeline = _build_chapter_pipeline_for_task(
        context,
        progress_callback=_handle_progress,
        should_abort=should_abort,
        should_pause=should_pause,
        canon_transaction_guard=canon_transaction_guard,
        reserve_chapter=reserve_chapter,
    )

    def _handle_result(result) -> None:
        if result.status == "capacity_wait":
            update_task(
                task_id,
                current_stage="capacity_wait",
                current_chapter=result.capacity_wait_chapter,
                message=result.capacity_wait_reason,
                error=None,
            )
        elif result.status == "cancelled":
            update_task(
                task_id,
                message=(
                    f"继续生成已取消。已完成 {len(result.completed_chapters)} / "
                    f"{result.requested_chapters} 章"
                ),
            )
        elif result.status == "paused":
            update_task(
                task_id,
                message=(
                    f"继续生成已安全暂停。已完成 {len(result.completed_chapters)} 章"
                ),
            )
        elif result.failed_chapters:
            failed_str = ", ".join(str(chapter) for chapter in result.failed_chapters)
            update_task(
                task_id,
                error=f"以下章节生成失败: {failed_str}",
                message=(
                    f"继续执行后完成 {len(result.completed_chapters)} 章，"
                    f"失败章节: {failed_str}"
                ),
            )
        elif result.paused_chapters:
            update_task(
                task_id, message=_paused_chapters_message(result, prefix="继续执行后")
            )
        elif result.completed_chapters:
            completed_str = ", ".join(
                str(chapter) for chapter in result.completed_chapters
            )
            update_task(task_id, message=f"继续执行完成章节: {completed_str}")
        else:
            update_task(task_id, message="没有剩余章节需要继续执行。")

    execute_pipeline_task(
        task_id,
        pipeline,
        lambda: pipeline.continue_project(
            project_id,
            max_chapters=max_chapters,
            resume_from_chapter=resume_from_chapter,
        ),
        update_task=update_task,
        logger=logger,
        error_message="继续生成失败",
        default_project_id=project_id,
        progress_handler=_handle_result,
        finish_task=finish_task,
        should_abort=should_abort,
        should_pause=should_pause,
        component=component,
    )
