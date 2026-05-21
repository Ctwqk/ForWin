from __future__ import annotations

import logging
import time
from typing import Any, Callable

from forwin.api_schemas import GenerateRequest
from forwin.config import Config, DEFAULT_MINIMAX_BASE_URL, DEFAULT_MINIMAX_MODEL
from forwin.governance import DecisionEventType
from forwin.observability import LogRecorder, OperationContext
from forwin.observability.ports import NullObservability
from forwin.orchestrator.loop import WritingOrchestrator
from forwin.runtime.container import RuntimeContainer
from forwin.runtime_settings import RuntimeSettingsStore
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


def copy_config(base_config: Config, **updates: object) -> Config:
    values = base_config.model_dump()
    values.update(updates)
    return Config(**values)


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
        "min_chapter_chars": base_config.min_chapter_chars if base_config else 2500,
        "review_interval_chapters": base_config.review_interval_chapters if base_config else 0,
        "progression_mode": base_config.progression_mode if base_config else "serial_canon_band_guard",
        "auto_band_checkpoint": base_config.auto_band_checkpoint if base_config else True,
        "band_warn_action": base_config.band_warn_action if base_config else "pause",
        "manual_checkpoints_enabled": base_config.manual_checkpoints_enabled if base_config else True,
        "future_constraints_enabled": base_config.future_constraints_enabled if base_config else True,
        "generation_audit_interval_chapters": base_config.generation_audit_interval_chapters if base_config else 0,
        "generation_audit_pause_enabled": base_config.generation_audit_pause_enabled if base_config else False,
        "skill_runtime_enabled": base_config.skill_runtime_enabled if base_config else True,
        "skill_registry_path": base_config.skill_registry_path if base_config else "forwin_skills",
        "skill_strictness": base_config.skill_strictness if base_config else "normal",
        "enabled_skill_groups": list(base_config.enabled_skill_groups) if base_config else [],
        "disabled_skill_ids": list(base_config.disabled_skill_ids) if base_config else [],
    }


def _resolve_profile(
    stored: dict[str, object],
    *,
    requested_profile_id: str = "",
) -> dict[str, str]:
    profiles = [
        item for item in stored.get("profiles", [])
        if isinstance(item, dict)
    ]
    target_id = requested_profile_id.strip() or str(stored.get("default_profile_id", "")).strip()
    selected = next(
        (
            item for item in profiles
            if str(item.get("id", "")).strip() == target_id
        ),
        None,
    )
    if selected is None and profiles:
        selected = profiles[0]
    if selected is None:
        selected = {
            "id": "",
            "api_key": str(stored.get("api_key", "")).strip(),
            "base_url": str(stored.get("base_url", "")).strip(),
            "model": str(stored.get("model", "")).strip(),
        }
    return {
        "id": str(selected.get("id", "")).strip(),
        "name": str(selected.get("name", "")).strip(),
        "api_key": str(selected.get("api_key", "")).strip(),
        "base_url": str(selected.get("base_url", "")).strip(),
        "model": str(selected.get("model", "")).strip(),
    }


def _runtime_fallback_profiles(
    stored: dict[str, object],
    *,
    primary: dict[str, str],
) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    if primary.get("api_key") and primary.get("base_url") and primary.get("model"):
        candidates.append(dict(primary))
    for item in stored.get("profiles", []):
        if not isinstance(item, dict):
            continue
        profile = {
            "id": str(item.get("id", "")).strip(),
            "name": str(item.get("name", "")).strip(),
            "api_key": str(item.get("api_key", "")).strip(),
            "base_url": str(item.get("base_url", "")).strip(),
            "model": str(item.get("model", "")).strip(),
        }
        if not profile["api_key"] or not profile["base_url"] or not profile["model"]:
            continue
        candidates.append(profile)
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for profile in candidates:
        key = (profile["api_key"], profile["base_url"].rstrip("/"), profile["model"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(profile)
    return deduped


def build_runtime_config(
    req: GenerateRequest,
    *,
    base_config: Config,
    runtime_settings: RuntimeSettingsStore | None,
) -> Config:
    stored = runtime_settings.get() if runtime_settings else {}
    selected = _resolve_profile(
        stored,
        requested_profile_id=str(req.model_profile_id or "").strip(),
    )
    api_key = (req.api_key or "").strip() or selected.get("api_key") or str(stored.get("api_key", base_config.minimax_api_key))
    base_url = (req.base_url or "").strip() or selected.get("base_url") or str(stored.get("base_url", base_config.minimax_base_url))
    model = (req.model or "").strip() or selected.get("model") or str(stored.get("model", base_config.minimax_model))
    operation_mode = (req.operation_mode or "").strip() or str(
        stored.get("operation_mode", base_config.operation_mode)
    )
    freeze_failed_candidates = (
        req.freeze_failed_candidates
        if req.freeze_failed_candidates is not None
        else bool(stored.get("freeze_failed_candidates", base_config.freeze_failed_candidates))
    )
    review_interval_chapters = (
        req.review_interval_chapters
        if req.review_interval_chapters is not None
        else int(stored.get("review_interval_chapters", base_config.review_interval_chapters))
    )
    progression_mode = (req.progression_mode or "").strip() or str(
        stored.get("progression_mode", base_config.progression_mode)
    )
    auto_band_checkpoint = (
        req.auto_band_checkpoint
        if req.auto_band_checkpoint is not None
        else bool(stored.get("auto_band_checkpoint", base_config.auto_band_checkpoint))
    )
    band_warn_action = (req.band_warn_action or "").strip() or str(
        stored.get("band_warn_action", base_config.band_warn_action)
    )
    manual_checkpoints_enabled = (
        req.manual_checkpoints_enabled
        if req.manual_checkpoints_enabled is not None
        else bool(stored.get("manual_checkpoints_enabled", base_config.manual_checkpoints_enabled))
    )
    future_constraints_enabled = (
        req.future_constraints_enabled
        if req.future_constraints_enabled is not None
        else bool(stored.get("future_constraints_enabled", base_config.future_constraints_enabled))
    )
    generation_audit_interval_chapters = int(
        stored.get(
            "generation_audit_interval_chapters",
            base_config.generation_audit_interval_chapters,
        )
    )
    generation_audit_pause_enabled = bool(
        stored.get(
            "generation_audit_pause_enabled",
            base_config.generation_audit_pause_enabled,
        )
    )
    min_chapter_chars = max(500, int(
        req.min_chapter_chars
        if req.min_chapter_chars is not None
        else int(stored.get("min_chapter_chars", base_config.min_chapter_chars))
    ))
    target_chapter_chars = max(min_chapter_chars, int(base_config.target_chapter_chars))
    max_chapter_chars = max(target_chapter_chars, int(base_config.max_chapter_chars))
    return copy_config(
        base_config,
        minimax_api_key=api_key,
        minimax_base_url=base_url,
        minimax_model=model,
        operation_mode=operation_mode,
        freeze_failed_candidates=freeze_failed_candidates,
        review_interval_chapters=max(0, int(review_interval_chapters)),
        progression_mode=progression_mode or "serial_canon_band_guard",
        auto_band_checkpoint=bool(auto_band_checkpoint),
        band_warn_action=band_warn_action or "pause",
        manual_checkpoints_enabled=bool(manual_checkpoints_enabled),
        future_constraints_enabled=bool(future_constraints_enabled),
        generation_audit_interval_chapters=max(0, int(generation_audit_interval_chapters)),
        generation_audit_pause_enabled=bool(generation_audit_pause_enabled),
        skill_runtime_enabled=bool(
            stored.get("skill_runtime_enabled", base_config.skill_runtime_enabled)
        ),
        skill_registry_path=str(
            stored.get("skill_registry_path", base_config.skill_registry_path)
        ),
        skill_strictness=str(
            stored.get("skill_strictness", base_config.skill_strictness)
        ),
        enabled_skill_groups=[
            str(item).strip()
            for item in (stored.get("enabled_skill_groups") or base_config.enabled_skill_groups or [])
            if str(item).strip()
        ],
        disabled_skill_ids=[
            str(item).strip()
            for item in (stored.get("disabled_skill_ids") or base_config.disabled_skill_ids or [])
            if str(item).strip()
        ],
        llm_fallback_profiles=_runtime_fallback_profiles(
            stored,
            primary={
                "id": selected.get("id", ""),
                "name": selected.get("name", ""),
                "api_key": api_key,
                "base_url": base_url,
                "model": model,
            },
        ),
        min_chapter_chars=min_chapter_chars,
        target_chapter_chars=target_chapter_chars,
        max_chapter_chars=max_chapter_chars,
    )


def build_saved_runtime_config(
    *,
    base_config: Config,
    runtime_settings: RuntimeSettingsStore | None,
) -> Config:
    stored = runtime_settings.get() if runtime_settings else {}
    min_chapter_chars = max(500, int(stored.get("min_chapter_chars", base_config.min_chapter_chars)))
    target_chapter_chars = max(min_chapter_chars, int(base_config.target_chapter_chars))
    max_chapter_chars = max(target_chapter_chars, int(base_config.max_chapter_chars))
    return copy_config(
        base_config,
        minimax_api_key=str(stored.get("api_key", base_config.minimax_api_key)),
        minimax_base_url=str(stored.get("base_url", base_config.minimax_base_url)),
        minimax_model=str(stored.get("model", base_config.minimax_model)),
        operation_mode=str(stored.get("operation_mode", base_config.operation_mode)),
        freeze_failed_candidates=bool(
            stored.get("freeze_failed_candidates", base_config.freeze_failed_candidates)
        ),
        review_interval_chapters=max(
            0,
            int(stored.get("review_interval_chapters", base_config.review_interval_chapters)),
        ),
        progression_mode=str(stored.get("progression_mode", base_config.progression_mode)),
        auto_band_checkpoint=bool(
            stored.get("auto_band_checkpoint", base_config.auto_band_checkpoint)
        ),
        band_warn_action=str(stored.get("band_warn_action", base_config.band_warn_action)),
        manual_checkpoints_enabled=bool(
            stored.get("manual_checkpoints_enabled", base_config.manual_checkpoints_enabled)
        ),
        future_constraints_enabled=bool(
            stored.get("future_constraints_enabled", base_config.future_constraints_enabled)
        ),
        generation_audit_interval_chapters=max(
            0,
            int(
                stored.get(
                    "generation_audit_interval_chapters",
                    base_config.generation_audit_interval_chapters,
                )
            ),
        ),
        generation_audit_pause_enabled=bool(
            stored.get(
                "generation_audit_pause_enabled",
                base_config.generation_audit_pause_enabled,
            )
        ),
        skill_runtime_enabled=bool(
            stored.get("skill_runtime_enabled", base_config.skill_runtime_enabled)
        ),
        skill_registry_path=str(
            stored.get("skill_registry_path", base_config.skill_registry_path)
        ),
        skill_strictness=str(
            stored.get("skill_strictness", base_config.skill_strictness)
        ),
        enabled_skill_groups=[
            str(item).strip()
            for item in (stored.get("enabled_skill_groups") or base_config.enabled_skill_groups or [])
            if str(item).strip()
        ],
        disabled_skill_ids=[
            str(item).strip()
            for item in (stored.get("disabled_skill_ids") or base_config.disabled_skill_ids or [])
            if str(item).strip()
        ],
        llm_fallback_profiles=_runtime_fallback_profiles(
            stored,
            primary={
                "id": str(stored.get("default_profile_id", "")).strip(),
                "name": "",
                "api_key": str(stored.get("api_key", base_config.minimax_api_key)),
                "base_url": str(stored.get("base_url", base_config.minimax_base_url)),
                "model": str(stored.get("model", base_config.minimax_model)),
            },
        ),
        min_chapter_chars=min_chapter_chars,
        target_chapter_chars=target_chapter_chars,
        max_chapter_chars=max_chapter_chars,
    )


def _record_task_observability_event(
    orchestrator: WritingOrchestrator,
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
    session_factory = getattr(orchestrator, "_SessionFactory", None)
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


def _task_observability(orchestrator: WritingOrchestrator):
    services = getattr(orchestrator, "services", None)
    observability = getattr(services, "observability", None)
    return observability if observability is not None else NullObservability()


def _build_writing_orchestrator_for_task(
    config: Config,
    *,
    progress_callback=None,
    should_abort=None,
    should_pause=None,
) -> WritingOrchestrator:
    if getattr(WritingOrchestrator, "__module__", "") != "forwin.orchestrator.loop":
        return WritingOrchestrator(
            config,
            progress_callback=progress_callback,
            should_abort=should_abort,
            should_pause=should_pause,
        )
    return RuntimeContainer.from_config(config).build_writing_orchestrator(
        progress_callback=progress_callback,
        should_abort=should_abort,
        should_pause=should_pause,
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
    completion_handler=None,
    should_abort: Callable[[], bool] | None = None,
    should_pause: Callable[[], bool] | None = None,
) -> None:
    started_at = time.perf_counter()
    observed_project_id = default_project_id
    observability = _task_observability(orchestrator)
    try:
        operation_ctx = OperationContext(
            project_id=str(observed_project_id or "").strip(),
            task_id=task_id,
            stage="task.operation",
            operation_id=task_id,
        )
        with observability.span(operation_ctx, "task.operation", span_kind="task", component="api") as span:
            if not (should_abort and should_abort()):
                update_task(task_id, status="running")
            _record_task_observability_event(
                orchestrator,
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
                orchestrator,
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
            orchestrator,
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
            orchestrator,
            task_id=task_id,
            project_id=observed_project_id,
            event_type=DecisionEventType.TASK_CLEANUP_STARTED,
            summary="生成任务 cleanup 已开始。",
        )
        try:
            with observability.span(cleanup_ctx, "task.cleanup", span_kind="task", component="api"):
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()
        finally:
            _record_task_observability_event(
                orchestrator,
                task_id=task_id,
                project_id=observed_project_id,
                event_type=DecisionEventType.TASK_CLEANUP_FINISHED,
                summary="生成任务 cleanup 已结束。",
            )


def run_generation_with_config(
    task_id: str,
    premise: str,
    genre: str,
    num_chapters: int,
    config: Config,
    update_task: TaskUpdater,
    logger: logging.Logger,
    project_id: str | None = None,
    should_abort: Callable[[], bool] | None = None,
    should_pause: Callable[[], bool] | None = None,
    completion_handler: Callable[[object], None] | None = None,
) -> None:
    normalized_project_id = str(project_id or "").strip()

    def _handle_progress(event: str, payload: dict[str, Any]) -> None:
        changes = _build_task_progress_changes(
            event,
            payload,
            include_project_created=True,
        )
        if changes:
            update_task(task_id, **changes)

    orchestrator = _build_writing_orchestrator_for_task(
        config,
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
            paused_str = ", ".join(str(chapter) for chapter in result.paused_chapters)
            update_task(task_id, message=f"质量门阻断，需自动修复或重试章节: {paused_str}")
        else:
            update_task(
                task_id,
                message=f"已完成 {result.requested_chapters} / {result.requested_chapters} 章",
            )

    if normalized_project_id:
        operation = lambda: orchestrator.run_existing_project(  # noqa: E731
            normalized_project_id,
            num_chapters=num_chapters,
        )
    else:
        operation = lambda: orchestrator.run(  # noqa: E731
            premise=premise,
            genre=genre,
            num_chapters=num_chapters,
        )

    run_orchestrator_task(
        task_id,
        orchestrator,
        operation,
        update_task=update_task,
        logger=logger,
        error_message="生成任务失败",
        default_project_id=normalized_project_id or None,
        progress_handler=_handle_result,
        completion_handler=completion_handler,
        should_abort=should_abort,
        should_pause=should_pause,
    )


def run_continue_project_with_config(
    task_id: str,
    project_id: str,
    config: Config,
    update_task: TaskUpdater,
    logger: logging.Logger,
    should_abort: Callable[[], bool] | None = None,
    should_pause: Callable[[], bool] | None = None,
    max_chapters: int | None = None,
    resume_from_chapter: int | None = None,
    completion_handler: Callable[[object], None] | None = None,
) -> None:
    def _handle_progress(event: str, payload: dict[str, Any]) -> None:
        changes = _build_task_progress_changes(event, payload)
        if changes:
            update_task(task_id, **changes)

    orchestrator = _build_writing_orchestrator_for_task(
        config,
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
            paused_str = ", ".join(str(chapter) for chapter in result.paused_chapters)
            update_task(task_id, message=f"继续执行后遇到质量门阻断，需自动修复或重试章节: {paused_str}")
        elif result.completed_chapters:
            completed_str = ", ".join(str(chapter) for chapter in result.completed_chapters)
            update_task(task_id, message=f"继续执行完成章节: {completed_str}")
        else:
            update_task(task_id, message="没有剩余章节需要继续执行。")

    run_orchestrator_task(
        task_id,
        orchestrator,
        lambda: orchestrator.continue_project(
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
    )
