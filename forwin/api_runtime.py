from __future__ import annotations

import logging
from typing import Any, Callable

from forwin.api_schemas import GenerateRequest
from forwin.config import Config, DEFAULT_MINIMAX_BASE_URL, DEFAULT_MINIMAX_MODEL
from forwin.orchestrator.loop import WritingOrchestrator
from forwin.runtime_settings import RuntimeSettingsStore


TaskUpdater = Callable[..., None]


def copy_config(base_config: Config, **updates: object) -> Config:
    return base_config.model_copy(update=updates)


def build_home_page_settings(
    *,
    base_config: Config | None,
    runtime_settings: RuntimeSettingsStore | None,
) -> dict[str, object]:
    if runtime_settings is not None:
        return runtime_settings.get()
    return {
        "api_key": "",
        "base_url": base_config.minimax_base_url if base_config else DEFAULT_MINIMAX_BASE_URL,
        "model": base_config.minimax_model if base_config else DEFAULT_MINIMAX_MODEL,
        "operation_mode": base_config.operation_mode if base_config else "blackbox",
        "freeze_failed_candidates": base_config.freeze_failed_candidates if base_config else True,
    }


def build_runtime_config(
    req: GenerateRequest,
    *,
    base_config: Config,
    runtime_settings: RuntimeSettingsStore | None,
) -> Config:
    stored = runtime_settings.get() if runtime_settings else {}
    api_key = (req.api_key or "").strip() or stored.get("api_key", base_config.minimax_api_key)
    base_url = (req.base_url or "").strip() or stored.get("base_url", base_config.minimax_base_url)
    model = (req.model or "").strip() or stored.get("model", base_config.minimax_model)
    operation_mode = (req.operation_mode or "").strip() or str(
        stored.get("operation_mode", base_config.operation_mode)
    )
    freeze_failed_candidates = (
        req.freeze_failed_candidates
        if req.freeze_failed_candidates is not None
        else bool(stored.get("freeze_failed_candidates", base_config.freeze_failed_candidates))
    )
    return copy_config(
        base_config,
        minimax_api_key=api_key,
        minimax_base_url=base_url,
        minimax_model=model,
        operation_mode=operation_mode,
        freeze_failed_candidates=freeze_failed_candidates,
    )


def build_saved_runtime_config(
    *,
    base_config: Config,
    runtime_settings: RuntimeSettingsStore | None,
) -> Config:
    stored = runtime_settings.get() if runtime_settings else {}
    return copy_config(
        base_config,
        minimax_api_key=str(stored.get("api_key", base_config.minimax_api_key)),
        minimax_base_url=str(stored.get("base_url", base_config.minimax_base_url)),
        minimax_model=str(stored.get("model", base_config.minimax_model)),
        operation_mode=str(stored.get("operation_mode", base_config.operation_mode)),
        freeze_failed_candidates=bool(
            stored.get("freeze_failed_candidates", base_config.freeze_failed_candidates)
        ),
    )


def run_orchestrator_task(
    task_id: str,
    orchestrator: WritingOrchestrator,
    operation,
    *,
    update_task: TaskUpdater,
    logger: logging.Logger,
    error_message: str,
    default_project_id: str | None = None,
    progress_handler=None,
) -> None:
    try:
        update_task(task_id, status="running")
        result = operation()
        update_task(
            task_id,
            status=result.status,
            project_id=result.project_id,
            failed_chapters=result.failed_chapters,
            paused_chapters=result.paused_chapters,
            frozen_artifacts=result.frozen_artifacts,
        )
        if progress_handler is not None:
            progress_handler(result)
    except Exception as exc:
        logger.exception("%s for task %s", error_message, task_id)
        update_task(
            task_id,
            status="failed",
            project_id=getattr(exc, "project_id", default_project_id),
            error=str(exc),
            message=error_message,
        )
    finally:
        orchestrator.llm_client.close()
        orchestrator.engine.dispose()


def run_generation_with_config(
    task_id: str,
    premise: str,
    genre: str,
    num_chapters: int,
    config: Config,
    update_task: TaskUpdater,
    logger: logging.Logger,
) -> None:
    def _handle_progress(event: str, payload: dict[str, Any]) -> None:
        if event == "project_created":
            update_task(
                task_id,
                project_id=payload.get("project_id"),
                message=f"项目已创建：{payload.get('title', '')}",
            )

    orchestrator = WritingOrchestrator(config, progress_callback=_handle_progress)

    def _handle_result(result) -> None:
        if result.failed_chapters:
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
            paused_str = ", ".join(str(chapter) for chapter in result.paused_chapters)
            update_task(task_id, message=f"已进入人工检查点，暂停章节: {paused_str}")
        else:
            update_task(
                task_id,
                message=f"已完成 {result.requested_chapters} / {result.requested_chapters} 章",
            )

    run_orchestrator_task(
        task_id,
        orchestrator,
        lambda: orchestrator.run(
            premise=premise,
            genre=genre,
            num_chapters=num_chapters,
        ),
        update_task=update_task,
        logger=logger,
        error_message="生成任务失败",
        progress_handler=_handle_result,
    )


def run_continue_project_with_config(
    task_id: str,
    project_id: str,
    config: Config,
    update_task: TaskUpdater,
    logger: logging.Logger,
) -> None:
    orchestrator = WritingOrchestrator(config)

    def _handle_result(result) -> None:
        if result.failed_chapters:
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
            paused_str = ", ".join(str(chapter) for chapter in result.paused_chapters)
            update_task(task_id, message=f"继续执行后再次进入人工检查点，暂停章节: {paused_str}")
        elif result.completed_chapters:
            completed_str = ", ".join(str(chapter) for chapter in result.completed_chapters)
            update_task(task_id, message=f"继续执行完成章节: {completed_str}")
        else:
            update_task(task_id, message="没有剩余章节需要继续执行。")

    run_orchestrator_task(
        task_id,
        orchestrator,
        lambda: orchestrator.continue_project(project_id),
        update_task=update_task,
        logger=logger,
        error_message="继续生成失败",
        default_project_id=project_id,
        progress_handler=_handle_result,
    )
