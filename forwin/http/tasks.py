"""ForWin Web API – FastAPI interface for the novel generation system."""

from __future__ import annotations

import logging
import json
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import DBAPIError

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
from forwin.storage.db_errors import is_retryable_database_error


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

    runtime.task_center_service = TaskCenterService(
        get_session=lambda: _get_session(runtime),
        display_datetime=_display_datetime,
        new_stage_history_entry=_new_stage_history_entry,
        generation_task_from_row=_generation_task_from_row,
        config_provider=lambda: runtime.config,
        terminal_statuses=GENERATION_TERMINAL_STATUSES,
        terminal_stage_by_status=GENERATION_TERMINAL_STAGE_BY_STATUS,
    )
    return runtime.task_center_service


class GenerationTaskPersistenceError(RuntimeError):
    pass


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
            if not is_retryable_database_error(exc):
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


def _load_generation_task(
    runtime: HttpRuntime,
    task_id: str,
    *,
    include_deleted: bool = False,
) -> dict[str, Any] | None:
    return _get_task_center_service(runtime).load_generation_task(
        task_id,
        include_deleted=include_deleted,
    )


def _list_generation_tasks(
    runtime: HttpRuntime,
    limit: int,
) -> list[tuple[str, dict[str, Any]]]:
    return _get_task_center_service(runtime).list_generation_tasks(limit)


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
        and (
            task.get("continuation_pending")
            or not _task_is_terminal(str(task.get("status", "")))
        )
    )


def _task_is_pausable(task: dict[str, Any]) -> bool:
    return (
        str(task.get("task_kind", "generation")) == "generation"
        and not task.get("deleted")
        and not task.get("cancel_requested")
        and not task.get("pause_requested")
        and (
            task.get("continuation_pending")
            or str(task.get("status", ""))
            in {"queued", "starting", "running", "capacity_wait"}
        )
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
    execution = task.get("execution_payload") or {}
    return TaskSummaryResponse(
        long_run_mode=str(execution.get("long_run_mode") or "daily_serial"),
        isolated=bool(execution.get("isolated", False)),
        capacity_config_version=int(execution.get("capacity_config_version") or 0),
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
