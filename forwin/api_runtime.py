from __future__ import annotations

import logging
import time
from typing import Any, Callable

from forwin.generation.task_payload import GenerationExecutionContext
from forwin.governance import DecisionEventType
from forwin.observability import LogRecorder, OperationContext
from forwin.observability.ports import NullObservability
from forwin.generation.pipeline import ChapterPipeline
from forwin.runtime.container import RuntimeContainer
from forwin.state.updater import StateUpdater


TaskUpdater = Callable[..., None]

_PROGRESS_STAGE_STATUS = {
    "cancelled": "cancelled",
    "paused": "paused",
    "paused_for_review": "needs_review",
    "scenario_rehearsal_patch_required": "needs_review",
    "scenario_rehearsal_blocked": "failed",
    "provisional_failed": "failed",
    "failed": "failed",
    "completed": "completed",
    "terminating": "terminating",
}
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
    *,
    include_project_created: bool = False,
) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if include_project_created and event == "project_created":
        changes["project_id"] = payload.get("project_id")
        changes["title"] = payload.get("title") or "未命名项目"
        changes["message"] = f"项目已创建：{payload.get('title', '')}"

    stage = str(payload.get("stage", "")).strip()
    if stage:
        changes["current_stage"] = stage
        status = _PROGRESS_STAGE_STATUS.get(stage)
        if status:
            changes["status"] = status
        elif stage != "queued":
            changes["status"] = "running"

    for key in _PROGRESS_PAYLOAD_KEYS:
        if key in payload:
            changes[key] = payload.get(key)
    return changes


def _paused_chapters_message(result, *, prefix: str = "") -> str:
    system_block_chapters = [
        int(chapter)
        for chapter in getattr(result, "system_block_chapters", []) or []
    ]
    if system_block_chapters:
        chapter_str = ", ".join(str(chapter) for chapter in system_block_chapters)
        return f"{prefix}章节 {chapter_str} 遇到 canon system block，需处理系统阻断后重试"
    paused_str = ", ".join(str(chapter) for chapter in getattr(result, "paused_chapters", []) or [])
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
        logging.getLogger(__name__).debug("Ignoring task observability event failure.", exc_info=True)
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
) -> ChapterPipeline:
    return RuntimeContainer.from_config(
        context.infrastructure,
        policy=context.policy,
        role="generation_worker",
    ).build_chapter_pipeline(
        progress_callback=progress_callback,
        should_abort=should_abort,
        should_pause=should_pause,
        task_id=context.task_id,
        root_event_id=context.root_event_id,
    )


def run_pipeline_task(
    task_id: str,
    pipeline: ChapterPipeline,
    operation,
    *,
    update_task: TaskUpdater,
    logger: logging.Logger,
    error_message: str,
    default_project_id: str | None = None,
    progress_handler=None,
    completion_handler=None,
    should_abort: Callable[[], bool] | None = None,
    should_pause: Callable[[], bool] | None = None,
    component: str = "api",
) -> None:
    started_at = time.perf_counter()
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
            observed_project_id = str(getattr(result, "project_id", "") or observed_project_id or "").strip()
            if observed_project_id and hasattr(span, "context"):
                span.context = OperationContext(
                    project_id=observed_project_id,
                    task_id=task_id,
                    stage="task.operation",
                    operation_id=task_id,
                )
            span.tag("status_after", str(result.status or ""))
            span.metric("failed_chapters", len(getattr(result, "failed_chapters", []) or []))
            span.metric("paused_chapters", len(getattr(result, "paused_chapters", []) or []))
            update_task(
                task_id,
                status=result.status,
                project_id=result.project_id,
                failed_chapters=result.failed_chapters,
                paused_chapters=result.paused_chapters,
                frozen_artifacts=result.frozen_artifacts,
            )
            _record_task_observability_event(
                pipeline,
                task_id=task_id,
                project_id=observed_project_id,
                event_type=DecisionEventType.TASK_OPERATION_SUCCEEDED,
                summary="生成任务 operation 已完成。",
                payload={
                    "status_after": result.status,
                    "duration_ms": max(0, int((time.perf_counter() - started_at) * 1000)),
                    "failed_chapters": list(getattr(result, "failed_chapters", []) or []),
                    "paused_chapters": list(getattr(result, "paused_chapters", []) or []),
                },
            )
        if progress_handler is not None:
            progress_handler(result)
        if completion_handler is not None:
            try:
                completion_handler(result)
            except Exception:  # noqa: BLE001
                logger.exception("Post-completion handler failed for task %s", task_id)
    except Exception as exc:
        logger.exception("%s for task %s", error_message, task_id)
        observed_project_id = str(getattr(exc, "project_id", observed_project_id) or observed_project_id or "").strip()
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
        update_task(
            task_id,
            status="failed",
            project_id=getattr(exc, "project_id", default_project_id),
            error=str(exc),
            message=error_message,
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
                pipeline.llm_client.close()
                pipeline.engine.dispose()
        finally:
            _record_task_observability_event(
                pipeline,
                task_id=task_id,
                project_id=observed_project_id,
                event_type=DecisionEventType.TASK_CLEANUP_FINISHED,
                summary="生成任务 cleanup 已结束。",
            )


def run_generation_with_context(
    context: GenerationExecutionContext,
    premise: str,
    genre: str,
    num_chapters: int,
    update_task: TaskUpdater,
    logger: logging.Logger,
    *,
    project_id: str | None = None,
    should_abort: Callable[[], bool] | None = None,
    should_pause: Callable[[], bool] | None = None,
    completion_handler: Callable[[object], None] | None = None,
    component: str = "api",
) -> None:
    task_id = context.task_id
    normalized_project_id = str(project_id or "").strip()

    def _handle_progress(event: str, payload: dict[str, Any]) -> None:
        changes = _build_task_progress_changes(
            event,
            payload,
            include_project_created=True,
        )
        if changes:
            update_task(task_id, **changes)

    pipeline = _build_chapter_pipeline_for_task(
        context,
        progress_callback=_handle_progress,
        should_abort=should_abort,
        should_pause=should_pause,
    )

    def _handle_result(result) -> None:
        if result.status == "cancelled":
            update_task(
                task_id,
                status="cancelled",
                message=(
                    f"生成任务已取消。已完成 {len(result.completed_chapters)} / "
                    f"{result.requested_chapters} 章"
                ),
            )
        elif result.status == "paused":
            update_task(
                task_id,
                status="paused",
                message=(
                    f"生成任务已安全暂停。已完成 {len(result.completed_chapters)} / "
                    f"{result.requested_chapters} 章"
                ),
            )
        elif result.failed_chapters:
            failed_str = ", ".join(str(chapter) for chapter in result.failed_chapters)
            update_task(
                task_id,
                error=f"以下章节生成失败: {failed_str}",
                message=(
                    f"已完成 {len(result.completed_chapters)} / {result.requested_chapters} 章，"
                    f"失败章节: {failed_str}"
                ),
            )
        elif result.paused_chapters:
            update_task(task_id, message=_paused_chapters_message(result))
        else:
            update_task(
                task_id,
                message=f"已完成 {result.requested_chapters} / {result.requested_chapters} 章",
            )

    if normalized_project_id:
        operation = lambda: pipeline.run_existing_project(  # noqa: E731
            normalized_project_id,
            num_chapters=num_chapters,
        )
    else:
        operation = lambda: pipeline.run(  # noqa: E731
            premise=premise,
            genre=genre,
            num_chapters=num_chapters,
        )

    run_pipeline_task(
        task_id,
        pipeline,
        operation,
        update_task=update_task,
        logger=logger,
        error_message="生成任务失败",
        default_project_id=normalized_project_id or None,
        progress_handler=_handle_result,
        completion_handler=completion_handler,
        should_abort=should_abort,
        should_pause=should_pause,
        component=component,
    )


def run_continue_project_with_context(
    context: GenerationExecutionContext,
    project_id: str,
    update_task: TaskUpdater,
    logger: logging.Logger,
    *,
    should_abort: Callable[[], bool] | None = None,
    should_pause: Callable[[], bool] | None = None,
    max_chapters: int | None = None,
    resume_from_chapter: int | None = None,
    completion_handler: Callable[[object], None] | None = None,
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
    )

    def _handle_result(result) -> None:
        if result.status == "cancelled":
            update_task(
                task_id,
                status="cancelled",
                message=(
                    f"继续生成已取消。已完成 {len(result.completed_chapters)} / "
                    f"{result.requested_chapters} 章"
                ),
            )
        elif result.status == "paused":
            update_task(
                task_id,
                status="paused",
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
            update_task(task_id, message=_paused_chapters_message(result, prefix="继续执行后"))
        elif result.completed_chapters:
            completed_str = ", ".join(str(chapter) for chapter in result.completed_chapters)
            update_task(task_id, message=f"继续执行完成章节: {completed_str}")
        else:
            update_task(task_id, message="没有剩余章节需要继续执行。")

    run_pipeline_task(
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
        completion_handler=completion_handler,
        should_abort=should_abort,
        should_pause=should_pause,
        component=component,
    )
