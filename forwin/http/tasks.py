"""ForWin Web API – FastAPI interface for the novel generation system."""

from __future__ import annotations

import logging
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import DBAPIError, OperationalError

from forwin.application.task_center import TaskCenterService
from forwin.api_schema import (
    GenerationControlInfo,
    TaskCenterItemResponse,
    TaskSummaryResponse,
)
from forwin.models.task import GenerationTask
import forwin.models.phase  # noqa: F401
from forwin.http.request_support import (
    _coerce_int_list,
    _display_datetime,
    _get_session,
    _json_dump,
    _json_load_list,
    _json_load_object,
    _utcnow,
)
from forwin.http.runtime import (
    GENERATION_TERMINAL_STAGE_BY_STATUS,
    GENERATION_TERMINAL_STATUSES,
    HttpRuntime,
)


logger = logging.getLogger(__name__)


def _generation_task_conflict_message(project_id: str) -> str:
    return f"项目 {project_id} 已有运行中的生成任务，请先终止或等待当前任务完成。"


def _generation_task_from_row(row: GenerationTask) -> dict[str, Any]:
    return {
        "task_kind": str(row.task_kind or "generation"),
        "status": str(row.status or "queued"),
        "title": str(row.title or "").strip(),
        "subtitle": str(row.subtitle or "").strip(),
        "project_id": str(row.project_id or "").strip() or None,
        "extension_client_id": str(row.extension_client_id or "").strip(),
        "error": str(row.error_message or "").strip() or None,
        "message": str(row.message or ""),
        "current_stage": str(row.current_stage or "queued").strip(),
        "stage_history": _json_load_list(row.stage_history_json),
        "requested_chapters": int(row.requested_chapters or 0),
        "current_chapter": int(row.current_chapter or 0),
        "completed_chapters": _coerce_int_list(
            _json_load_list(row.completed_chapters_json)
        ),
        "failed_chapters": _coerce_int_list(_json_load_list(row.failed_chapters_json)),
        "paused_chapters": _coerce_int_list(_json_load_list(row.paused_chapters_json)),
        "frozen_artifacts": [
            str(item).strip()
            for item in _json_load_list(row.frozen_artifacts_json)
            if str(item).strip()
        ],
        "cancel_requested": bool(row.cancel_requested),
        "pause_requested": bool(getattr(row, "pause_requested", False)),
        "lease_owner": str(getattr(row, "lease_owner", "") or ""),
        "lease_expires_at": getattr(row, "lease_expires_at", None),
        "heartbeat_at": getattr(row, "heartbeat_at", None),
        "resume_from_chapter": int(getattr(row, "resume_from_chapter", 0) or 0),
        "run_until_chapter": int(getattr(row, "run_until_chapter", 0) or 0),
        "max_chapters": int(getattr(row, "max_chapters", 0) or 0),
        "execution_payload": _json_load_object(
            getattr(row, "execution_payload_json", "{}") or "{}"
        ),
        "deleted": row.deleted_at is not None,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "paused_at": getattr(row, "paused_at", None),
        "persistence_degraded": False,
        "persistence_error": None,
    }


def _apply_generation_task_to_row(
    row: GenerationTask,
    task: dict[str, Any],
    *,
    now: datetime | None = None,
) -> None:
    timestamp = now or _utcnow()
    row.task_kind = (
        str(task.get("task_kind", "generation") or "generation").strip() or "generation"
    )
    row.status = str(task.get("status", "queued") or "queued").strip() or "queued"
    row.title = str(task.get("title", "") or "").strip()
    row.subtitle = str(task.get("subtitle", "") or "").strip()
    row.project_id = str(task.get("project_id", "") or "").strip()
    row.extension_client_id = str(task.get("extension_client_id", "") or "").strip()
    row.error_message = str(task.get("error", "") or "")
    row.message = str(task.get("message", "") or "")
    row.current_stage = (
        str(task.get("current_stage", "queued") or "queued").strip() or "queued"
    )
    row.stage_history_json = _json_dump(task.get("stage_history", []), [])
    row.requested_chapters = int(task.get("requested_chapters", 0) or 0)
    row.current_chapter = int(task.get("current_chapter", 0) or 0)
    row.completed_chapters_json = _json_dump(
        _coerce_int_list(task.get("completed_chapters", [])), []
    )
    row.failed_chapters_json = _json_dump(
        _coerce_int_list(task.get("failed_chapters", [])), []
    )
    row.paused_chapters_json = _json_dump(
        _coerce_int_list(task.get("paused_chapters", [])), []
    )
    row.frozen_artifacts_json = _json_dump(
        [
            str(item).strip()
            for item in task.get("frozen_artifacts", [])
            if str(item).strip()
        ],
        [],
    )
    row.cancel_requested = bool(task.get("cancel_requested"))
    row.pause_requested = bool(task.get("pause_requested"))
    row.lease_owner = str(task.get("lease_owner", "") or "")
    row.lease_expires_at = task.get("lease_expires_at") or None
    row.heartbeat_at = task.get("heartbeat_at") or None
    row.resume_from_chapter = int(task.get("resume_from_chapter", 0) or 0)
    row.run_until_chapter = int(task.get("run_until_chapter", 0) or 0)
    row.max_chapters = int(task.get("max_chapters", 0) or 0)
    row.execution_payload_json = json.dumps(
        task.get("execution_payload", {}) or {},
        ensure_ascii=False,
    )
    if row.created_at is None:
        row.created_at = task.get("created_at") or timestamp
    row.updated_at = task.get("updated_at") or timestamp
    if row.started_at is None:
        row.started_at = task.get("started_at") or row.created_at or timestamp
    if task.get("deleted"):
        row.deleted_at = row.deleted_at or timestamp
    elif row.deleted_at is not None:
        row.deleted_at = None
    if _task_is_terminal(row.status):
        row.finished_at = task.get("finished_at") or row.finished_at or timestamp
        if row.status == "paused":
            row.paused_at = task.get("paused_at") or row.paused_at or timestamp
    elif row.status in {"starting", "running", "terminating"}:
        row.finished_at = None


def _sync_task_cache(
    runtime: HttpRuntime,
    task_id: str,
    task: dict[str, Any] | None,
) -> None:
    with runtime.tasks_lock:
        if task is None or task.get("deleted"):
            runtime.tasks.pop(task_id, None)
        else:
            runtime.tasks[task_id] = dict(task)


def _cached_generation_task(
    runtime: HttpRuntime,
    task_id: str,
) -> dict[str, Any] | None:
    with runtime.tasks_lock:
        task = runtime.tasks.get(task_id)
        return dict(task) if task is not None else None


def _coerce_task_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, str) and value.strip():
        try:
            timestamp = datetime.fromisoformat(value.strip())
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
    else:
        return datetime.min.replace(tzinfo=timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _get_task_center_service(runtime: HttpRuntime) -> TaskCenterService:
    if runtime.task_center_service is not None:
        return runtime.task_center_service

    def _iter_cached_generation_tasks() -> list[tuple[str, dict[str, Any]]]:
        with runtime.tasks_lock:
            return [(task_id, dict(task)) for task_id, task in runtime.tasks.items()]

    runtime.task_center_service = TaskCenterService(
        get_session=lambda: _get_session(runtime),
        has_db_session=lambda: runtime.session_factory is not None,
        prune_task_cache=lambda: _prune_tasks(runtime, include_db=False),
        utcnow=_utcnow,
        display_datetime=_display_datetime,
        coerce_task_datetime=_coerce_task_datetime,
        new_stage_history_entry=_new_stage_history_entry,
        cached_generation_task=lambda task_id: _cached_generation_task(
            runtime, task_id
        ),
        iter_cached_generation_tasks=_iter_cached_generation_tasks,
        prefer_cached_generation_task=_prefer_cached_generation_task,
        generation_task_from_row=_generation_task_from_row,
        config_provider=lambda: runtime.config,
        terminal_statuses=GENERATION_TERMINAL_STATUSES,
        terminal_stage_by_status=GENERATION_TERMINAL_STAGE_BY_STATUS,
    )
    return runtime.task_center_service


def _task_history_len(task: dict[str, Any] | None) -> int:
    if task is None:
        return 0
    history = task.get("stage_history", [])
    return len(history) if isinstance(history, list) else 0


def _prefer_cached_generation_task(
    persisted: dict[str, Any] | None,
    cached: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if cached is None:
        return persisted
    if persisted is None:
        return cached
    cached_updated = _coerce_task_datetime(cached.get("updated_at"))
    persisted_updated = _coerce_task_datetime(persisted.get("updated_at"))
    if cached_updated > persisted_updated:
        return cached
    if cached_updated == persisted_updated and _task_history_len(
        cached
    ) > _task_history_len(persisted):
        return cached
    return persisted


def _apply_task_visibility_rules(
    runtime: HttpRuntime,
    task: dict[str, Any] | None,
    *,
    include_deleted: bool,
) -> dict[str, Any] | None:
    return _get_task_center_service(runtime).apply_task_visibility_rules(
        task,
        include_deleted=include_deleted,
    )


class GenerationTaskPersistenceError(RuntimeError):
    pass


def _is_sqlite_locked_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


def _is_retryable_generation_task_db_error(exc: Exception) -> bool:
    if _is_sqlite_locked_error(exc):
        return True
    orig = getattr(exc, "orig", None)
    sqlstate = str(
        getattr(orig, "sqlstate", "") or getattr(orig, "pgcode", "") or ""
    ).strip()
    if sqlstate in {
        "40001",
        "40P01",
        "55P03",
        "57014",
        "08000",
        "08003",
        "08006",
        "08001",
    }:
        return True
    message = str(exc).lower()
    retryable_fragments = (
        "deadlock detected",
        "could not serialize access",
        "canceling statement due to lock timeout",
        "lock not available",
        "lock timeout",
        "connection refused",
        "connection not open",
        "server closed the connection",
        "terminating connection",
    )
    return any(fragment in message for fragment in retryable_fragments)


def _run_generation_task_db_write(
    operation,
    *,
    context: str,
    attempts: int = 5,
    delay: float = 0.25,
    raise_on_failure: bool = True,
) -> bool:
    final_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            operation()
            return True
        except DBAPIError as exc:
            if not _is_retryable_generation_task_db_error(exc):
                raise
            final_exc = exc
            if attempt == attempts:
                message = f"Generation task DB write failed after {attempts} retries in {context}"
                logger.error("%s: %s", message, exc, exc_info=True)
                if raise_on_failure:
                    raise GenerationTaskPersistenceError(message) from exc
                return False
            time.sleep(delay * attempt)
    if raise_on_failure:
        raise GenerationTaskPersistenceError(
            f"Generation task DB write failed after {attempts} retries in {context}"
        ) from final_exc
    return False


def _mark_task_persistence_degraded(
    runtime: HttpRuntime,
    task_id: str,
    task: dict[str, Any],
    exc: Exception,
) -> None:
    task["persistence_degraded"] = True
    task["persistence_error"] = str(exc)
    task["updated_at"] = _utcnow()
    _sync_task_cache(runtime, task_id, task)


def _clear_task_persistence_degraded(
    runtime: HttpRuntime,
    task_id: str,
    task: dict[str, Any],
) -> None:
    if task.get("persistence_degraded") or task.get("persistence_error"):
        task["persistence_degraded"] = False
        task["persistence_error"] = None
        _sync_task_cache(runtime, task_id, task)


def _prune_generation_tasks_db(
    runtime: HttpRuntime,
    now: datetime | None = None,
) -> None:
    if runtime.session_factory is None:
        return

    current = now or _utcnow()
    if runtime.last_generation_task_db_prune_at is not None:
        elapsed = (
            current - runtime.last_generation_task_db_prune_at
        ).total_seconds()
        if elapsed < runtime.task_db_prune_interval_seconds:
            return

    runtime.last_generation_task_db_prune_at = current
    cutoff = current - timedelta(seconds=runtime.task_retention_seconds)

    def _operation() -> None:
        with _get_session(runtime) as session:
            session.execute(
                delete(GenerationTask).where(
                    or_(
                        GenerationTask.deleted_at.is_not(None),
                        (
                            GenerationTask.status.in_(
                                tuple(GENERATION_TERMINAL_STATUSES)
                            )
                            & (GenerationTask.updated_at < cutoff)
                        ),
                    )
                )
            )
            total_rows = session.execute(
                select(func.count(GenerationTask.id)).where(
                    GenerationTask.deleted_at.is_(None)
                )
            ).scalar_one()
            overflow = max(0, int(total_rows or 0) - runtime.max_tasks)
            if overflow:
                overflow_ids = (
                    session.execute(
                        select(GenerationTask.id)
                        .where(
                            GenerationTask.deleted_at.is_(None),
                            GenerationTask.status.in_(
                                tuple(GENERATION_TERMINAL_STATUSES)
                            ),
                        )
                        .order_by(GenerationTask.updated_at.asc())
                        .limit(overflow)
                    )
                    .scalars()
                    .all()
                )
                if overflow_ids:
                    session.execute(
                        delete(GenerationTask).where(
                            GenerationTask.id.in_(overflow_ids)
                        )
                    )
            session.commit()

    _run_generation_task_db_write(
        _operation,
        context="generation_task_prune",
        attempts=2,
        delay=0.15,
        raise_on_failure=False,
    )


def _prune_tasks(runtime: HttpRuntime, *, include_db: bool = True) -> None:
    now = _utcnow()
    with runtime.tasks_lock:
        stale_ids = [
            task_id
            for task_id, task in runtime.tasks.items()
            if task.get("deleted")
            or (
                task.get("status") in GENERATION_TERMINAL_STATUSES
                and (now - task.get("updated_at", now)).total_seconds()
                > runtime.task_retention_seconds
            )
        ]
        for task_id in stale_ids:
            runtime.tasks.pop(task_id, None)

    if include_db:
        _prune_generation_tasks_db(runtime, now)


def _load_generation_task(
    runtime: HttpRuntime,
    task_id: str,
    *,
    include_deleted: bool = False,
) -> dict[str, Any] | None:
    try:
        return _get_task_center_service(runtime).load_generation_task(
            task_id,
            include_deleted=include_deleted,
        )
    except OperationalError as exc:
        if not _is_retryable_generation_task_db_error(exc):
            raise
        logger.warning(
            "Generation task read fell back to cache due to DB retryable error for %s",
            task_id,
        )
        return _apply_task_visibility_rules(
            runtime,
            _cached_generation_task(runtime, task_id),
            include_deleted=include_deleted,
        )


def recover_interrupted_generation_tasks(runtime: HttpRuntime) -> list[str]:
    if runtime.session_factory is None:
        return []

    now = _utcnow()
    recovered_ids: list[str] = []
    with _get_session(runtime) as session:
        rows = (
            session.execute(
                select(GenerationTask).where(
                    GenerationTask.deleted_at.is_(None),
                    GenerationTask.status.notin_(
                        tuple(GENERATION_TERMINAL_STATUSES)
                    ),
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            task = _generation_task_from_row(row)
            if task.get("cancel_requested"):
                task["status"] = "cancelled"
                task["current_stage"] = "cancelled"
                task["message"] = "服务重启时检测到终止请求，生成任务已取消。"
                task["error"] = None
            elif task.get("pause_requested"):
                task["status"] = "paused"
                task["current_stage"] = "paused"
                task["message"] = "服务重启时检测到暂停请求，生成任务已安全暂停。"
                task["error"] = None
                task["paused_at"] = now
            elif task.get("failed_chapters"):
                task["status"] = "failed"
                task["current_stage"] = "failed"
                task["message"] = "服务重启前生成任务已有失败章节，需修复后再继续。"
                task["error"] = "generation_failed_chapter_blocker_after_restart"
                task["finished_at"] = now
            else:
                task["status"] = "queued"
                task["current_stage"] = "queued"
                task["message"] = (
                    "服务重启后生成任务已重新排队，等待 durable worker 接管。"
                )
                task["error"] = None
                task["lease_owner"] = ""
                task["lease_expires_at"] = now
                task["finished_at"] = None
            task["updated_at"] = now
            if (
                str(task.get("current_stage", "")).strip()
                != str(row.current_stage or "").strip()
            ):
                history = list(task.get("stage_history", []))
                history.append(
                    _new_stage_history_entry(
                        str(task.get("current_stage", "")).strip(),
                        now=now,
                        current_chapter=int(task.get("current_chapter", 0) or 0),
                        message=str(task.get("message", "")).strip(),
                    )
                )
                task["stage_history"] = history
            if task.get("status") in GENERATION_TERMINAL_STATUSES:
                task["finished_at"] = now
            _apply_generation_task_to_row(row, task, now=now)
            recovered_ids.append(row.id)
        session.commit()
    return recovered_ids


def _new_stage_history_entry(
    stage: str,
    *,
    now: datetime | None = None,
    current_chapter: int = 0,
    message: str = "",
) -> dict[str, Any]:
    timestamp = now or _utcnow()
    return {
        "stage": stage,
        "at": _display_datetime(timestamp),
        "chapter": int(current_chapter or 0),
        "message": str(message or "").strip(),
    }


def _task_is_terminal(status: str) -> bool:
    return status in GENERATION_TERMINAL_STATUSES


def _task_is_terminable(task: dict[str, Any]) -> bool:
    return (
        not task.get("deleted")
        and not task.get("cancel_requested")
        and not _task_is_terminal(str(task.get("status", "")))
    )


def _task_is_pausable(task: dict[str, Any]) -> bool:
    return (
        str(task.get("task_kind", "generation")) == "generation"
        and not task.get("deleted")
        and not task.get("cancel_requested")
        and not task.get("pause_requested")
        and str(task.get("status", "")) in {"queued", "starting", "running"}
    )


def _task_is_resumable(task: dict[str, Any]) -> bool:
    return (
        str(task.get("task_kind", "generation")) == "generation"
        and not task.get("deleted")
        and str(task.get("status", "")) == "paused"
        and bool(str(task.get("project_id", "") or "").strip())
    )


def _task_is_deletable(task: dict[str, Any]) -> bool:
    return not task.get("deleted") and _task_is_terminal(str(task.get("status", "")))


def _task_interrupted_by_restart(task: dict[str, Any]) -> bool:
    return str(task.get("error") or "") == "generation_interrupted_after_restart"


def _task_recovery_suggestion(task: dict[str, Any]) -> str:
    if _task_interrupted_by_restart(task):
        if task.get("frozen_artifacts"):
            return "check_artifact"
        if task.get("paused_chapters"):
            return "needs_review"
        if str(task.get("project_id", "") or "").strip():
            return "rerun_or_continue"
        return "rerun"
    if str(task.get("status", "")) == "paused":
        return "continue_available" if _task_is_resumable(task) else "needs_review"
    if task.get("failed_chapters"):
        return "rerun"
    if task.get("frozen_artifacts"):
        return "check_artifact"
    return ""


def _serialize_task(task_id: str, task: dict[str, Any]) -> TaskSummaryResponse:
    accepted = list(task.get("completed_chapters", []))
    pending_review = list(task.get("paused_chapters", []))
    generated = list(dict.fromkeys([*accepted, *pending_review]))
    return TaskSummaryResponse(
        task_kind=str(task.get("task_kind", "generation")),
        task_id=task_id,
        status=task["status"],
        title=str(task.get("title", "")).strip(),
        subtitle=str(task.get("subtitle", "")).strip(),
        project_id=task.get("project_id"),
        extension_client_id=str(task.get("extension_client_id", "")).strip(),
        error=task.get("error"),
        message=task.get("message", ""),
        current_stage=str(task.get("current_stage", "queued")).strip(),
        stage_history=list(task.get("stage_history", [])),
        requested_chapters=int(task.get("requested_chapters", 0) or 0),
        current_chapter=int(task.get("current_chapter", 0) or 0),
        completed_chapters=list(task.get("completed_chapters", [])),
        failed_chapters=task.get("failed_chapters", []),
        paused_chapters=task.get("paused_chapters", []),
        frozen_artifacts=task.get("frozen_artifacts", []),
        pause_requested=bool(task.get("pause_requested")),
        lease_owner=str(task.get("lease_owner", "") or ""),
        lease_expires_at=_display_datetime(task.get("lease_expires_at")),
        heartbeat_at=_display_datetime(task.get("heartbeat_at")),
        resume_from_chapter=int(task.get("resume_from_chapter", 0) or 0),
        run_until_chapter=int(task.get("run_until_chapter", 0) or 0),
        max_chapters=int(task.get("max_chapters", 0) or 0),
        pausable=_task_is_pausable(task),
        resumable=_task_is_resumable(task),
        generation_control=GenerationControlInfo(
            current_stage=str(task.get("current_stage", "queued")).strip(),
            current_chapter=int(task.get("current_chapter", 0) or 0),
            accepted_chapters=accepted,
            drafted_chapters=pending_review,
            generated_chapters=generated,
            failed_chapters=list(task.get("failed_chapters", [])),
            pending_review_chapters=pending_review,
            can_pause=_task_is_pausable(task),
            can_resume=_task_is_resumable(task),
            pause_requested=bool(task.get("pause_requested")),
        ),
        terminable=_task_is_terminable(task),
        deletable=_task_is_deletable(task),
        interrupted_by_restart=_task_interrupted_by_restart(task),
        recovery_suggestion=_task_recovery_suggestion(task),
        persistence_degraded=bool(task.get("persistence_degraded")),
        persistence_error=(
            str(task.get("persistence_error")).strip()
            if task.get("persistence_error")
            else None
        ),
        created_at=_display_datetime(task.get("created_at")),
        updated_at=_display_datetime(task.get("updated_at")),
    )


def _serialize_generation_task_center_item(
    task_id: str, task: dict[str, Any]
) -> TaskCenterItemResponse:
    serialized = _serialize_task(task_id, task)
    return TaskCenterItemResponse.model_validate(serialized.model_dump())


def _serialize_upload_task_center_item(
    payload: dict[str, Any],
) -> TaskCenterItemResponse:
    return TaskCenterItemResponse(
        task_kind="upload",
        task_id=str(payload.get("job_id", "")).strip(),
        status=str(payload.get("status", "")).strip(),
        title=str(payload.get("book_name", "")).strip()
        or str(payload.get("display_name", "")).strip(),
        subtitle=str(payload.get("chapter_title", "")).strip(),
        project_id=str(payload.get("project_id", "")).strip() or None,
        extension_client_id=str(payload.get("extension_client_id", "")).strip(),
        message=str(payload.get("message", "")).strip(),
        error=str(payload.get("error", "")).strip(),
        current_url=str(payload.get("current_url", "")).strip(),
        upload_url=payload.get("upload_url"),
        platform=str(payload.get("platform", "")).strip(),
        display_name=str(payload.get("display_name", "")).strip(),
        publish=payload.get("publish"),
        result_payload=payload.get("result_payload", {}) or {},
        created_at=str(payload.get("created_at", "")).strip(),
        updated_at=str(payload.get("updated_at", "")).strip(),
        claimed_at=str(payload.get("claimed_at", "")).strip(),
        started_at=str(payload.get("started_at", "")).strip(),
        finished_at=str(payload.get("finished_at", "")).strip(),
        abort_requested=bool(payload.get("abort_requested")),
        terminable=bool(payload.get("terminable")),
        deletable=bool(payload.get("deletable")),
    )


def _parse_project_task_id(runtime: HttpRuntime, task_id: str) -> str | None:
    return _get_task_center_service(runtime).parse_project_task_id(task_id)


def _list_project_backed_task_items(
    runtime: HttpRuntime,
    limit: int,
) -> list[TaskCenterItemResponse]:
    return _get_task_center_service(runtime).list_project_backed_task_items(limit)


def _get_project_backed_task_item_or_404(
    runtime: HttpRuntime,
    task_id: str,
) -> TaskCenterItemResponse:
    return _get_task_center_service(runtime).get_project_backed_task_item_or_404(
        task_id
    )


__all__ = [name for name in globals() if not name.startswith("__")]
