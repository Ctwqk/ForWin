"""ForWin Web API – FastAPI interface for the novel generation system."""
from __future__ import annotations

import logging
import os
import threading
import uuid
import json
import io
import inspect
import time
import zipfile
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.exc import OperationalError

from forwin.api_pages import render_home_page, render_publishers_page
from forwin.api_project_payloads import (
    build_project_detail,
    build_project_summaries,
    build_provisional_band_detail,
    latest_provisional_band_execution,
    normalize_project_automation,
)
from forwin.api_runtime import (
    build_home_page_settings,
    build_runtime_config,
    build_saved_runtime_config,
    copy_config,
    run_continue_project_with_config,
    run_generation_with_config,
)
from forwin.api_schemas import (
    BandCheckpointApproveRequest,
    BandCheckpointDetail,
    BandExperienceOverrideRequest,
    BandExperienceOverrideResponse,
    ActiveGenerationTaskCheckResponse,
    CausalReplayResponse,
    DecisionEventsResponse,
    BulkDeleteResponse,
    ChapterDetail,
    ChapterInfo,
    ChapterReviewApproveRequest,
    ChapterReviewApproveResponse,
    ChapterReviewDetail,
    ChapterReviewIssueInfo,
    CommentSyncJobResultRequest,
    EntityInfo,
    ExtensionClaimCommentSyncJobRequest,
    ExtensionClaimCommentSyncJobResponse,
    ExtensionClaimUploadJobRequest,
    ExtensionClaimUploadJobResponse,
    ExtensionCommentsBatchRequest,
    ExtensionCommentsBatchResponse,
    ExtensionHeartbeatRequest,
    ExtensionHeartbeatResponse,
    ExtensionPlatformHeartbeat,
    ExtensionBrowserSessionResponse,
    ExtensionSessionSyncRequest,
    ExtensionSessionSyncResponse,
    GenerateRequest,
    GenerationControlInfo,
    LLMDefaultProfileRequest,
    LLMPreferencesRequest,
    LLMProfileUpsertRequest,
    LLMSettingsRequest,
    LLMSettingsResponse,
    ModelProfile,
    NarrativeConstraintCreateRequest,
    NarrativeConstraintUpdateRequest,
    NarrativeConstraintsResponse,
    GovernanceInsightsResponse,
    ManualCheckpointRequest,
    ProjectArcSnapshotFields,
    ProjectChapterPublishRequest,
    ProjectCreateRequest,
    ProjectCreateResponse,
    ProjectContinueGenerationRequest,
    ProjectAutomationSettings,
    ProjectAutomationUpdateRequest,
    ProjectAutomationUpdateResponse,
    ProjectBulkDeleteRequest,
    ProjectDeleteResponse,
    ProjectDetail,
    ProjectGovernanceResponse,
    ProjectGovernanceUpdateRequest,
    ProjectSummary,
    ProvisionalBandDetail,
    ProvisionalChapterLedgerInfo,
    PublisherCommentSyncJobRequest,
    PublisherCommentSyncJobResponse,
    PublisherPlatformInfo,
    PublisherRawCommentInput,
    PublisherUploadJobCreateRequest,
    PublisherUploadJobResponse,
    TaskResponse,
    TaskCenterItemResponse,
    TaskBulkDeleteRequest,
    TaskMutationResponse,
    TaskContractResponse,
    TaskContractUpdateRequest,
    TaskSummaryResponse,
    ThreadInfo,
    TropeTemplateInfo,
    TropeRegistrySummaryResponse,
    TropeTemplateValidationRequest,
    TropeTemplateValidationResponse,
    UploadJobResultRequest,
    LintSignalInfo,
)
from forwin.config import Config
from forwin.governance import (
    BandCheckpointIssueInfo,
    CONSTRAINT_LEVELS,
    CONSTRAINT_STATUSES,
    CONSTRAINT_TYPES,
    DecisionEventType,
    DecisionEventInfo,
    NarrativeConstraintInfo,
    ensure_decision_event_type,
    issue_group_for_issue,
    load_plan_task_contract,
    new_project_governance,
    normalize_project_governance,
    plan_task_contract_to_json,
)
from forwin.models.base import Base, get_engine, get_session_factory, init_db
from forwin.models.project import Project, ChapterPlan, ArcPlanVersion
from forwin.models.entity import Entity
from forwin.models.event import CanonEvent, EventEntityLink
from forwin.models.governance import BandCheckpoint, DecisionEvent, NarrativeConstraint
from forwin.models.publisher import PublisherCommentSyncJob, PublisherConnectionState, PublisherExtensionClient, PublisherRawComment, PublisherUploadJob
from forwin.models.thread import PlotThread
from forwin.models.task import GenerationTask
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.phase import (
    BandExperiencePlan,
    ChapterRewriteAttempt,
    ProvisionalBandExecution,
)
from forwin.models.timeline import ChapterTimeline, StoryTimePoint
from forwin.models.phase4 import NPCIntentSnapshot
import forwin.models.phase  # noqa: F401
from forwin.protocol.experience import BandDelightSchedule
from forwin.protocol.trope_library import (
    TROPE_TEMPLATE_LIBRARY,
    trope_registry_summary,
    validate_trope_template_payload,
)
from forwin.state.repo import StateRepository
from forwin.orchestrator.loop import WritingOrchestrator
from forwin.orchestrator.feedback_aggregator import derive_action_effectiveness
from forwin.publishers import PublisherManager
from forwin.runtime_settings import RuntimeSettingsStore
from forwin.state.query_helpers import load_latest_drafts_by_plan_id

logger = logging.getLogger(__name__)

# Backwards-compatible aliases for tests and local integrations while api.py is being split.
_build_runtime_config = build_runtime_config
_build_saved_runtime_config = build_saved_runtime_config
_run_generation_with_config = run_generation_with_config
_run_continue_project_with_config = run_continue_project_with_config

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_config: Config | None = None
_engine = None
_SessionFactory = None
_orchestrator: WritingOrchestrator | None = None
_publisher_manager: PublisherManager | None = None
_runtime_settings: RuntimeSettingsStore | None = None
_automation_scheduler_thread: threading.Thread | None = None
_automation_scheduler_stop = threading.Event()

# Runtime cache for live generation threads. Persistent task state is stored in DB.
_tasks: dict[str, dict] = {}
_tasks_lock = threading.Lock()
_TASK_RETENTION_SECONDS = 6 * 60 * 60
_MAX_TASKS = 256
_TASK_DB_PRUNE_INTERVAL_SECONDS = 60
_DISPLAY_TZ = ZoneInfo("America/Los_Angeles")
_last_generation_task_db_prune_at: datetime | None = None
_GENERATION_TERMINAL_STATUSES = {"completed", "partial_failed", "failed", "needs_review", "cancelled", "paused"}
_GENERATION_TERMINAL_STAGE_BY_STATUS = {
    "completed": "completed",
    "partial_failed": "failed",
    "failed": "failed",
    "needs_review": "paused_for_review",
    "cancelled": "cancelled",
    "paused": "paused",
}
_UPLOAD_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
_GENERATION_STAGE_ORDER = [
    "queued",
    "planning_arc",
    "creating_project",
    "resolving_arc_envelope",
    "running_provisional_preview",
    "provisional_failed",
    "assembling_context",
    "writing_chapter",
    "chapter_failed",
    "continuity_review",
    "applying_canon",
    "running_post_acceptance",
    "paused_for_review",
    "completed",
    "failed",
    "terminating",
    "cancelled",
]


class ActiveGenerationTaskError(RuntimeError):
    pass


def _get_session():
    return _SessionFactory()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _display_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def _json_load_list(raw: str | None) -> list[Any]:
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _json_load_object(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_dump(value: Any, fallback: Any) -> str:
    normalized = value if isinstance(value, type(fallback)) else fallback
    return json.dumps(normalized, ensure_ascii=False)


def _coerce_int_list(value: Any) -> list[int]:
    numbers: list[int] = []
    for item in value if isinstance(value, list) else []:
        try:
            numbers.append(int(item))
        except (TypeError, ValueError):
            continue
    return numbers


def _generation_task_from_row(row: GenerationTask) -> dict[str, Any]:
    return {
        "task_kind": str(row.task_kind or "generation"),
        "status": str(row.status or "starting"),
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
        "completed_chapters": _coerce_int_list(_json_load_list(row.completed_chapters_json)),
        "failed_chapters": _coerce_int_list(_json_load_list(row.failed_chapters_json)),
        "paused_chapters": _coerce_int_list(_json_load_list(row.paused_chapters_json)),
        "frozen_artifacts": [
            str(item).strip()
            for item in _json_load_list(row.frozen_artifacts_json)
            if str(item).strip()
        ],
        "cancel_requested": bool(row.cancel_requested),
        "pause_requested": bool(getattr(row, "pause_requested", False)),
        "deleted": row.deleted_at is not None,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "paused_at": getattr(row, "paused_at", None),
    }


def _apply_generation_task_to_row(
    row: GenerationTask,
    task: dict[str, Any],
    *,
    now: datetime | None = None,
) -> None:
    timestamp = now or _utcnow()
    row.task_kind = str(task.get("task_kind", "generation") or "generation").strip() or "generation"
    row.status = str(task.get("status", "starting") or "starting").strip() or "starting"
    row.title = str(task.get("title", "") or "").strip()
    row.subtitle = str(task.get("subtitle", "") or "").strip()
    row.project_id = str(task.get("project_id", "") or "").strip()
    row.extension_client_id = str(task.get("extension_client_id", "") or "").strip()
    row.error_message = str(task.get("error", "") or "")
    row.message = str(task.get("message", "") or "")
    row.current_stage = str(task.get("current_stage", "queued") or "queued").strip() or "queued"
    row.stage_history_json = _json_dump(task.get("stage_history", []), [])
    row.requested_chapters = int(task.get("requested_chapters", 0) or 0)
    row.current_chapter = int(task.get("current_chapter", 0) or 0)
    row.completed_chapters_json = _json_dump(_coerce_int_list(task.get("completed_chapters", [])), [])
    row.failed_chapters_json = _json_dump(_coerce_int_list(task.get("failed_chapters", [])), [])
    row.paused_chapters_json = _json_dump(_coerce_int_list(task.get("paused_chapters", [])), [])
    row.frozen_artifacts_json = _json_dump(
        [str(item).strip() for item in task.get("frozen_artifacts", []) if str(item).strip()],
        [],
    )
    row.cancel_requested = bool(task.get("cancel_requested"))
    row.pause_requested = bool(task.get("pause_requested"))
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


def _sync_task_cache(task_id: str, task: dict[str, Any] | None) -> None:
    with _tasks_lock:
        if task is None or task.get("deleted"):
            _tasks.pop(task_id, None)
        else:
            _tasks[task_id] = dict(task)


def _cached_generation_task(task_id: str) -> dict[str, Any] | None:
    with _tasks_lock:
        task = _tasks.get(task_id)
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
    if cached_updated == persisted_updated and _task_history_len(cached) > _task_history_len(persisted):
        return cached
    return persisted


def _task_has_stage(task: dict[str, Any], stage: str) -> bool:
    history = task.get("stage_history", [])
    if not isinstance(history, list):
        return False
    return any(str(entry.get("stage", "")).strip() == stage for entry in history if isinstance(entry, dict))


def _normalize_loaded_generation_task(task: dict[str, Any]) -> dict[str, Any]:
    status = str(task.get("status", "")).strip()
    expected_stage = _GENERATION_TERMINAL_STAGE_BY_STATUS.get(status)
    if not expected_stage:
        return task
    current_stage = str(task.get("current_stage", "")).strip()
    if current_stage == expected_stage:
        return task
    normalized = dict(task)
    normalized["current_stage"] = expected_stage
    history = list(normalized.get("stage_history", []))
    if not history or str(history[-1].get("stage", "")).strip() != expected_stage:
        history.append(
            _new_stage_history_entry(
                expected_stage,
                now=normalized.get("updated_at") if isinstance(normalized.get("updated_at"), datetime) else None,
                current_chapter=int(normalized.get("current_chapter", 0) or 0),
                message=str(normalized.get("message", "")).strip(),
            )
        )
        normalized["stage_history"] = history
    return normalized


def _apply_task_visibility_rules(
    task: dict[str, Any] | None,
    *,
    include_deleted: bool,
) -> dict[str, Any] | None:
    if task is None:
        return None
    normalized = _normalize_loaded_generation_task(task)
    if normalized.get("deleted") and not include_deleted:
        return None
    return normalized


def _augment_task_with_provisional_history(session, task: dict[str, Any]) -> dict[str, Any]:
    project_id = str(task.get("project_id", "") or "").strip()
    if not project_id or _task_has_stage(task, "running_provisional_preview"):
        return task

    created_at = _coerce_task_datetime(task.get("created_at"))
    finished_at = _coerce_task_datetime(task.get("finished_at"))
    updated_at = _coerce_task_datetime(task.get("updated_at"))
    window_end = max(finished_at, updated_at)
    if created_at == datetime.min.replace(tzinfo=timezone.utc):
        return task
    if window_end == datetime.min.replace(tzinfo=timezone.utc):
        window_end = _utcnow()

    execution = session.execute(
        select(ProvisionalBandExecution)
        .where(
            ProvisionalBandExecution.project_id == project_id,
            ProvisionalBandExecution.created_at >= created_at - timedelta(seconds=5),
            ProvisionalBandExecution.created_at <= window_end + timedelta(seconds=5),
        )
        .order_by(ProvisionalBandExecution.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()
    if execution is None:
        return task

    try:
        chapter_numbers = json.loads(execution.chapter_numbers_json or "[]")
    except json.JSONDecodeError:
        chapter_numbers = []
    chapter = int(chapter_numbers[0]) if chapter_numbers else 0
    augmented = dict(task)
    history = list(augmented.get("stage_history", []))
    history.append(
        _new_stage_history_entry(
            "running_provisional_preview",
            now=execution.created_at,
            current_chapter=chapter,
            message=str(augmented.get("message", "")).strip(),
        )
    )
    if (
        str(execution.aggregate_verdict or "").strip().lower() == "fail"
        or int(execution.failure_count or 0) > 0
    ) and not _task_has_stage(augmented, "provisional_failed"):
        history.append(
            _new_stage_history_entry(
                "provisional_failed",
                now=execution.created_at,
                current_chapter=chapter,
                message="Provisional 预演失败，已阻断正式写作。",
            )
        )
    augmented["stage_history"] = history
    return augmented


def _is_sqlite_locked_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


def _run_generation_task_db_write(operation, *, context: str, attempts: int = 5, delay: float = 0.25) -> bool:
    for attempt in range(1, attempts + 1):
        try:
            operation()
            return True
        except OperationalError as exc:
            if not _is_sqlite_locked_error(exc):
                raise
            if attempt == attempts:
                logger.warning(
                    "Generation task DB write skipped after %d lock retries in %s: %s",
                    attempts,
                    context,
                    exc,
                )
                return False
            time.sleep(delay * attempt)
    return False


def _prune_generation_tasks_db(now: datetime | None = None) -> None:
    global _last_generation_task_db_prune_at
    if _SessionFactory is None:
        return

    current = now or _utcnow()
    if _last_generation_task_db_prune_at is not None:
        elapsed = (current - _last_generation_task_db_prune_at).total_seconds()
        if elapsed < _TASK_DB_PRUNE_INTERVAL_SECONDS:
            return

    _last_generation_task_db_prune_at = current
    cutoff = current - timedelta(seconds=_TASK_RETENTION_SECONDS)

    def _operation() -> None:
        with _get_session() as session:
            session.execute(
                delete(GenerationTask).where(
                    or_(
                        GenerationTask.deleted_at.is_not(None),
                        (
                            GenerationTask.status.in_(tuple(_GENERATION_TERMINAL_STATUSES))
                            & (GenerationTask.updated_at < cutoff)
                        ),
                    )
                )
            )
            total_rows = session.execute(
                select(func.count(GenerationTask.id)).where(GenerationTask.deleted_at.is_(None))
            ).scalar_one()
            overflow = max(0, int(total_rows or 0) - _MAX_TASKS)
            if overflow:
                overflow_ids = session.execute(
                    select(GenerationTask.id)
                    .where(
                        GenerationTask.deleted_at.is_(None),
                        GenerationTask.status.in_(tuple(_GENERATION_TERMINAL_STATUSES)),
                    )
                    .order_by(GenerationTask.updated_at.asc())
                    .limit(overflow)
                ).scalars().all()
                if overflow_ids:
                    session.execute(delete(GenerationTask).where(GenerationTask.id.in_(overflow_ids)))
            session.commit()

    _run_generation_task_db_write(_operation, context="generation_task_prune", attempts=2, delay=0.15)


def _prune_tasks() -> None:
    now = _utcnow()
    with _tasks_lock:
        stale_ids = [
            task_id
            for task_id, task in _tasks.items()
            if task.get("deleted")
            or (
                task.get("status") in _GENERATION_TERMINAL_STATUSES
                and (now - task.get("updated_at", now)).total_seconds() > _TASK_RETENTION_SECONDS
            )
        ]
        for task_id in stale_ids:
            _tasks.pop(task_id, None)

    _prune_generation_tasks_db(now)


def _load_generation_task(task_id: str, *, include_deleted: bool = False) -> dict[str, Any] | None:
    if _SessionFactory is None:
        return _apply_task_visibility_rules(
            _cached_generation_task(task_id),
            include_deleted=include_deleted,
        )

    try:
        with _get_session() as session:
            cached = _cached_generation_task(task_id)
            row = session.get(GenerationTask, task_id)
            persisted = _generation_task_from_row(row) if row is not None else None
            task = _prefer_cached_generation_task(persisted, cached)
            if task is not None:
                task = _augment_task_with_provisional_history(session, task)
            return _apply_task_visibility_rules(task, include_deleted=include_deleted)
    except OperationalError as exc:
        if not _is_sqlite_locked_error(exc):
            raise
        logger.warning("Generation task read fell back to cache due to DB lock for %s", task_id)
        return _apply_task_visibility_rules(
            _cached_generation_task(task_id),
            include_deleted=include_deleted,
        )


def _persist_generation_task(task_id: str, task: dict[str, Any]) -> None:
    _sync_task_cache(task_id, task)
    if _SessionFactory is None:
        return

    def _operation() -> None:
        with _get_session() as session:
            row = session.get(GenerationTask, task_id)
            if row is None:
                row = GenerationTask(id=task_id)
            _apply_generation_task_to_row(row, task)
            session.add(row)
            session.commit()

    _run_generation_task_db_write(_operation, context=f"persist_generation_task:{task_id}")


def _recover_interrupted_generation_tasks() -> list[str]:
    if _SessionFactory is None:
        return []

    now = _utcnow()
    recovered_ids: list[str] = []
    with _get_session() as session:
        rows = session.execute(
            select(GenerationTask).where(
                GenerationTask.deleted_at.is_(None),
                GenerationTask.status.notin_(tuple(_GENERATION_TERMINAL_STATUSES)),
            )
        ).scalars().all()
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
            else:
                task["status"] = "failed"
                task["current_stage"] = "failed"
                task["message"] = "服务重启前生成任务中断，已标记为失败。"
                task["error"] = "generation_interrupted_after_restart"
            task["updated_at"] = now
            if str(task.get("current_stage", "")).strip() != str(row.current_stage or "").strip():
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
    return status in _GENERATION_TERMINAL_STATUSES


def _task_is_terminable(task: dict[str, Any]) -> bool:
    return not task.get("deleted") and not task.get("cancel_requested") and not _task_is_terminal(str(task.get("status", "")))


def _task_is_pausable(task: dict[str, Any]) -> bool:
    return (
        str(task.get("task_kind", "generation")) == "generation"
        and not task.get("deleted")
        and not task.get("cancel_requested")
        and not task.get("pause_requested")
        and str(task.get("status", "")) in {"starting", "running"}
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


def _create_task_record(
    message: str = "",
    *,
    title: str = "",
    subtitle: str = "",
    requested_chapters: int = 0,
    task_kind: str = "generation",
) -> dict[str, Any]:
    now = _utcnow()
    _prune_tasks()
    return {
        "task_kind": task_kind,
        "status": "starting",
        "title": title,
        "subtitle": subtitle,
        "project_id": None,
        "extension_client_id": "",
        "error": None,
        "message": message,
        "current_stage": "queued",
        "stage_history": [_new_stage_history_entry("queued", now=now, message=message)],
        "requested_chapters": int(requested_chapters or 0),
        "current_chapter": 0,
        "completed_chapters": [],
        "failed_chapters": [],
        "paused_chapters": [],
        "frozen_artifacts": [],
        "cancel_requested": False,
        "pause_requested": False,
        "deleted": False,
        "created_at": now,
        "updated_at": now,
    }


def _serialize_task(task_id: str, task: dict[str, Any]) -> TaskSummaryResponse:
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
        pausable=_task_is_pausable(task),
        resumable=_task_is_resumable(task),
        generation_control=GenerationControlInfo(
            current_stage=str(task.get("current_stage", "queued")).strip(),
            current_chapter=int(task.get("current_chapter", 0) or 0),
            accepted_chapters=list(task.get("completed_chapters", [])),
            failed_chapters=list(task.get("failed_chapters", [])),
            pending_review_chapters=list(task.get("paused_chapters", [])),
            can_pause=_task_is_pausable(task),
            can_resume=_task_is_resumable(task),
            pause_requested=bool(task.get("pause_requested")),
        ),
        terminable=_task_is_terminable(task),
        deletable=_task_is_deletable(task),
        interrupted_by_restart=_task_interrupted_by_restart(task),
        recovery_suggestion=_task_recovery_suggestion(task),
        created_at=_display_datetime(task.get("created_at")),
        updated_at=_display_datetime(task.get("updated_at")),
    )


def _serialize_generation_task_center_item(task_id: str, task: dict[str, Any]) -> TaskCenterItemResponse:
    serialized = _serialize_task(task_id, task)
    return TaskCenterItemResponse.model_validate(serialized.model_dump())


def _serialize_upload_task_center_item(payload: dict[str, Any]) -> TaskCenterItemResponse:
    return TaskCenterItemResponse(
        task_kind="upload",
        task_id=str(payload.get("job_id", "")).strip(),
        status=str(payload.get("status", "")).strip(),
        title=str(payload.get("book_name", "")).strip() or str(payload.get("display_name", "")).strip(),
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


def _project_task_id(project_id: str) -> str:
    return f"project-{project_id}"


def _parse_project_task_id(task_id: str) -> str | None:
    normalized = str(task_id or "").strip()
    if not normalized.startswith("project-"):
        return None
    project_id = normalized[len("project-"):].strip()
    return project_id or None


def _load_project_task_center_plans(session, project_ids: list[str]) -> dict[str, list[tuple[int, str]]]:
    ids = [str(project_id or "").strip() for project_id in project_ids if str(project_id or "").strip()]
    if not ids:
        return {}

    rows = session.execute(
        select(
            ChapterPlan.project_id,
            ChapterPlan.chapter_number,
            ChapterPlan.status,
        )
        .where(ChapterPlan.project_id.in_(ids))
        .order_by(ChapterPlan.project_id.asc(), ChapterPlan.chapter_number.asc())
    ).all()

    grouped: dict[str, list[tuple[int, str]]] = {project_id: [] for project_id in ids}
    for project_id, chapter_number, status in rows:
        grouped.setdefault(str(project_id), []).append(
            (int(chapter_number or 0), str(status or ""))
        )
    return grouped


def _build_project_task_center_item(
    project: Project,
    plans: list[tuple[int, str]],
) -> TaskCenterItemResponse:
    requested = len(plans)
    completed = [
        chapter_number
        for chapter_number, status in plans
        if status in {"accepted", "drafted"}
    ]
    failed = [chapter_number for chapter_number, status in plans if status == "failed"]
    paused = [chapter_number for chapter_number, status in plans if status == "needs_review"]
    if requested == 0:
        status = "created"
        current_stage = "queued"
    elif paused:
        status = "needs_review"
        current_stage = "paused_for_review"
    elif failed and not completed:
        status = "failed"
        current_stage = "failed"
    elif failed:
        status = "partial_failed"
        current_stage = "failed"
    else:
        status = "completed"
        current_stage = "completed"
    current_chapter = max(completed + failed + paused, default=0)
    message = "书本已创建，当前没有活跃生成任务。" if requested == 0 else "项目入口（当前没有活跃生成任务）"
    planned = [chapter_number for chapter_number, status in plans if status == "planned"]
    can_resume = bool((planned or failed) and not paused)
    stage_history = [
        _new_stage_history_entry(
            current_stage,
            now=project.updated_at,
            current_chapter=current_chapter,
            message=message,
        )
    ]
    return TaskCenterItemResponse(
        task_kind="generation",
        task_id=_project_task_id(project.id),
        status=status,
        title=project.title,
        subtitle=f"书本入口 · {project.genre}",
        project_id=project.id,
        message=message,
        current_stage=current_stage,
        stage_history=stage_history,
        requested_chapters=requested,
        current_chapter=current_chapter,
        completed_chapters=completed,
        failed_chapters=failed,
        paused_chapters=paused,
        generation_control=GenerationControlInfo(
            plan_state="none" if requested == 0 else status,
            writing_state="not_started" if not completed else "completed" if len(completed) == requested else "started",
            review_state="pending" if paused else "none",
            current_stage=current_stage,
            current_chapter=current_chapter,
            next_chapter=min(planned + failed, default=0),
            accepted_chapters=completed,
            planned_chapters=planned,
            failed_chapters=failed,
            pending_review_chapters=paused,
            can_resume=can_resume,
            review_interval_chapters=max(0, int(_config.review_interval_chapters if _config else 0)),
        ),
        resumable=can_resume,
        created_at=_display_datetime(project.created_at),
        updated_at=_display_datetime(project.updated_at),
        terminable=False,
        deletable=False,
    )


def _list_project_backed_task_items(limit: int) -> list[TaskCenterItemResponse]:
    live_project_ids: set[str] = set()
    if _SessionFactory is not None:
        with _get_session() as session:
            live_project_ids = {
                str(project_id).strip()
                for project_id in session.execute(
                    select(GenerationTask.project_id).where(
                        GenerationTask.deleted_at.is_(None),
                        GenerationTask.project_id != "",
                        GenerationTask.status.notin_(tuple(_GENERATION_TERMINAL_STATUSES)),
                    )
                ).scalars().all()
                if str(project_id).strip()
            }
    session = _get_session()
    try:
        projects = session.execute(
            select(Project).order_by(Project.updated_at.desc()).limit(max(1, min(int(limit or 50), 200)))
        ).scalars().all()
        plans_by_project = _load_project_task_center_plans(
            session,
            [project.id for project in projects],
        )
        items: list[TaskCenterItemResponse] = []
        for project in projects:
            if project.id in live_project_ids:
                continue
            items.append(
                _build_project_task_center_item(
                    project,
                    plans_by_project.get(project.id, []),
                )
            )
        return items
    finally:
        session.close()


def _get_project_backed_task_item_or_404(task_id: str) -> TaskCenterItemResponse:
    project_id = _parse_project_task_id(task_id)
    if not project_id:
        raise HTTPException(404, "任务不存在")
    session = _get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        plans_by_project = _load_project_task_center_plans(session, [project.id])
        return _build_project_task_center_item(
            project,
            plans_by_project.get(project.id, []),
        )
    finally:
        session.close()


def _serialize_model_profiles(payload: dict[str, object]) -> list[ModelProfile]:
    profiles = []
    for item in payload.get("profiles", []):
        if not isinstance(item, dict):
            continue
        profiles.append(
            ModelProfile(
                id=str(item.get("id", "")).strip(),
                name=str(item.get("name", "")).strip() or "未命名模型",
                has_api_key=bool(str(item.get("api_key", "")).strip()),
                base_url=str(item.get("base_url", "")).strip(),
                model=str(item.get("model", "")).strip(),
            )
        )
    return profiles


def _serialize_llm_settings(payload: dict[str, object], *, message: str) -> LLMSettingsResponse:
    return LLMSettingsResponse(
        has_api_key=bool(payload["api_key"]),
        base_url=str(payload["base_url"]),
        model=str(payload["model"]),
        profiles=_serialize_model_profiles(payload),
        default_profile_id=str(payload.get("default_profile_id", "")).strip(),
        operation_mode=str(payload["operation_mode"]),
        freeze_failed_candidates=bool(payload["freeze_failed_candidates"]),
        min_chapter_chars=max(500, int(payload.get("min_chapter_chars", 2500))),
        review_interval_chapters=max(0, int(payload.get("review_interval_chapters", 0))),
        progression_mode=str(payload.get("progression_mode", "serial_canon_band_guard")),
        auto_band_checkpoint=bool(payload.get("auto_band_checkpoint", True)),
        band_warn_action=str(payload.get("band_warn_action", "pause")) or "pause",
        manual_checkpoints_enabled=bool(payload.get("manual_checkpoints_enabled", True)),
        future_constraints_enabled=bool(payload.get("future_constraints_enabled", True)),
        message=message,
    )


def _delete_project(session, project_id: str) -> None:
    chapter_plan_ids = session.execute(
        select(ChapterPlan.id).where(ChapterPlan.project_id == project_id)
    ).scalars().all()
    if chapter_plan_ids:
        draft_ids = session.execute(
            select(ChapterDraft.id).where(ChapterDraft.chapter_plan_id.in_(chapter_plan_ids))
        ).scalars().all()
        if draft_ids:
            session.execute(
                delete(ChapterReview).where(ChapterReview.draft_id.in_(draft_ids))
            )
            session.execute(
                delete(ChapterDraft).where(ChapterDraft.id.in_(draft_ids))
            )

    thread_ids = session.execute(
        select(PlotThread.id).where(PlotThread.project_id == project_id)
    ).scalars().all()
    if thread_ids:
        session.execute(
            delete(Base.metadata.tables["plot_thread_beats"]).where(
                Base.metadata.tables["plot_thread_beats"].c.thread_id.in_(thread_ids)
            )
        )

    entity_ids = session.execute(
        select(Entity.id).where(Entity.project_id == project_id)
    ).scalars().all()
    if entity_ids:
        session.execute(
            delete(Base.metadata.tables["entity_states"]).where(
                Base.metadata.tables["entity_states"].c.entity_id.in_(entity_ids)
            )
        )

    event_ids = session.execute(
        select(CanonEvent.id).where(CanonEvent.project_id == project_id)
    ).scalars().all()
    if event_ids:
        session.execute(
            delete(EventEntityLink).where(EventEntityLink.event_id.in_(event_ids))
        )

    time_point_ids = session.execute(
        select(StoryTimePoint.id).where(StoryTimePoint.project_id == project_id)
    ).scalars().all()
    if time_point_ids:
        session.execute(
            delete(ChapterTimeline).where(
                (ChapterTimeline.start_time_id.in_(time_point_ids))
                | (ChapterTimeline.end_time_id.in_(time_point_ids))
            )
        )

    session.execute(
        delete(NPCIntentSnapshot).where(NPCIntentSnapshot.project_id == project_id)
    )

    for table in reversed(Base.metadata.sorted_tables):
        if table.name == "projects" or "project_id" not in table.c:
            continue
        session.execute(delete(table).where(table.c.project_id == project_id))

    session.execute(delete(Project).where(Project.id == project_id))


def _update_task(task_id: str, **changes: Any) -> None:
    task = _load_generation_task(task_id, include_deleted=True)
    if task is None or task.get("deleted"):
        return
    normalized = dict(changes)
    if task.get("cancel_requested") and normalized.get("status") in {"starting", "running", "needs_review"}:
        normalized.pop("status", None)
    if task.get("cancel_requested") and normalized.get("current_stage") not in {"terminating", "cancelled"}:
        normalized.pop("current_stage", None)
    if task.get("pause_requested") and normalized.get("status") in {"starting", "running"}:
        normalized.pop("status", None)
    if task.get("pause_requested") and normalized.get("current_stage") not in {"paused", "cancelled", "terminating"}:
        normalized.pop("current_stage", None)
    if "message" in normalized:
        normalized["message"] = str(normalized.get("message") or "")
    if "status" in normalized and normalized["status"] == "terminating":
        normalized["current_stage"] = "terminating"
    elif "status" in normalized:
        terminal_stage = _GENERATION_TERMINAL_STAGE_BY_STATUS.get(str(normalized["status"]).strip())
        if terminal_stage:
            normalized["current_stage"] = terminal_stage
    if "current_chapter" in normalized:
        try:
            normalized["current_chapter"] = int(normalized["current_chapter"] or 0)
        except (TypeError, ValueError):
            normalized["current_chapter"] = 0
    now = _utcnow()
    if normalized.get("status") == "paused":
        normalized["pause_requested"] = True
        normalized["paused_at"] = now
    next_stage = str(normalized.get("current_stage", "")).strip()
    if next_stage and next_stage != str(task.get("current_stage", "")).strip():
        history = list(task.get("stage_history", []))
        history.append(
            _new_stage_history_entry(
                next_stage,
                now=now,
                current_chapter=int(normalized.get("current_chapter", task.get("current_chapter", 0)) or 0),
                message=str(normalized.get("message", task.get("message", ""))).strip(),
            )
        )
        normalized["stage_history"] = history

    task.update(normalized)
    task["updated_at"] = now
    _sync_task_cache(task_id, task)

    if _SessionFactory is not None:
        def _operation() -> None:
            with _get_session() as session:
                row = session.get(GenerationTask, task_id)
                if row is None:
                    row = GenerationTask(id=task_id)
                _apply_generation_task_to_row(row, task, now=now)
                session.add(row)
                session.commit()

        _run_generation_task_db_write(_operation, context=f"update_generation_task:{task_id}")


def _task_should_abort(task_id: str) -> bool:
    task = _load_generation_task(task_id)
    if task is None or task.get("deleted"):
        return True
    return bool(task.get("cancel_requested"))


def _task_should_pause(task_id: str) -> bool:
    task = _load_generation_task(task_id)
    if task is None or task.get("deleted"):
        return False
    return bool(task.get("pause_requested")) and not bool(task.get("cancel_requested"))


def _get_generation_task_or_404(task_id: str) -> dict[str, Any]:
    _prune_tasks()
    task = _load_generation_task(task_id)
    if task is None or task.get("deleted"):
        raise HTTPException(404, "任务不存在")
    return task


def _saved_runtime_config_or_503() -> Config:
    if not _config:
        raise HTTPException(503, "服务尚未初始化")
    return build_saved_runtime_config(
        base_config=_config,
        runtime_settings=_runtime_settings,
    )


def _require_reason(reason: str, *, action: str) -> str:
    normalized = str(reason or "").strip()
    if not normalized:
        raise HTTPException(400, f"{action} 必须填写 reason。")
    return normalized


def _validate_constraint_payload(*, constraint_type: str, level: str, status: str) -> tuple[str, str, str]:
    normalized_type = str(constraint_type or "").strip()
    normalized_level = str(level or "hard").strip()
    normalized_status = str(status or "active").strip() or "active"
    if normalized_type not in CONSTRAINT_TYPES:
        raise HTTPException(400, f"未知 constraint_type: {normalized_type or '<empty>'}")
    if normalized_level not in CONSTRAINT_LEVELS:
        raise HTTPException(400, f"未知 constraint level: {normalized_level or '<empty>'}")
    if normalized_status not in CONSTRAINT_STATUSES:
        raise HTTPException(400, f"未知 constraint status: {normalized_status or '<empty>'}")
    return normalized_type, normalized_level, normalized_status


def _persist_project_automation(
    session,
    project: Project,
    automation: ProjectAutomationSettings,
) -> ProjectAutomationSettings:
    normalized = normalize_project_automation(automation.model_dump(mode="json"))
    project.automation_json = json.dumps(
        normalized.model_dump(mode="json"),
        ensure_ascii=False,
    )
    session.add(project)
    session.flush()
    return normalized


def _governance_request_payload(req: object) -> dict[str, object]:
    if req is None:
        return {}
    payload: dict[str, object] = {}
    for field in (
        "default_operation_mode",
        "operation_mode",
        "review_interval_chapters",
        "progression_mode",
        "auto_band_checkpoint",
        "band_warn_action",
        "manual_checkpoints_enabled",
        "future_constraints_enabled",
    ):
        if not hasattr(req, field):
            continue
        value = getattr(req, field)
        if value is None:
            continue
        target_field = "default_operation_mode" if field == "operation_mode" else field
        payload[target_field] = value
    return payload


def _resolve_project_governance(
    project: Project | None,
    *,
    overrides: dict[str, object] | None = None,
    base_config: Config | None = None,
) -> object:
    fallback_operation_mode = (
        base_config.operation_mode if base_config is not None else "blackbox"
    )
    fallback_review_interval = (
        max(0, int(base_config.review_interval_chapters or 0))
        if base_config is not None
        else 0
    )
    raw = project.governance_json if project is not None else "{}"
    governance = normalize_project_governance(
        raw,
        fallback_operation_mode=fallback_operation_mode,
        fallback_review_interval=fallback_review_interval,
        treat_empty_as_legacy=project is not None,
    )
    merged = governance.model_dump(mode="json")
    for key, value in (overrides or {}).items():
        merged[key] = value
    return normalize_project_governance(
        merged,
        fallback_operation_mode=fallback_operation_mode,
        fallback_review_interval=fallback_review_interval,
        treat_empty_as_legacy=False,
    )


def _persist_project_governance(
    session,
    project: Project,
    governance,
) -> object:
    normalized = _resolve_project_governance(project, overrides=governance.model_dump(mode="json"), base_config=_config)
    project.governance_json = json.dumps(
        normalized.model_dump(mode="json"),
        ensure_ascii=False,
    )
    session.add(project)
    session.flush()
    return normalized


def _log_decision_event(
    session,
    *,
    project_id: str,
    event_family: str,
    event_type: str,
    summary: str,
    reason: str = "",
    scope: str = "project",
    actor_type: str = "system",
    actor_id: str = "",
    band_id: str = "",
    chapter_number: int = 0,
    task_id: str = "",
    payload: dict[str, Any] | None = None,
    related_object_type: str = "",
    related_object_id: str = "",
    parent_event_id: str = "",
    causal_root_id: str = "",
) -> DecisionEvent:
    row = DecisionEvent(
        project_id=project_id,
        task_id=task_id,
        band_id=band_id,
        chapter_number=chapter_number,
        scope=scope,
        event_family=event_family,
        event_type=ensure_decision_event_type(event_type),
        actor_type=actor_type,
        actor_id=actor_id,
        summary=summary,
        reason=reason,
        payload_json=json.dumps(payload or {}, ensure_ascii=False),
        related_object_type=related_object_type,
        related_object_id=related_object_id,
        parent_event_id=parent_event_id,
        causal_root_id=causal_root_id,
    )
    session.add(row)
    session.flush()
    if not str(row.causal_root_id or "").strip():
        row.causal_root_id = row.id
        session.add(row)
        session.flush()
    return row


def _latest_band_checkpoint_row(
    session,
    *,
    project_id: str,
    band_id: str = "",
) -> BandCheckpoint | None:
    stmt = select(BandCheckpoint).where(BandCheckpoint.project_id == project_id)
    if band_id:
        stmt = stmt.where(BandCheckpoint.band_id == band_id)
    return session.execute(
        stmt.order_by(BandCheckpoint.created_at.desc(), BandCheckpoint.id.desc()).limit(1)
    ).scalar_one_or_none()


def _serialize_band_checkpoint(row: BandCheckpoint, *, session=None) -> BandCheckpointDetail:
    issues_payload = _json_load_list(row.issues_json)
    return BandCheckpointDetail(
        id=row.id,
        project_id=row.project_id,
        arc_id=row.arc_id,
        band_id=row.band_id,
        chapter_start=int(row.chapter_start or 0),
        chapter_end=int(row.chapter_end or 0),
        trigger_source=str(row.trigger_source or ""),
        boundary_kind=str(row.boundary_kind or ""),
        boundary_chapter=int(row.boundary_chapter or 0),
        status=str(row.status or "pending"),
        summary=str(row.summary or ""),
        reason=str(row.reason or ""),
        issues=[
            BandCheckpointIssueInfo.model_validate(item)
            for item in issues_payload
            if isinstance(item, dict)
        ],
        decision_refs=_decision_refs_for_checkpoint(session, row) if session is not None else [],
        created_at=_display_datetime(row.created_at),
        updated_at=_display_datetime(row.updated_at),
        resolved_at=_display_datetime(row.resolved_at),
    )


def _serialize_constraint(row: NarrativeConstraint) -> NarrativeConstraintInfo:
    payload = json.loads(row.payload_json or "{}") if str(row.payload_json or "").strip() else {}
    if not isinstance(payload, dict):
        payload = {}
    return NarrativeConstraintInfo(
        id=row.id,
        project_id=row.project_id,
        arc_id=row.arc_id,
        band_id=row.band_id,
        constraint_type=row.constraint_type,
        level=row.level,
        subject_name=row.subject_name,
        description=row.description,
        payload=payload,
        effective_from_chapter=int(row.effective_from_chapter or 1),
        protect_until_chapter=int(row.protect_until_chapter or 0),
        status=row.status,
        created_at=_display_datetime(row.created_at),
        updated_at=_display_datetime(row.updated_at),
    )


def _serialize_decision_event(row: DecisionEvent) -> DecisionEventInfo:
    payload = json.loads(row.payload_json or "{}") if str(row.payload_json or "").strip() else {}
    if not isinstance(payload, dict):
        payload = {}
    return DecisionEventInfo(
        id=row.id,
        project_id=row.project_id,
        task_id=row.task_id,
        band_id=row.band_id,
        chapter_number=int(row.chapter_number or 0),
        scope=row.scope,
        event_family=row.event_family,
        event_type=row.event_type,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        summary=row.summary,
        reason=row.reason,
        payload=payload,
        related_object_type=row.related_object_type,
        related_object_id=row.related_object_id,
        parent_event_id=str(getattr(row, "parent_event_id", "") or ""),
        causal_root_id=str(getattr(row, "causal_root_id", "") or ""),
        created_at=_display_datetime(row.created_at),
    )


def _decision_event_stmt(
    *,
    project_id: str,
    scope: str = "",
    band_id: str = "",
    chapter_number: int = 0,
    task_id: str = "",
    event_family: str = "",
    related_object_type: str = "",
    related_object_id: str = "",
    causal_root_id: str = "",
):
    stmt = select(DecisionEvent).where(DecisionEvent.project_id == project_id)
    if scope:
        stmt = stmt.where(DecisionEvent.scope == scope)
    if band_id:
        stmt = stmt.where(DecisionEvent.band_id == band_id)
    if chapter_number > 0:
        stmt = stmt.where(DecisionEvent.chapter_number == chapter_number)
    if task_id:
        stmt = stmt.where(DecisionEvent.task_id == task_id)
    if event_family:
        stmt = stmt.where(DecisionEvent.event_family == event_family)
    if related_object_type:
        stmt = stmt.where(DecisionEvent.related_object_type == related_object_type)
    if related_object_id:
        stmt = stmt.where(DecisionEvent.related_object_id == related_object_id)
    if causal_root_id:
        stmt = stmt.where(DecisionEvent.causal_root_id == causal_root_id)
    return stmt


def _list_decision_event_rows(
    session,
    *,
    project_id: str,
    scope: str = "",
    band_id: str = "",
    chapter_number: int = 0,
    task_id: str = "",
    event_family: str = "",
    related_object_type: str = "",
    related_object_id: str = "",
    causal_root_id: str = "",
    limit: int = 200,
    ascending: bool = False,
) -> list[DecisionEvent]:
    order_clause = (
        (DecisionEvent.created_at.asc(), DecisionEvent.id.asc())
        if ascending
        else (DecisionEvent.created_at.desc(), DecisionEvent.id.desc())
    )
    return session.execute(
        _decision_event_stmt(
            project_id=project_id,
            scope=scope,
            band_id=band_id,
            chapter_number=chapter_number,
            task_id=task_id,
            event_family=event_family,
            related_object_type=related_object_type,
            related_object_id=related_object_id,
            causal_root_id=causal_root_id,
        )
        .order_by(*order_clause)
        .limit(max(1, limit))
    ).scalars().all()


def _latest_related_decision_event(
    session,
    *,
    project_id: str,
    related_object_type: str = "",
    related_object_id: str = "",
    band_id: str = "",
    chapter_number: int = 0,
) -> DecisionEvent | None:
    rows = _list_decision_event_rows(
        session,
        project_id=project_id,
        related_object_type=related_object_type,
        related_object_id=related_object_id,
        band_id=band_id,
        chapter_number=chapter_number,
        limit=1,
        ascending=False,
    )
    if rows:
        return rows[0]
    if related_object_type or related_object_id:
        return None
    rows = _list_decision_event_rows(
        session,
        project_id=project_id,
        band_id=band_id,
        chapter_number=chapter_number,
        limit=1,
        ascending=False,
    )
    return rows[0] if rows else None


def _decision_refs_for_checkpoint(session, row: BandCheckpoint) -> list[DecisionEventInfo]:
    rows = _list_decision_event_rows(
        session,
        project_id=row.project_id,
        related_object_type="band_checkpoint",
        related_object_id=row.id,
        limit=50,
        ascending=True,
    )
    return [_serialize_decision_event(item) for item in rows]


def _decision_refs_for_chapter_review(
    session,
    *,
    project_id: str,
    chapter_number: int,
    review_id: str,
) -> list[DecisionEventInfo]:
    allowed_types = {
        DecisionEventType.REVIEW_VERDICT_RECORDED,
        DecisionEventType.REPAIR_STARTED,
        DecisionEventType.REPAIR_FAILED,
        DecisionEventType.REPAIR_SUCCEEDED,
        DecisionEventType.FORCED_ACCEPT_APPLIED,
        DecisionEventType.REVIEW_APPROVED,
        DecisionEventType.CANON_COMMIT,
        DecisionEventType.CANON_COMMIT_FAILED,
        DecisionEventType.HARD_GATE_HIT,
    }
    ordered: dict[str, DecisionEventInfo] = {}
    rows = _list_decision_event_rows(
        session,
        project_id=project_id,
        related_object_type="chapter_review",
        related_object_id=review_id,
        limit=80,
        ascending=True,
    )
    for row in rows:
        event = _serialize_decision_event(row)
        ordered[event.id] = event
    rows = _list_decision_event_rows(
        session,
        project_id=project_id,
        chapter_number=chapter_number,
        scope="chapter",
        limit=120,
        ascending=True,
    )
    for row in rows:
        if str(row.event_type or "") not in allowed_types:
            continue
        event = _serialize_decision_event(row)
        ordered.setdefault(event.id, event)
    return list(ordered.values())


def _counter_rows(counter: Counter[str], *, limit: int = 5) -> list[dict[str, Any]]:
    return [
        {"name": name, "count": count}
        for name, count in counter.most_common(max(1, limit))
        if str(name or "").strip()
    ]


def _build_causal_replay(
    session,
    *,
    project_id: str,
    scope: str = "",
    arc_id: str = "",
    band_id: str = "",
    chapter_number: int = 0,
    task_id: str = "",
) -> CausalReplayResponse:
    if str(scope or "").strip() == "arc":
        target_arc_id = str(arc_id or "").strip()
        if not target_arc_id:
            active_arc = session.execute(
                select(ArcPlanVersion)
                .where(ArcPlanVersion.project_id == project_id, ArcPlanVersion.status == "active")
                .order_by(ArcPlanVersion.version.desc(), ArcPlanVersion.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            target_arc_id = str(active_arc.id if active_arc is not None else "")
        if not target_arc_id:
            return CausalReplayResponse(current_outcome="no_active_arc")
        bands = session.execute(
            select(BandExperiencePlan)
            .where(BandExperiencePlan.project_id == project_id, BandExperiencePlan.arc_id == target_arc_id)
            .order_by(BandExperiencePlan.chapter_start.asc(), BandExperiencePlan.created_at.asc())
        ).scalars().all()
        band_ids = {str(row.band_id or "") for row in bands if str(row.band_id or "").strip()}
        chapter_numbers: set[int] = set()
        for band in bands:
            start = int(band.chapter_start or 0)
            end = int(band.chapter_end or 0)
            if start and end >= start:
                chapter_numbers.update(range(start, end + 1))
        rows = _list_decision_event_rows(
            session,
            project_id=project_id,
            limit=1000,
            ascending=True,
        )
        scoped_rows: list[DecisionEvent] = []
        checkpoint_ids = set(
            session.execute(
                select(BandCheckpoint.id).where(
                    BandCheckpoint.project_id == project_id,
                    BandCheckpoint.arc_id == target_arc_id,
                )
            ).scalars().all()
        )
        for row in rows:
            payload = _json_load_object(getattr(row, "payload_json", "") or "")
            if str(payload.get("arc_id") or "") == target_arc_id:
                scoped_rows.append(row)
                continue
            if str(getattr(row, "band_id", "") or "") in band_ids:
                scoped_rows.append(row)
                continue
            if int(getattr(row, "chapter_number", 0) or 0) in chapter_numbers:
                scoped_rows.append(row)
                continue
            if (
                str(getattr(row, "related_object_type", "") or "") == "band_checkpoint"
                and str(getattr(row, "related_object_id", "") or "") in checkpoint_ids
            ):
                scoped_rows.append(row)
        items = [_serialize_decision_event(row) for row in scoped_rows]
        scope_rank = {"arc": 0, "band": 1, "chapter": 2, "project": 3, "task": 4}
        items.sort(
            key=lambda item: (
                int(item.chapter_number or 0),
                0 if not str(item.parent_event_id or "").strip() else 1,
                scope_rank.get(str(item.scope or ""), 99),
                str(item.created_at or ""),
                str(item.id or ""),
            )
        )
        by_parent: dict[str, list[DecisionEventInfo]] = defaultdict(list)
        for item in items:
            if item.parent_event_id:
                by_parent[str(item.parent_event_id)].append(item)
        linked_review_refs = [item for item in items if item.related_object_type == "chapter_review"]
        linked_checkpoint_refs = [item for item in items if item.related_object_type == "band_checkpoint"]
        current_outcome = "arc_empty"
        if items:
            current_outcome = items[-1].summary or items[-1].event_type
        elif bands:
            current_outcome = f"arc {target_arc_id} has {len(bands)} band(s), no decision events"
        return CausalReplayResponse(
            root_event=next((item for item in items if not item.parent_event_id), items[0] if items else None),
            timeline=items,
            branches=dict(by_parent),
            current_outcome=current_outcome,
            linked_review_refs=linked_review_refs,
            linked_checkpoint_refs=linked_checkpoint_refs,
        )
    scoped_rows = _list_decision_event_rows(
        session,
        project_id=project_id,
        scope=scope,
        band_id=band_id,
        chapter_number=chapter_number,
        task_id=task_id,
        limit=400,
        ascending=False,
    )
    if not scoped_rows:
        return CausalReplayResponse()
    pivot = scoped_rows[0]
    root_id = str(getattr(pivot, "causal_root_id", "") or pivot.id or "")
    timeline_rows = _list_decision_event_rows(
        session,
        project_id=project_id,
        causal_root_id=root_id,
        limit=400,
        ascending=True,
    )
    if not timeline_rows:
        timeline_rows = [pivot]
    items = [_serialize_decision_event(row) for row in timeline_rows]
    by_parent: dict[str, list[DecisionEventInfo]] = defaultdict(list)
    for item in items:
        parent_id = str(item.parent_event_id or "")
        if parent_id:
            by_parent[parent_id].append(item)
    root_event = next((item for item in items if item.id == root_id), items[0] if items else None)
    linked_review_refs = [item for item in items if item.related_object_type == "chapter_review"]
    linked_checkpoint_refs = [item for item in items if item.related_object_type == "band_checkpoint"]
    current_outcome = items[-1].event_type if items else ""
    if items and items[-1].summary:
        current_outcome = items[-1].summary
    return CausalReplayResponse(
        root_event=root_event,
        timeline=items,
        branches=dict(by_parent),
        current_outcome=current_outcome,
        linked_review_refs=linked_review_refs,
        linked_checkpoint_refs=linked_checkpoint_refs,
    )


def _build_governance_insights(session, *, project_id: str) -> GovernanceInsightsResponse:
    event_rows = _list_decision_event_rows(
        session,
        project_id=project_id,
        limit=1000,
        ascending=False,
    )
    override_counter: Counter[str] = Counter()
    override_reason_counter: Counter[str] = Counter()
    warn_allowed_counter: Counter[str] = Counter()
    constraint_counter: Counter[str] = Counter()
    blocking_counter: Counter[str] = Counter()
    issue_group_counter: Counter[str] = Counter()
    forced_accept_frequency = 0
    recent_examples: list[dict[str, Any]] = []
    checkpoint_rows = (
        session.execute(
            select(BandCheckpoint)
            .where(BandCheckpoint.project_id == project_id)
            .order_by(BandCheckpoint.created_at.desc(), BandCheckpoint.id.desc())
            .limit(20)
        ).scalars().all()
    )
    checkpoint_status_counter: Counter[str] = Counter(
        str(row.status or "") for row in checkpoint_rows if str(row.status or "").strip()
    )
    checkpoint_map = {row.id: row for row in checkpoint_rows}
    for checkpoint in checkpoint_rows:
        for issue in _json_load_list(checkpoint.issues_json):
            if not isinstance(issue, dict):
                continue
            code = str(issue.get("code") or "").strip()
            group = str(issue.get("issue_group") or issue_group_for_issue(code=code)).strip()
            if group:
                issue_group_counter[group] += 1
    for row in event_rows:
        payload = json.loads(row.payload_json or "{}") if str(row.payload_json or "").strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        if row.event_type == DecisionEventType.FORCED_ACCEPT_APPLIED:
            forced_accept_frequency += 1
            override_counter["forced_accept"] += 1
            reason = str(payload.get("reason") or row.reason or "").strip()
            if reason:
                override_reason_counter[reason] += 1
            recent_examples.append(
                {
                    "event_id": row.id,
                    "event_type": row.event_type,
                    "chapter_number": int(row.chapter_number or 0),
                    "band_id": str(row.band_id or ""),
                    "summary": str(row.summary or ""),
                }
            )
        if row.event_type == DecisionEventType.HARD_GATE_HIT:
            blocking_counter[str(payload.get("blocking_reason") or "hard_gate_hit")] += 1
            recent_examples.append(
                {
                    "event_id": row.id,
                    "event_type": row.event_type,
                    "chapter_number": int(row.chapter_number or 0),
                    "band_id": str(row.band_id or ""),
                    "summary": str(row.summary or ""),
                    "blocking_reason": str(payload.get("blocking_reason") or ""),
                }
            )
        if row.event_type in {DecisionEventType.BAND_CHECKPOINT_HIT, DecisionEventType.BAND_CHECKPOINT_CREATED}:
            status = str(payload.get("status") or "")
            if status in {"warn", "fail", "error"}:
                blocking_counter[f"band_checkpoint_{status}"] += 1
        if row.event_type == DecisionEventType.BAND_CHECKPOINT_OVERRIDDEN:
            override_counter["band_checkpoint_override"] += 1
            reason = str(payload.get("reason") or row.reason or "").strip()
            if reason:
                override_reason_counter[reason] += 1
            checkpoint = checkpoint_map.get(str(row.related_object_id or ""))
            issues = _json_load_list(checkpoint.issues_json) if checkpoint is not None else []
            for issue in issues:
                code = str(issue.get("code") or issue.get("severity") or "checkpoint_issue")
                issue_group = str(issue.get("issue_group") or issue_group_for_issue(code=code)).strip()
                if issue_group:
                    issue_group_counter[issue_group] += 1
                warn_allowed_counter[code] += 1
                if code in {"future_constraint", "future_resource_preservation", "next_band_compatibility"}:
                    constraint_counter[code] += 1
                category = str(issue.get("category") or "").strip()
                if category:
                    warn_allowed_counter[category] += 1
            recent_examples.append(
                {
                    "event_id": row.id,
                    "event_type": row.event_type,
                    "chapter_number": int(row.chapter_number or 0),
                    "band_id": str(row.band_id or ""),
                    "summary": str(row.summary or ""),
                    "related_object_id": str(row.related_object_id or ""),
                }
            )
        issue_types = payload.get("issue_types") or []
        issue_groups = payload.get("issue_groups") or []
        if row.event_type == DecisionEventType.REVIEW_APPROVED:
            reason = str(payload.get("reason") or row.reason or "").strip()
            if reason:
                override_reason_counter[reason] += 1
            for issue_type in issue_types if isinstance(issue_types, list) else []:
                warn_allowed_counter[str(issue_type or "")] += 1
                if "constraint" in str(issue_type or ""):
                    constraint_counter[str(issue_type or "")] += 1
                group = issue_group_for_issue(issue_type=str(issue_type or ""))
                if group:
                    issue_group_counter[group] += 1
            for group in issue_groups if isinstance(issue_groups, list) else []:
                normalized_group = str(group or "").strip()
                if normalized_group:
                    issue_group_counter[normalized_group] += 1
            recent_examples.append(
                {
                    "event_id": row.id,
                    "event_type": row.event_type,
                    "chapter_number": int(row.chapter_number or 0),
                    "band_id": str(row.band_id or ""),
                    "summary": str(row.summary or ""),
                }
            )
    recommended_adjustments: list[dict[str, Any]] = []
    if override_counter.get("band_checkpoint_override", 0) >= 2:
        recommended_adjustments.append(
            {
                "type": "review_band_checkpoint_policy",
                "target": "band_checkpoint",
                "reason": "band checkpoint override 次数偏高，建议复查 warn 阈值和 issue 口径。",
                "count": override_counter["band_checkpoint_override"],
            }
        )
    if warn_allowed_counter.get("future_resource_preservation", 0) or any(
        key in warn_allowed_counter
        for key in {
            "character_locked_out",
            "thread_closed_too_early",
            "relationship_closed_too_early",
            "secret_over_explained",
            "growth_arc_completed_too_early",
        }
    ):
        recommended_adjustments.append(
            {
                "type": "review_future_preservation_warns",
                "target": "future_resource_preservation",
                "reason": "未来资源保留 warn 多次被人工放行，建议复查风险分类和证据阈值。",
                "count": warn_allowed_counter.get("future_resource_preservation", 0),
            }
        )
    if forced_accept_frequency:
        recommended_adjustments.append(
            {
                "type": "review_forced_accept_frequency",
                "target": "chapter_review",
                "reason": "forced accept 已出现，建议复查 reviewer 规则或 repair 链是否过严。",
                "count": forced_accept_frequency,
            }
        )
    if constraint_counter:
        top_constraint = constraint_counter.most_common(1)[0]
        recommended_adjustments.append(
            {
                "type": "review_constraint_quality",
                "target": top_constraint[0],
                "reason": "future constraint 相关问题频繁进入人工放行，建议检查 hard/soft 边界。",
                "count": top_constraint[1],
            }
        )
    if issue_group_counter.get("director_imbalance", 0) >= 2:
        recommended_adjustments.append(
            {
                "type": "review_director_imbalance_rules",
                "target": "director_imbalance",
                "reason": "导演失衡类问题较多，建议复查 task contract、payoff 和 future preservation 口径。",
                "count": issue_group_counter["director_imbalance"],
            }
        )
    if issue_group_counter.get("fact_conflict", 0) >= 2:
        recommended_adjustments.append(
            {
                "type": "review_fact_conflict_rules",
                "target": "fact_conflict",
                "reason": "事实冲突类问题较多，建议复查 hard/soft constraint 与 continuity 判定证据。",
                "count": issue_group_counter["fact_conflict"],
            }
        )
    return GovernanceInsightsResponse(
        top_override_rule_types=_counter_rows(override_counter),
        top_override_reasons=_counter_rows(override_reason_counter),
        top_warn_but_allowed_issue_types=_counter_rows(warn_allowed_counter),
        top_constraint_false_positive_types=_counter_rows(constraint_counter),
        forced_accept_frequency=forced_accept_frequency,
        most_common_blocking_reasons=_counter_rows(blocking_counter),
        recent_band_checkpoint_distribution=_counter_rows(checkpoint_status_counter),
        issue_group_distribution=_counter_rows(issue_group_counter),
        recent_action_effectiveness=derive_action_effectiveness(session, project_id=project_id, limit=8),
        recommended_adjustments=recommended_adjustments[:5],
        recent_examples=recent_examples[:8],
    )


def _project_has_active_generation_task(project_id: str, *, session=None) -> bool:
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id:
        return False
    if _SessionFactory is None:
        with _tasks_lock:
            for task in _tasks.values():
                if task.get("deleted"):
                    continue
                if str(task.get("task_kind", "generation")) != "generation":
                    continue
                if str(task.get("project_id", "")).strip() != normalized_project_id:
                    continue
                if _task_is_terminal(str(task.get("status", "")).strip()):
                    continue
                return True
        return False

    if session is not None:
        active_task_id = session.execute(
            select(GenerationTask.id).where(
                GenerationTask.deleted_at.is_(None),
                GenerationTask.project_id == normalized_project_id,
                GenerationTask.status.notin_(tuple(_GENERATION_TERMINAL_STATUSES)),
            ).limit(1)
        ).scalar_one_or_none()
        return active_task_id is not None
    with _get_session() as managed_session:
        return _project_has_active_generation_task(
            normalized_project_id,
            session=managed_session,
        )


def _project_has_active_upload_job(project_id: str, *, session=None) -> bool:
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id or _SessionFactory is None:
        return False
    if session is not None:
        active_job_id = session.execute(
            select(PublisherUploadJob.id).where(
                PublisherUploadJob.deleted_at.is_(None),
                PublisherUploadJob.project_id == normalized_project_id,
                PublisherUploadJob.status.notin_(tuple(_UPLOAD_TERMINAL_STATUSES)),
            ).limit(1)
        ).scalar_one_or_none()
        return active_job_id is not None
    with _get_session() as managed_session:
        return _project_has_active_upload_job(
            normalized_project_id,
            session=managed_session,
        )


def _generation_task_conflict_message(project_id: str) -> str:
    return f"项目 {project_id} 已有运行中的生成任务，请先终止或等待当前任务完成。"


def _project_delete_conflict_message(blockers: list[str]) -> str:
    labels = {
        "generation": "生成任务",
        "upload": "发布任务",
    }
    blocker_text = "、".join(labels.get(item, item) for item in blockers)
    return f"项目存在运行中的{blocker_text}，请先终止后再删除。"


def _project_delete_blockers(project_id: str, *, session) -> list[str]:
    blockers: list[str] = []
    if _project_has_active_generation_task(project_id, session=session):
        blockers.append("generation")
    if _project_has_active_upload_job(project_id, session=session):
        blockers.append("upload")
    return blockers


def _create_task_root_event(
    *,
    project_id: str,
    task_id: str,
    event_type: str,
    summary: str,
    reason: str = "",
    actor_type: str = "api",
) -> str:
    if not str(project_id or "").strip():
        return ""
    with _get_session() as session:
        row = _log_decision_event(
            session,
            project_id=project_id,
            task_id=task_id,
            scope="task",
            event_family="audit_action",
            event_type=event_type,
            actor_type=actor_type,
            summary=summary,
            reason=reason,
            related_object_type="generation_task",
            related_object_id=task_id,
        )
        session.commit()
        return row.id


def _make_generation_completion_handler(
    *,
    task_id: str,
    root_event_id: str = "",
    prior_handler=None,
):
    def _handler(result) -> None:
        if prior_handler is not None:
            prior_handler(result)
        project_id = str(getattr(result, "project_id", "") or "").strip()
        if not project_id:
            return
        event_type = ""
        summary = ""
        if getattr(result, "paused", False):
            event_type = DecisionEventType.PAUSE_REACHED
            summary = "生成任务已在安全检查点暂停。"
        elif getattr(result, "cancelled", False):
            event_type = DecisionEventType.TERMINATE_REACHED
            summary = "生成任务已在安全检查点终止。"
        elif getattr(result, "failed_chapters", None):
            event_type = DecisionEventType.RUN_COMPLETED_WITH_FAILURES
            summary = "生成任务已结束，存在失败章节。"
        else:
            event_type = DecisionEventType.RUN_COMPLETED
            summary = "生成任务已完成。"
        with _get_session() as session:
            _log_decision_event(
                session,
                project_id=project_id,
                task_id=task_id,
                scope="task",
                event_family="business_event",
                event_type=event_type,
                actor_type="system",
                summary=summary,
                payload={
                    "completed_chapters": list(getattr(result, "completed_chapters", []) or []),
                    "failed_chapters": list(getattr(result, "failed_chapters", []) or []),
                    "paused_chapters": list(getattr(result, "paused_chapters", []) or []),
                },
                related_object_type="generation_task",
                related_object_id=task_id,
                causal_root_id=root_event_id,
            )
            session.commit()

    return _handler


def _create_generation_task(
    *,
    premise: str,
    genre: str,
    num_chapters: int,
    runtime_config: Config,
    project_id: str = "",
    title: str = "",
    subtitle: str = "",
) -> str:
    normalized_project_id = str(project_id or "").strip()
    if normalized_project_id and _project_has_active_generation_task(normalized_project_id):
        raise ActiveGenerationTaskError(
            _generation_task_conflict_message(normalized_project_id)
        )
    task_id = uuid.uuid4().hex[:12]
    task_record = _create_task_record(
        message=f"开始生成 {num_chapters} 章。",
        title=title or (premise.strip()[:36] if premise.strip() else "未命名生成任务"),
        subtitle=subtitle or f"{genre} · {num_chapters} 章",
        requested_chapters=num_chapters,
    )
    if normalized_project_id:
        task_record["project_id"] = normalized_project_id
    _persist_generation_task(task_id, task_record)
    root_event_id = ""
    if normalized_project_id:
        root_event_id = _create_task_root_event(
            project_id=normalized_project_id,
            task_id=task_id,
            event_type=DecisionEventType.GENERATION_REQUESTED,
            summary="项目生成任务已创建。",
        )
    runtime_config = copy_config(
        runtime_config,
        governance_task_id=task_id,
        governance_causal_root_id=root_event_id,
    )
    t = threading.Thread(
        target=_run_generation_with_config,
        args=(
            task_id,
            premise,
            genre,
            num_chapters,
            runtime_config,
            _update_task,
            logger,
            normalized_project_id or None,
            lambda: _task_should_abort(task_id),
            lambda: _task_should_pause(task_id),
            _make_generation_completion_handler(
                task_id=task_id,
                root_event_id=root_event_id,
                prior_handler=_maybe_enqueue_auto_publish_jobs,
            ),
        ),
        daemon=True,
    )
    t.start()
    return task_id


def _create_continue_generation_task(
    *,
    project_id: str,
    runtime_config: Config,
    requested_chapters: int,
    max_chapters: int | None = None,
    title: str = "",
    subtitle: str = "",
    message: str = "",
) -> str:
    normalized_project_id = str(project_id or "").strip()
    if normalized_project_id and _project_has_active_generation_task(normalized_project_id):
        raise ActiveGenerationTaskError(
            _generation_task_conflict_message(normalized_project_id)
        )
    task_id = uuid.uuid4().hex[:12]
    task_record = _create_task_record(
        message=message or "准备继续后续章节。",
        title=title or f"继续生成 {normalized_project_id}",
        subtitle=subtitle or f"项目 {normalized_project_id}",
        requested_chapters=requested_chapters,
    )
    task_record["project_id"] = normalized_project_id
    _persist_generation_task(task_id, task_record)
    root_event_id = _create_task_root_event(
        project_id=normalized_project_id,
        task_id=task_id,
        event_type=DecisionEventType.CONTINUE_REQUESTED,
        summary="继续生成任务已创建。",
    )
    runtime_config = copy_config(
        runtime_config,
        governance_task_id=task_id,
        governance_causal_root_id=root_event_id,
    )
    thread = threading.Thread(
        target=_run_continue_project_with_config,
        args=(
            task_id,
            normalized_project_id,
            runtime_config,
            _update_task,
            logger,
            lambda: _task_should_abort(task_id),
            lambda: _task_should_pause(task_id),
            max_chapters,
            _make_generation_completion_handler(
                task_id=task_id,
                root_event_id=root_event_id,
                prior_handler=_maybe_enqueue_auto_publish_jobs,
            ),
        ),
        daemon=True,
    )
    thread.start()
    return task_id


def _maybe_enqueue_auto_publish_jobs(result) -> None:
    project_id = str(getattr(result, "project_id", "") or "").strip()
    chapter_numbers = sorted({
        int(item)
        for item in getattr(result, "completed_chapters", []) or []
        if str(item).isdigit() or isinstance(item, int)
    })
    if not project_id or not chapter_numbers or _publisher_manager is None:
        return

    session = _get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            return
        automation = normalize_project_automation(project.automation_json)
        if not automation.auto_publish:
            return
        publish = automation.publish
        platform = str(publish.platform or "").strip()
        book_name = str(publish.book_name or "").strip() or project.title
        if not platform or not book_name:
            return
        plans = session.execute(
            select(ChapterPlan)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number.in_(chapter_numbers),
            )
            .order_by(ChapterPlan.chapter_number.asc())
        ).scalars().all()
        if not plans:
            return
        plan_by_number = {
            int(plan.chapter_number): plan
            for plan in plans
        }
        draft_map = load_latest_drafts_by_plan_id(session, [plan.id for plan in plans])
        jobs = []
        for chapter_number in chapter_numbers:
            plan = plan_by_number.get(chapter_number)
            if plan is None:
                continue
            draft = draft_map.get(plan.id)
            if draft is None:
                continue
            jobs.append(
                {
                    "chapter_title": plan.title,
                    "body": draft.body_text,
                }
            )
        if not jobs:
            return
        _publisher_manager.create_upload_jobs_batch(
            project_id=project_id,
            platform=platform,
            book_name=book_name,
            jobs=jobs,
            upload_url=publish.upload_url or None,
            publish=True,
            create_if_missing=bool(publish.create_if_missing),
            book_meta=publish.book_meta.model_dump(mode="json"),
        )
    except Exception:  # noqa: BLE001
        logger.exception("Auto publish enqueue failed for project %s", project_id)
    finally:
        session.close()


def _automation_daily_start_minutes(automation: ProjectAutomationSettings) -> int:
    try:
        hour_text, minute_text = automation.daily_start_time.split(":", 1)
        return int(hour_text) * 60 + int(minute_text)
    except (TypeError, ValueError):
        return 9 * 60


def _load_automation_scheduler_metrics(
    session,
    project_ids: list[str],
) -> tuple[dict[str, int], dict[str, int], dict[str, list[int]], set[str]]:
    normalized_project_ids = [
        str(project_id or "").strip()
        for project_id in project_ids
        if str(project_id or "").strip()
    ]
    pending_review_counts = {project_id: 0 for project_id in normalized_project_ids}
    total_plan_counts = {project_id: 0 for project_id in normalized_project_ids}
    pending_numbers_by_project: dict[str, list[int]] = {
        project_id: []
        for project_id in normalized_project_ids
    }
    if not normalized_project_ids:
        return pending_review_counts, total_plan_counts, pending_numbers_by_project, set()

    plan_count_rows = session.execute(
        select(
            ChapterPlan.project_id,
            func.count(ChapterPlan.id),
            func.sum(
                case(
                    (ChapterPlan.status == "needs_review", 1),
                    else_=0,
                )
            ),
        )
        .where(ChapterPlan.project_id.in_(normalized_project_ids))
        .group_by(ChapterPlan.project_id)
    ).all()
    for project_id, total_count, pending_review_count in plan_count_rows:
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id:
            continue
        total_plan_counts[normalized_project_id] = int(total_count or 0)
        pending_review_counts[normalized_project_id] = int(pending_review_count or 0)

    pending_number_rows = session.execute(
        select(ChapterPlan.project_id, ChapterPlan.chapter_number)
        .where(
            ChapterPlan.project_id.in_(normalized_project_ids),
            ChapterPlan.status.in_(["planned", "failed"]),
        )
        .order_by(ChapterPlan.project_id.asc(), ChapterPlan.chapter_number.asc())
    ).all()
    for project_id, chapter_number in pending_number_rows:
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id:
            continue
        try:
            pending_numbers_by_project.setdefault(normalized_project_id, []).append(int(chapter_number))
        except (TypeError, ValueError):
            continue

    active_generation_project_ids = {
        str(project_id or "").strip()
        for project_id in session.execute(
            select(GenerationTask.project_id)
            .where(
                GenerationTask.deleted_at.is_(None),
                GenerationTask.project_id.in_(normalized_project_ids),
                GenerationTask.status.notin_(tuple(_GENERATION_TERMINAL_STATUSES)),
            )
            .distinct()
        ).scalars().all()
        if str(project_id or "").strip()
    }
    return (
        pending_review_counts,
        total_plan_counts,
        pending_numbers_by_project,
        active_generation_project_ids,
    )


def _run_automation_scheduler_pass() -> None:
    if _SessionFactory is None or _config is None:
        return
    try:
        runtime_config = _saved_runtime_config_or_503()
    except HTTPException:
        return
    now = _utcnow()
    now_local = now.astimezone(_DISPLAY_TZ)
    today = now_local.strftime("%Y-%m-%d")
    current_minutes = now_local.hour * 60 + now_local.minute

    session = _get_session()
    try:
        ready_projects: list[tuple[Project, ProjectAutomationSettings]] = []
        projects = session.execute(
            select(Project).order_by(Project.updated_at.desc())
        ).scalars().all()
        for project in projects:
            automation = normalize_project_automation(project.automation_json)
            if not automation.enabled:
                continue
            if automation.last_scheduler_date == today:
                continue
            if current_minutes < _automation_daily_start_minutes(automation):
                continue
            ready_projects.append((project, automation))

        (
            pending_review_counts,
            total_plan_counts,
            pending_numbers_by_project,
            active_generation_project_ids,
        ) = _load_automation_scheduler_metrics(
            session,
            [project.id for project, _automation in ready_projects],
        )

        for project, automation in ready_projects:
            pending_review = int(pending_review_counts.get(project.id, 0) or 0)
            total_plans = int(total_plan_counts.get(project.id, 0) or 0)
            pending_numbers = list(pending_numbers_by_project.get(project.id, []))

            updated = automation.model_copy(
                update={
                    "last_scheduler_date": today,
                    "last_scheduler_at": _display_datetime(now),
                }
            )
            if project.id in active_generation_project_ids:
                updated = updated.model_copy(
                    update={
                        "last_scheduler_action": "active_task",
                        "last_scheduler_message": "已有运行中的生成任务，今日不重复调度。",
                        "last_scheduler_task_id": "",
                    }
                )
                _persist_project_automation(session, project, updated)
                continue
            if pending_review:
                updated = updated.model_copy(
                    update={
                        "last_scheduler_action": "waiting_review",
                        "last_scheduler_message": "仍有章节等待人工 review，今日暂停自动生成。",
                        "last_scheduler_task_id": "",
                    }
                )
                _persist_project_automation(session, project, updated)
                continue

            quota = min(20, max(1, int(automation.daily_chapter_quota or 1)))
            task_id = ""
            if total_plans == 0:
                try:
                    task_id = _create_generation_task(
                        premise=project.premise,
                        genre=project.genre,
                        num_chapters=quota,
                        runtime_config=runtime_config,
                        project_id=project.id,
                        title=project.title,
                        subtitle=f"自动调度 · 首批 {quota} 章",
                    )
                    updated = updated.model_copy(
                        update={
                            "last_scheduler_action": "started_initial_generation",
                            "last_scheduler_message": f"已按计划启动首批 {quota} 章生成。",
                            "last_scheduler_task_id": task_id,
                        }
                    )
                except ActiveGenerationTaskError:
                    updated = updated.model_copy(
                        update={
                            "last_scheduler_action": "active_task",
                            "last_scheduler_message": "已有运行中的生成任务，今日不重复调度。",
                            "last_scheduler_task_id": "",
                        }
                    )
            elif pending_numbers:
                try:
                    task_id = _create_continue_generation_task(
                        project_id=project.id,
                        runtime_config=runtime_config,
                        requested_chapters=total_plans,
                        max_chapters=quota,
                        title=project.title,
                        subtitle=f"自动调度 · 今日上限 {quota} 章",
                        message=f"按计划继续生成，今日最多处理 {quota} 章。",
                    )
                    updated = updated.model_copy(
                        update={
                            "last_scheduler_action": "started_continue_generation",
                            "last_scheduler_message": f"已按计划继续生成，今日最多处理 {quota} 章。",
                            "last_scheduler_task_id": task_id,
                        }
                    )
                except ActiveGenerationTaskError:
                    updated = updated.model_copy(
                        update={
                            "last_scheduler_action": "active_task",
                            "last_scheduler_message": "已有运行中的生成任务，今日不重复调度。",
                            "last_scheduler_task_id": "",
                        }
                    )
            else:
                updated = updated.model_copy(
                    update={
                        "last_scheduler_action": "idle",
                        "last_scheduler_message": "没有待生成章节，今日无需调度。",
                        "last_scheduler_task_id": "",
                    }
                )

            _persist_project_automation(session, project, updated)
        session.commit()
    except Exception:  # noqa: BLE001
        session.rollback()
        raise
    finally:
        session.close()


def _automation_scheduler_loop() -> None:
    while not _automation_scheduler_stop.wait(30.0):
        try:
            _run_automation_scheduler_pass()
        except Exception:  # noqa: BLE001
            logger.exception("Automation scheduler loop failed.")


def _start_automation_scheduler() -> None:
    global _automation_scheduler_thread
    if _automation_scheduler_thread is not None and _automation_scheduler_thread.is_alive():
        return
    _automation_scheduler_stop.clear()
    _automation_scheduler_thread = threading.Thread(
        target=_automation_scheduler_loop,
        name="forwin-automation-scheduler",
        daemon=True,
    )
    _automation_scheduler_thread.start()


def _stop_automation_scheduler() -> None:
    global _automation_scheduler_thread
    _automation_scheduler_stop.set()
    thread = _automation_scheduler_thread
    _automation_scheduler_thread = None
    if thread is not None and thread.is_alive():
        thread.join(timeout=2.0)


def _list_generation_tasks(limit: int) -> list[tuple[str, dict[str, Any]]]:
    _prune_tasks()
    normalized_limit = max(1, min(int(limit or 30), 100))
    if _SessionFactory is None:
        with _tasks_lock:
            return [
                (task_id, dict(task))
                for task_id, task in sorted(
                    _tasks.items(),
                    key=lambda item: item[1].get("updated_at", _utcnow()),
                    reverse=True,
                )
                if not task.get("deleted")
            ][:normalized_limit]

    with _get_session() as session:
        rows = session.execute(
            select(GenerationTask)
            .where(GenerationTask.deleted_at.is_(None))
            .order_by(GenerationTask.updated_at.desc())
            .limit(normalized_limit)
        ).scalars().all()
        merged: dict[str, dict[str, Any]] = {}
        for row in rows:
            persisted = _generation_task_from_row(row)
            cached = _cached_generation_task(row.id)
            task = _prefer_cached_generation_task(persisted, cached)
            if task is not None:
                task = _augment_task_with_provisional_history(session, task)
                visible = _apply_task_visibility_rules(task, include_deleted=False)
                if visible is not None:
                    merged[row.id] = visible
        with _tasks_lock:
            cached_items = [(task_id, dict(task)) for task_id, task in _tasks.items()]
        for task_id, cached in cached_items:
            visible = _apply_task_visibility_rules(cached, include_deleted=False)
            if visible is None:
                continue
            current = merged.get(task_id)
            merged[task_id] = _prefer_cached_generation_task(current, visible) or visible
        return sorted(
            merged.items(),
            key=lambda item: _coerce_task_datetime(item[1].get("updated_at")),
            reverse=True,
        )[:normalized_limit]


def _shutdown_runtime_state() -> None:
    global _engine, _SessionFactory, _orchestrator, _publisher_manager, _runtime_settings

    _stop_automation_scheduler()
    if _orchestrator is not None:
        try:
            _orchestrator.llm_client.close()
        except Exception:  # noqa: BLE001
            logger.debug("Ignoring orchestrator LLM client shutdown error.", exc_info=True)
        try:
            _orchestrator.engine.dispose()
        except Exception:  # noqa: BLE001
            logger.debug("Ignoring orchestrator engine shutdown error.", exc_info=True)

    if _engine is not None:
        try:
            _engine.dispose()
        except Exception:  # noqa: BLE001
            logger.debug("Ignoring API engine shutdown error.", exc_info=True)

    _orchestrator = None
    _publisher_manager = None
    _runtime_settings = None
    _SessionFactory = None
    _engine = None


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _config, _engine, _SessionFactory, _orchestrator, _publisher_manager, _runtime_settings

    if _config is None:
        _config = Config.from_env()
    if _engine is None:
        db_path = os.environ.get("FORWIN_DB_PATH", _config.db_path)
        _config = _config.model_copy(update={"db_path": db_path})
        Path(_config.db_path).parent.mkdir(parents=True, exist_ok=True)
        _engine = get_engine(_config.db_path)
        init_db(_engine)
    if _SessionFactory is None:
        _SessionFactory = get_session_factory(_engine)
    if _orchestrator is None:
        _orchestrator = WritingOrchestrator(_config)
        with _SessionFactory() as bootstrap_session:
            created_envelopes = _orchestrator.arc_envelope_manager.backfill_missing_resolutions(
                session=bootstrap_session
            )
            if created_envelopes:
                bootstrap_session.commit()
                logger.info("Backfilled %d active arc envelopes.", created_envelopes)
            else:
                bootstrap_session.rollback()
    recovered_generation_ids = _recover_interrupted_generation_tasks()
    if recovered_generation_ids:
        logger.info(
            "Recovered %d interrupted generation tasks after restart.",
            len(recovered_generation_ids),
        )
    if _publisher_manager is None:
        _publisher_manager = PublisherManager(
            _SessionFactory,
            extension_api_key=_config.publisher_extension_api_key,
            preferred_client_id=_config.publisher_preferred_client_id,
        )
    _publisher_manager.requeue_interrupted_upload_jobs()
    if _runtime_settings is None:
        _runtime_settings = RuntimeSettingsStore(
            _config.runtime_settings_path,
            default_api_key=_config.minimax_api_key,
            default_base_url=_config.minimax_base_url,
            default_model=_config.minimax_model,
            default_operation_mode=_config.operation_mode,
            default_freeze_failed_candidates=_config.freeze_failed_candidates,
            default_min_chapter_chars=_config.min_chapter_chars,
            default_review_interval_chapters=_config.review_interval_chapters,
            default_progression_mode=_config.progression_mode,
            default_auto_band_checkpoint=_config.auto_band_checkpoint,
            default_band_warn_action=_config.band_warn_action,
            default_manual_checkpoints_enabled=_config.manual_checkpoints_enabled,
            default_future_constraints_enabled=_config.future_constraints_enabled,
        )
    _start_automation_scheduler()
    logger.info("ForWin API started. DB: %s", _config.db_path)
    try:
        yield
    finally:
        logger.info("ForWin API shutting down.")
        _shutdown_runtime_state()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="ForWin – 长篇中文网文生成系统",
    version="0.5.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def home_page():
    settings = build_home_page_settings(
        base_config=_config,
        runtime_settings=_runtime_settings,
    )
    backend_ready = (
        _publisher_manager.backend_ready_payload()
        if _publisher_manager is not None
        else {"extension_api_key_configured": False}
    )
    return HTMLResponse(
        render_home_page(
            has_api_key=bool(settings["api_key"]),
            base_url=str(settings["base_url"]),
            model=str(settings["model"]),
            operation_mode=str(settings["operation_mode"]),
            freeze_failed_candidates=bool(settings["freeze_failed_candidates"]),
            min_chapter_chars=max(500, int(settings.get("min_chapter_chars", 2500))),
            review_interval_chapters=max(0, int(settings.get("review_interval_chapters", 0))),
            extension_api_key_configured=bool(backend_ready.get("extension_api_key_configured")),
            extension_install_path="browser_extension/forwin-publisher",
        )
    )


@app.get("/publishers", response_class=HTMLResponse)
def publishers_page():
    backend_ready = (
        _publisher_manager.backend_ready_payload()
        if _publisher_manager is not None
        else {"extension_api_key_configured": False}
    )
    return HTMLResponse(
        render_publishers_page(
            backend_ready=backend_ready,
            extension_install_path="browser_extension/forwin-publisher",
        )
    )


@app.post("/api/generate", response_model=TaskResponse)
def generate(req: GenerateRequest):
    if not _config:
        raise HTTPException(503, "服务尚未初始化")

    runtime_config = build_runtime_config(
        req,
        base_config=_config,
        runtime_settings=_runtime_settings,
    )
    if not runtime_config.minimax_api_key:
        raise HTTPException(400, "MINIMAX_API_KEY 未设置。请在页面填写 API Key，或通过环境变量配置。")

    normalized_project_id = str(req.project_id or "").strip()
    task_title = (req.premise or "").strip()[:36] or "未命名生成任务"
    task_subtitle = f"{req.genre} · {req.num_chapters} 章"
    if normalized_project_id:
        session = _get_session()
        try:
            project = session.get(Project, normalized_project_id)
            if project is None:
                raise HTTPException(404, "项目不存在")
            if _project_has_active_generation_task(normalized_project_id, session=session):
                raise HTTPException(409, _generation_task_conflict_message(normalized_project_id))
            governance = _resolve_project_governance(
                project,
                overrides=_governance_request_payload(req),
                base_config=_config,
            )
            runtime_config = copy_config(
                runtime_config,
                operation_mode=governance.default_operation_mode,
                review_interval_chapters=governance.review_interval_chapters,
                progression_mode=governance.progression_mode,
                auto_band_checkpoint=governance.auto_band_checkpoint,
                band_warn_action=governance.band_warn_action,
                manual_checkpoints_enabled=governance.manual_checkpoints_enabled,
                future_constraints_enabled=governance.future_constraints_enabled,
            )
            task_title = project.title or task_title
            task_subtitle = f"书本生成 · {project.genre} · {req.num_chapters} 章"
        finally:
            session.close()

    try:
        task_id = _create_generation_task(
            premise=req.premise,
            genre=req.genre,
            num_chapters=req.num_chapters,
            runtime_config=runtime_config,
            project_id=normalized_project_id,
            title=task_title,
            subtitle=task_subtitle,
        )
    except ActiveGenerationTaskError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _serialize_task(task_id, _get_generation_task_or_404(task_id))


@app.get("/api/settings/llm", response_model=LLMSettingsResponse)
def get_llm_settings():
    if not _runtime_settings:
        raise HTTPException(503, "服务尚未初始化")
    payload = _runtime_settings.get()
    return _serialize_llm_settings(payload, message="已读取当前默认模型配置")


@app.post("/api/settings/llm", response_model=LLMSettingsResponse)
def save_llm_settings(req: LLMSettingsRequest):
    if not _runtime_settings:
        raise HTTPException(503, "服务尚未初始化")
    payload = _runtime_settings.save(
        api_key=req.api_key,
        base_url=req.base_url,
        model=req.model,
        operation_mode=req.operation_mode,
        freeze_failed_candidates=req.freeze_failed_candidates,
        min_chapter_chars=req.min_chapter_chars,
        review_interval_chapters=req.review_interval_chapters,
        progression_mode=req.progression_mode,
        auto_band_checkpoint=req.auto_band_checkpoint,
        band_warn_action=req.band_warn_action,
        manual_checkpoints_enabled=req.manual_checkpoints_enabled,
        future_constraints_enabled=req.future_constraints_enabled,
    )
    return _serialize_llm_settings(
        payload,
        message="默认模型配置已保存",
    )


@app.post("/api/settings/llm/preferences", response_model=LLMSettingsResponse)
def save_llm_preferences(req: LLMPreferencesRequest):
    if not _runtime_settings:
        raise HTTPException(503, "服务尚未初始化")
    payload = _runtime_settings.save(
        operation_mode=req.operation_mode,
        freeze_failed_candidates=req.freeze_failed_candidates,
        min_chapter_chars=req.min_chapter_chars,
        review_interval_chapters=req.review_interval_chapters,
        progression_mode=req.progression_mode,
        auto_band_checkpoint=req.auto_band_checkpoint,
        band_warn_action=req.band_warn_action,
        manual_checkpoints_enabled=req.manual_checkpoints_enabled,
        future_constraints_enabled=req.future_constraints_enabled,
    )
    return _serialize_llm_settings(payload, message="运行偏好已保存")


@app.post("/api/settings/llm/profiles", response_model=LLMSettingsResponse)
def save_llm_profile(req: LLMProfileUpsertRequest):
    if not _runtime_settings:
        raise HTTPException(503, "服务尚未初始化")
    payload = _runtime_settings.save_profile(
        profile_id=req.profile_id,
        name=req.name,
        api_key=req.api_key,
        base_url=req.base_url,
        model=req.model,
        set_as_default=req.set_as_default,
    )
    return _serialize_llm_settings(payload, message="模型配置已保存")


@app.post("/api/settings/llm/default-profile", response_model=LLMSettingsResponse)
def set_default_llm_profile(req: LLMDefaultProfileRequest):
    if not _runtime_settings:
        raise HTTPException(503, "服务尚未初始化")
    try:
        payload = _runtime_settings.set_default_profile(req.profile_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return _serialize_llm_settings(payload, message="默认模型已切换")


@app.delete("/api/settings/llm/profiles/{profile_id}", response_model=LLMSettingsResponse)
def delete_llm_profile(profile_id: str):
    if not _runtime_settings:
        raise HTTPException(503, "服务尚未初始化")
    try:
        payload = _runtime_settings.delete_profile(profile_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _serialize_llm_settings(payload, message="模型配置已删除")


@app.get("/api/tasks/active-generation-check", response_model=ActiveGenerationTaskCheckResponse)
def active_generation_task_check(project_id: str = ""):
    normalized_project_id = str(project_id or "").strip()
    active_ids: list[str] = []
    for task_id, task in _list_generation_tasks(200):
        if str(task.get("task_kind", "generation")) != "generation":
            continue
        if task.get("deleted"):
            continue
        if normalized_project_id and str(task.get("project_id", "") or "").strip() != normalized_project_id:
            continue
        if _task_is_terminal(str(task.get("status", "")).strip()):
            continue
        active_ids.append(task_id)
    return ActiveGenerationTaskCheckResponse(
        has_active_generation_task=bool(active_ids),
        active_task_ids=active_ids,
        active_count=len(active_ids),
        safe_to_restart=not active_ids,
        message=(
            "存在 active generation task，重启前请等待、暂停或终止。"
            if active_ids
            else "当前没有 active generation task，可以安全重启。"
        ),
    )


@app.get("/api/tasks/{task_id}", response_model=TaskResponse)
def get_task(task_id: str):
    task = _get_generation_task_or_404(task_id)
    return _serialize_task(task_id, task)


@app.get("/api/tasks", response_model=list[TaskSummaryResponse])
def list_tasks(limit: int = 30):
    ordered = _list_generation_tasks(limit)
    return [_serialize_task(task_id, task) for task_id, task in ordered]


@app.get("/api/task-center/items", response_model=list[TaskCenterItemResponse])
def list_task_center_items(limit: int = 50):
    normalized_limit = max(1, min(int(limit or 50), 100))
    generation_items = [
        _serialize_generation_task_center_item(task_id, task)
        for task_id, task in _list_generation_tasks(normalized_limit)
    ]
    project_items = _list_project_backed_task_items(normalized_limit)
    upload_items = [
        _serialize_upload_task_center_item(item)
        for item in _publisher_manager.list_upload_jobs(
            limit=normalized_limit,
            include_deleted=False,
        )
    ]
    combined = generation_items + project_items + upload_items
    combined.sort(key=lambda item: item.updated_at or item.created_at, reverse=True)
    return combined[:normalized_limit]


@app.get("/api/task-center/items/{task_kind}/{task_id}", response_model=TaskCenterItemResponse)
def get_task_center_item(task_kind: str, task_id: str):
    normalized_kind = str(task_kind or "").strip()
    if normalized_kind == "generation":
        project_task_id = _parse_project_task_id(task_id)
        if project_task_id:
            return _get_project_backed_task_item_or_404(task_id)
        task = _get_generation_task_or_404(task_id)
        return _serialize_generation_task_center_item(task_id, task)
    if normalized_kind == "upload":
        try:
            payload = _publisher_manager.get_upload_job(task_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return _serialize_upload_task_center_item(payload)
    raise HTTPException(404, "任务类型不存在")


@app.post("/api/tasks/{task_id}/terminate", response_model=TaskMutationResponse)
def terminate_task(task_id: str):
    task = _get_generation_task_or_404(task_id)
    if not _task_is_terminable(task):
        raise HTTPException(400, "当前任务状态不支持终止")
    project_id = str(task.get("project_id", "") or "").strip()
    if project_id:
        with _get_session() as session:
            parent = _latest_related_decision_event(
                session,
                project_id=project_id,
                related_object_type="generation_task",
                related_object_id=task_id,
            )
            _log_decision_event(
                session,
                project_id=project_id,
                task_id=task_id,
                scope="task",
                event_family="audit_action",
                event_type=DecisionEventType.TERMINATE_REQUESTED,
                actor_type="manual_ui",
                summary="已请求终止生成任务。",
                related_object_type="generation_task",
                related_object_id=task_id,
                parent_event_id=str(parent.id if parent is not None else ""),
                causal_root_id=str(parent.causal_root_id if parent is not None else ""),
            )
            session.commit()
    _update_task(
        task_id,
        cancel_requested=True,
        status="terminating",
        current_stage="terminating",
        message="已请求终止生成任务，系统会在下一个安全检查点停止。",
    )
    updated = _get_generation_task_or_404(task_id)
    return TaskMutationResponse(
        ok=True,
        task_kind="generation",
        task_id=task_id,
        status=str(updated.get("status", "")),
        message=str(updated.get("message", "")),
    )


@app.post("/api/tasks/{task_id}/pause", response_model=TaskMutationResponse)
def pause_task(task_id: str):
    task = _get_generation_task_or_404(task_id)
    if not _task_is_pausable(task):
        raise HTTPException(400, "当前任务状态不支持安全暂停")
    project_id = str(task.get("project_id", "") or "").strip()
    if project_id:
        with _get_session() as session:
            parent = _latest_related_decision_event(
                session,
                project_id=project_id,
                related_object_type="generation_task",
                related_object_id=task_id,
            )
            _log_decision_event(
                session,
                project_id=project_id,
                task_id=task_id,
                scope="task",
                event_family="audit_action",
                event_type=DecisionEventType.PAUSE_REQUESTED,
                actor_type="manual_ui",
                summary="已请求安全暂停生成任务。",
                related_object_type="generation_task",
                related_object_id=task_id,
                parent_event_id=str(parent.id if parent is not None else ""),
                causal_root_id=str(parent.causal_root_id if parent is not None else ""),
            )
            session.commit()
    _update_task(
        task_id,
        pause_requested=True,
        message="已请求安全暂停，系统会在下一个安全检查点保存进度并暂停。",
    )
    updated = _get_generation_task_or_404(task_id)
    return TaskMutationResponse(
        ok=True,
        task_kind="generation",
        task_id=task_id,
        status=str(updated.get("status", "")),
        message=str(updated.get("message", "")),
    )


@app.delete("/api/tasks/{task_id}", response_model=TaskMutationResponse)
def delete_task(task_id: str):
    task = _get_generation_task_or_404(task_id)
    if not _task_is_deletable(task):
        raise HTTPException(400, "只有终态任务可以删除")
    _update_task(task_id, deleted=True, message="任务已删除。")
    return TaskMutationResponse(
        ok=True,
        task_kind="generation",
        task_id=task_id,
        status=str(task.get("status", "")),
        message="任务已删除。",
    )


@app.post("/api/tasks/bulk-delete", response_model=BulkDeleteResponse)
def bulk_delete_tasks(req: TaskBulkDeleteRequest):
    deleted_ids: list[str] = []
    skipped_ids: list[str] = []
    seen: set[str] = set()

    for item in req.items:
        task_kind = str(item.task_kind or "").strip()
        task_id = str(item.task_id or "").strip()
        key = f"{task_kind}:{task_id}"
        if not task_kind or not task_id or key in seen:
            continue
        seen.add(key)
        if task_kind == "generation":
            try:
                task = _get_generation_task_or_404(task_id)
            except HTTPException:
                skipped_ids.append(key)
                continue
            if not _task_is_deletable(task):
                skipped_ids.append(key)
                continue
            _update_task(task_id, deleted=True, message="任务已删除。")
            deleted_ids.append(key)
            continue
        if task_kind == "upload":
            try:
                _publisher_manager.delete_upload_job(task_id)
            except ValueError:
                skipped_ids.append(key)
                continue
            deleted_ids.append(key)
            continue
        skipped_ids.append(key)

    return BulkDeleteResponse(
        ok=True,
        deleted_count=len(deleted_ids),
        skipped_count=len(skipped_ids),
        deleted_ids=deleted_ids,
        skipped_ids=skipped_ids,
        message=f"已删除 {len(deleted_ids)} 条任务，跳过 {len(skipped_ids)} 条。",
    )


def _build_extension_package() -> bytes:
    extension_root = Path.cwd() / "browser_extension" / "forwin-publisher"
    if not extension_root.exists():
        raise HTTPException(404, "浏览器扩展目录不存在。")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(extension_root.rglob("*")):
            if path.is_dir():
                continue
            archive.write(path, arcname=Path("forwin-publisher") / path.relative_to(extension_root))
    buffer.seek(0)
    return buffer.getvalue()


@app.get("/api/publishers/extension-package")
def download_publisher_extension_package():
    payload = _build_extension_package()
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="forwin-publisher-extension.zip"',
        },
    )


@app.get("/api/publishers/platforms", response_model=list[PublisherPlatformInfo])
def list_publisher_platforms():
    return [PublisherPlatformInfo(**item) for item in _publisher_manager.list_platforms()]


def _require_extension_auth(x_forwin_extension_key: str | None) -> None:
    try:
        _publisher_manager.verify_extension_api_key(x_forwin_extension_key)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(401, str(exc)) from exc


@app.post("/api/publishers/upload-jobs", response_model=PublisherUploadJobResponse)
def create_publisher_upload_job(req: PublisherUploadJobCreateRequest):
    try:
        payload = _publisher_manager.create_upload_job(
            project_id=str(req.project_id or "").strip(),
            platform=req.platform,
            book_name=req.book_name,
            chapter_title=req.chapter_title,
            body=req.body,
            upload_url=req.upload_url,
            publish=req.publish,
            create_if_missing=req.create_if_missing,
            book_meta=req.book_meta.model_dump() if req.book_meta else None,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return PublisherUploadJobResponse(**payload)


@app.get("/api/publishers/upload-jobs/{job_id}", response_model=PublisherUploadJobResponse)
def get_publisher_upload_job(job_id: str):
    try:
        payload = _publisher_manager.get_upload_job(job_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return PublisherUploadJobResponse(**payload)


@app.get("/api/publishers/upload-jobs", response_model=list[PublisherUploadJobResponse])
def list_publisher_upload_jobs(
    status: str = "",
    platform: str = "",
    limit: int = 30,
):
    payload = _publisher_manager.list_upload_jobs(
        status=status,
        platform=platform,
        limit=limit,
    )
    return [PublisherUploadJobResponse(**item) for item in payload]


@app.post("/api/publishers/upload-jobs/{job_id}/terminate", response_model=TaskMutationResponse)
def terminate_publisher_upload_job(job_id: str):
    try:
        payload = _publisher_manager.terminate_upload_job(job_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return TaskMutationResponse(
        ok=True,
        task_kind="upload",
        task_id=job_id,
        status=str(payload.get("status", "")),
        message=str(payload.get("message", "")),
    )


@app.delete("/api/publishers/upload-jobs/{job_id}", response_model=TaskMutationResponse)
def delete_publisher_upload_job(job_id: str):
    try:
        _publisher_manager.delete_upload_job(job_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return TaskMutationResponse(
        ok=True,
        task_kind="upload",
        task_id=job_id,
        status="deleted",
        message="任务已删除。",
    )


@app.post("/api/publishers/extension/heartbeat", response_model=ExtensionHeartbeatResponse)
def publisher_extension_heartbeat(
    req: ExtensionHeartbeatRequest,
    x_forwin_extension_key: str | None = Header(default=None),
):
    _require_extension_auth(x_forwin_extension_key)
    payload = _publisher_manager.record_extension_heartbeat(
        client_id=req.client_id,
        extension_version=req.extension_version,
        browser_name=req.browser_name,
        browser_version=req.browser_version,
        backend_base_url=req.backend_base_url,
        platforms=[
            {
                "platform": item.platform,
                "connected": item.connected,
                "login_method": item.login_method,
                "last_error": item.last_error,
                **item.raw_state,
            }
            for item in req.platforms
        ],
    )
    return ExtensionHeartbeatResponse(**payload)


@app.post("/api/publishers/extension/session-sync", response_model=ExtensionSessionSyncResponse)
def publisher_extension_session_sync(
    req: ExtensionSessionSyncRequest,
    x_forwin_extension_key: str | None = Header(default=None),
):
    _require_extension_auth(x_forwin_extension_key)
    payload = _publisher_manager.record_browser_session(
        client_id=req.client_id,
        platform=req.platform,
        cookies=[item.model_dump() for item in req.cookies],
    )
    return ExtensionSessionSyncResponse(**payload)


@app.get(
    "/api/publishers/extension/browser-sessions/{platform}",
    response_model=ExtensionBrowserSessionResponse | None,
)
def publisher_extension_get_browser_session(
    platform: str,
    x_forwin_extension_key: str | None = Header(default=None),
):
    _require_extension_auth(x_forwin_extension_key)
    payload = _publisher_manager.get_browser_session(platform)
    if payload is None:
        return None
    return ExtensionBrowserSessionResponse(**payload)


@app.post("/api/publishers/upload-jobs/{job_id}/result", response_model=PublisherUploadJobResponse)
def update_publisher_upload_job_result(
    job_id: str,
    req: UploadJobResultRequest,
    x_forwin_extension_key: str | None = Header(default=None),
):
    _require_extension_auth(x_forwin_extension_key)
    try:
        payload = _publisher_manager.update_upload_job_result(
            job_id=job_id,
            client_id=req.client_id,
            status=req.status,
            message=req.message,
            current_url=req.current_url,
            error=req.error,
            result_payload=req.result_payload,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return PublisherUploadJobResponse(**payload)


@app.post("/api/publishers/extension/upload-jobs/claim", response_model=ExtensionClaimUploadJobResponse)
def claim_publisher_upload_job(
    req: ExtensionClaimUploadJobRequest,
    x_forwin_extension_key: str | None = Header(default=None),
):
    _require_extension_auth(x_forwin_extension_key)
    payload = _publisher_manager.claim_next_upload_job(
        client_id=req.client_id,
        connected_platforms=req.connected_platforms,
    )
    if payload is None:
        return ExtensionClaimUploadJobResponse(found=False, job=None)
    return ExtensionClaimUploadJobResponse(
        found=True,
        job=PublisherUploadJobResponse(**payload),
    )


@app.post("/api/publishers/extension/comment-sync-jobs/claim", response_model=ExtensionClaimCommentSyncJobResponse)
def claim_publisher_comment_sync_job(
    req: ExtensionClaimCommentSyncJobRequest,
    x_forwin_extension_key: str | None = Header(default=None),
):
    _require_extension_auth(x_forwin_extension_key)
    payload = _publisher_manager.claim_next_comment_sync_job(
        client_id=req.client_id,
        connected_platforms=req.connected_platforms,
    )
    if payload is None:
        return ExtensionClaimCommentSyncJobResponse(found=False, job=None)
    return ExtensionClaimCommentSyncJobResponse(
        found=True,
        job=PublisherCommentSyncJobResponse(**payload),
    )


@app.post("/api/publishers/comment-sync-jobs", response_model=PublisherCommentSyncJobResponse)
def create_publisher_comment_sync_job(req: PublisherCommentSyncJobRequest):
    try:
        payload = _publisher_manager.create_comment_sync_job(
            project_id=req.project_id,
            platform=req.platform,
            work_id=req.work_id,
            work_name=req.work_name,
            chapter_id=req.chapter_id,
            chapter_title=req.chapter_title,
            limit=req.limit,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return PublisherCommentSyncJobResponse(**payload)


@app.post("/api/publishers/comment-sync-jobs/{job_id}/result", response_model=PublisherCommentSyncJobResponse)
def update_publisher_comment_sync_job_result(
    job_id: str,
    req: CommentSyncJobResultRequest,
    x_forwin_extension_key: str | None = Header(default=None),
):
    _require_extension_auth(x_forwin_extension_key)
    try:
        payload = _publisher_manager.update_comment_sync_job_result(
            job_id=job_id,
            client_id=req.client_id,
            status=req.status,
            message=req.message,
            error=req.error,
            result_payload=req.result_payload,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return PublisherCommentSyncJobResponse(**payload)


@app.post("/api/publishers/extension/comments/batch", response_model=ExtensionCommentsBatchResponse)
def ingest_publisher_comments_batch(
    req: ExtensionCommentsBatchRequest,
    x_forwin_extension_key: str | None = Header(default=None),
):
    _require_extension_auth(x_forwin_extension_key)
    try:
        payload = _publisher_manager.ingest_comments_batch(
            client_id=req.client_id,
            platform=req.platform,
            job_id=req.job_id,
            comments=[item.model_dump() for item in req.comments],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return ExtensionCommentsBatchResponse(**payload)


@app.get("/api/projects", response_model=list[ProjectSummary])
def list_projects():
    session = _get_session()
    try:
        projects = session.execute(
            select(Project).order_by(Project.created_at.desc())
        ).scalars().all()
        return build_project_summaries(
            session=session,
            projects=projects,
            display_datetime=_display_datetime,
            review_interval_chapters=max(0, int(_config.review_interval_chapters if _config else 0)),
        )
    finally:
        session.close()


@app.post("/api/projects", response_model=ProjectCreateResponse)
def create_project(req: ProjectCreateRequest):
    session = _get_session()
    try:
        title = str(req.title or "").strip()
        premise = str(req.premise or "").strip()
        if not title:
            raise HTTPException(400, "书名不能为空")
        if not premise:
            raise HTTPException(400, "作品 premise 不能为空")

        publish_bindings = [
            {
                "platform": str(binding.platform or "").strip(),
                "book_name": str(binding.book_name or "").strip() or title,
                "upload_url": str(binding.upload_url or "").strip(),
                "create_if_missing": bool(binding.create_if_missing),
                "book_meta": binding.book_meta.model_dump(mode="json"),
            }
            for binding in req.publish_bindings
            if str(binding.platform or "").strip()
        ]
        if publish_bindings:
            default_publish = publish_bindings[0]
        else:
            publish_platform = str(req.publish_platform or "").strip()
            publish_book_name = str(req.publish_book_name or "").strip() or title
            publish_upload_url = str(req.publish_upload_url or "").strip()
            platform_has_existing_book = bool(req.platform_has_existing_book)
            default_publish = {
                "platform": publish_platform,
                "book_name": publish_book_name,
                "upload_url": publish_upload_url,
                "create_if_missing": bool(publish_platform) and not platform_has_existing_book,
            }
            if publish_platform:
                publish_bindings = [default_publish]
        automation = normalize_project_automation(
            {
                "publish": default_publish,
                "publish_bindings": publish_bindings,
            }
        )
        governance = new_project_governance(
            default_operation_mode=_config.operation_mode if _config is not None else "blackbox",
            review_interval_chapters=_config.review_interval_chapters if _config is not None else 0,
        )

        project = Project(
            title=title,
            premise=premise,
            genre=str(req.genre or "").strip() or "玄幻",
            setting_summary=str(req.setting_summary or "").strip(),
            target_total_chapters=max(1, int(req.target_total_chapters or 1)),
            automation_json=json.dumps(
                automation.model_dump(mode="json"),
                ensure_ascii=False,
            ),
            governance_json=json.dumps(
                governance.model_dump(mode="json"),
                ensure_ascii=False,
            ),
        )
        session.add(project)
        session.flush()
        _log_decision_event(
            session,
            project_id=project.id,
            event_family="business_event",
            event_type=DecisionEventType.PROJECT_CREATED,
            summary="项目已创建并启用默认治理策略。",
            payload={"governance": governance.model_dump(mode="json")},
        )
        session.commit()
        session.refresh(project)
        return ProjectCreateResponse(
            ok=True,
            project_id=project.id,
            title=project.title,
            target_total_chapters=int(project.target_total_chapters or 3),
            message=f"书本《{project.title}》已创建。",
        )
    finally:
        session.close()


@app.delete("/api/projects/{project_id}", response_model=ProjectDeleteResponse)
def delete_project(project_id: str):
    session = _get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        blockers = _project_delete_blockers(project_id, session=session)
        if blockers:
            raise HTTPException(409, _project_delete_conflict_message(blockers))
        _delete_project(session, project_id)
        session.commit()
        return ProjectDeleteResponse(
            ok=True,
            project_id=project_id,
            message=f"项目《{project.title}》已删除。",
        )
    finally:
        session.close()


@app.post("/api/projects/bulk-delete", response_model=BulkDeleteResponse)
def bulk_delete_projects(req: ProjectBulkDeleteRequest):
    session = _get_session()
    deleted_ids: list[str] = []
    skipped_ids: list[str] = []
    try:
        seen: set[str] = set()
        for project_id in req.project_ids:
            normalized = str(project_id or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            project = session.get(Project, normalized)
            if project is None:
                skipped_ids.append(normalized)
                continue
            if _project_delete_blockers(normalized, session=session):
                skipped_ids.append(normalized)
                continue
            _delete_project(session, normalized)
            deleted_ids.append(normalized)
        session.commit()
        return BulkDeleteResponse(
            ok=True,
            deleted_count=len(deleted_ids),
            skipped_count=len(skipped_ids),
            deleted_ids=deleted_ids,
            skipped_ids=skipped_ids,
            message=f"已删除 {len(deleted_ids)} 本书，跳过 {len(skipped_ids)} 本。",
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.get("/api/projects/{project_id}", response_model=ProjectDetail)
def get_project(project_id: str):
    session = _get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        return build_project_detail(
            session=session,
            project=project,
            display_datetime=_display_datetime,
            review_interval_chapters=max(0, int(_config.review_interval_chapters if _config else 0)),
        )
    finally:
        session.close()


@app.post("/api/projects/{project_id}/continue-generation", response_model=TaskResponse)
def continue_project_generation(project_id: str, req: ProjectContinueGenerationRequest | None = None):
    if not _config:
        raise HTTPException(503, "服务尚未初始化")
    runtime_config = build_saved_runtime_config(
        base_config=_config,
        runtime_settings=_runtime_settings,
    )
    session = _get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        governance = _resolve_project_governance(
            project,
            overrides=_governance_request_payload(req),
            base_config=_config,
        )
        runtime_config = copy_config(
            runtime_config,
            operation_mode=governance.default_operation_mode,
            review_interval_chapters=governance.review_interval_chapters,
            progression_mode=governance.progression_mode,
            auto_band_checkpoint=governance.auto_band_checkpoint,
            band_warn_action=governance.band_warn_action,
            manual_checkpoints_enabled=governance.manual_checkpoints_enabled,
            future_constraints_enabled=governance.future_constraints_enabled,
        )
        if _project_has_active_generation_task(project_id, session=session):
            raise HTTPException(409, _generation_task_conflict_message(project_id))
        plans = session.execute(
            select(ChapterPlan)
            .where(ChapterPlan.project_id == project_id)
            .order_by(ChapterPlan.chapter_number.asc())
        ).scalars().all()
        waiting_review = [plan.chapter_number for plan in plans if plan.status == "needs_review"]
        if waiting_review:
            raise HTTPException(409, f"仍有章节等待 review：{', '.join(str(item) for item in waiting_review)}")
        remaining = [plan.chapter_number for plan in plans if plan.status in {"planned", "failed"}]
        if not remaining:
            raise HTTPException(400, "没有剩余章节需要继续生成")
        project_detail = build_project_detail(
            session=session,
            project=project,
            display_datetime=_display_datetime,
            review_interval_chapters=governance.review_interval_chapters,
        )
        if project_detail.blocking_reason.code:
            _log_decision_event(
                session,
                project_id=project_id,
                event_family="evaluation_verdict",
                event_type=DecisionEventType.HARD_GATE_HIT,
                actor_type="api",
                scope="project",
                summary=project_detail.blocking_reason.message or project_detail.blocking_reason.code,
                payload={"blocking_reason": project_detail.blocking_reason.code},
                band_id=project_detail.blocking_reason.band_id,
                chapter_number=int(project_detail.blocking_reason.chapter_number or 0),
                related_object_type="project",
                related_object_id=project_id,
            )
            session.commit()
            raise HTTPException(409, project_detail.blocking_reason.message)
        max_chapters = req.max_chapters if req is not None else None
        task_id = _create_continue_generation_task(
            project_id=project_id,
            runtime_config=runtime_config,
            requested_chapters=len(plans),
            max_chapters=max_chapters,
            title=project.title,
            subtitle=f"继续生成 · {project.genre}",
            message="准备继续生成剩余章节。",
        )
    except ActiveGenerationTaskError as exc:
        raise HTTPException(409, str(exc)) from exc
    finally:
        session.close()
    return _serialize_task(task_id, _get_generation_task_or_404(task_id))


@app.put("/api/projects/{project_id}/automation", response_model=ProjectAutomationUpdateResponse)
def update_project_automation(project_id: str, req: ProjectAutomationUpdateRequest):
    session = _get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        current = normalize_project_automation(project.automation_json)
        payload = current.model_dump(mode="json")
        payload.update(
            {
                "enabled": bool(req.enabled),
                "daily_start_time": req.daily_start_time,
                "daily_chapter_quota": req.daily_chapter_quota,
                "auto_publish": bool(req.auto_publish),
            }
        )
        if req.publish is not None:
            payload["publish"] = req.publish.model_dump(mode="json")
        if req.publish_bindings is not None:
            payload["publish_bindings"] = [
                binding.model_dump(mode="json")
                for binding in req.publish_bindings
            ]
        updated = normalize_project_automation(payload)
        stored = _persist_project_automation(session, project, updated)
        session.commit()
        return ProjectAutomationUpdateResponse(
            ok=True,
            project_id=project_id,
            automation=stored,
            message="书本自动化设置已保存。",
        )
    finally:
        session.close()


@app.get("/api/projects/{project_id}/governance", response_model=ProjectGovernanceResponse)
def get_project_governance(project_id: str):
    session = _get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        governance = _resolve_project_governance(project, base_config=_config)
        return ProjectGovernanceResponse(
            ok=True,
            project_id=project_id,
            governance=governance,
            message="已读取项目治理设置。",
        )
    finally:
        session.close()


@app.put("/api/projects/{project_id}/governance", response_model=ProjectGovernanceResponse)
def update_project_governance(project_id: str, req: ProjectGovernanceUpdateRequest):
    session = _get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        reason = _require_reason(req.reason, action="修改项目治理设置")
        governance = _resolve_project_governance(
            project,
            overrides=_governance_request_payload(req),
            base_config=_config,
        )
        stored = _persist_project_governance(session, project, governance)
        _log_decision_event(
            session,
            project_id=project_id,
            event_family="audit_action",
            event_type=DecisionEventType.GOVERNANCE_UPDATED,
            actor_type="manual_ui",
            summary="项目治理设置已更新。",
            reason=reason,
            payload={"governance": stored.model_dump(mode="json")},
        )
        session.commit()
        return ProjectGovernanceResponse(
            ok=True,
            project_id=project_id,
            governance=stored,
            message="项目治理设置已保存。",
        )
    finally:
        session.close()


@app.post("/api/projects/{project_id}/manual-checkpoints", response_model=BandCheckpointDetail)
def create_manual_checkpoint(project_id: str, req: ManualCheckpointRequest):
    session = _get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        reason = _require_reason(req.reason, action="创建 manual checkpoint")
        governance = _resolve_project_governance(project, base_config=_config)
        if not governance.manual_checkpoints_enabled:
            raise HTTPException(409, "当前项目未启用 manual checkpoint。")
        boundary_kind = str(req.boundary_kind or "").strip()
        if boundary_kind not in {"chapter_start", "chapter_accepted", "band_end"}:
            raise HTTPException(400, "manual checkpoint 仅支持章开始前、章 accepted 后、band 结束处。")
        active_arc = session.execute(
            select(Base.metadata.tables["arc_plan_versions"].c.id)
            .where(
                Base.metadata.tables["arc_plan_versions"].c.project_id == project_id,
                Base.metadata.tables["arc_plan_versions"].c.status == "active",
            )
            .order_by(Base.metadata.tables["arc_plan_versions"].c.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        if active_arc is None:
            raise HTTPException(409, "当前项目没有 active arc。")
        chapter_number = max(0, int(req.boundary_chapter or 0))
        band = session.execute(
            select(BandExperiencePlan)
            .where(
                BandExperiencePlan.project_id == project_id,
                BandExperiencePlan.arc_id == active_arc,
                BandExperiencePlan.chapter_start <= max(1, chapter_number or 1),
                BandExperiencePlan.chapter_end >= max(1, chapter_number or 1),
            )
            .order_by(BandExperiencePlan.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if band is None and boundary_kind == "band_end":
            band = session.execute(
                select(BandExperiencePlan)
                .where(
                    BandExperiencePlan.project_id == project_id,
                    BandExperiencePlan.arc_id == active_arc,
                    BandExperiencePlan.chapter_end == chapter_number,
                )
                .order_by(BandExperiencePlan.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
        if band is None:
            raise HTTPException(400, "未找到对应 band，manual checkpoint 只能落在章边界或 band 边界。")
        if boundary_kind == "band_end":
            chapter_number = int(band.chapter_end or 0)
        row = BandCheckpoint(
            project_id=project_id,
            arc_id=str(active_arc),
            band_id=band.band_id,
            chapter_start=int(band.chapter_start or 0),
            chapter_end=int(band.chapter_end or 0),
            trigger_source="manual_boundary",
            boundary_kind=boundary_kind,
            boundary_chapter=chapter_number,
            status="pending",
            summary="人工 checkpoint 已创建，等待边界命中或人工处理。",
            reason=reason,
            issues_json="[]",
        )
        session.add(row)
        session.flush()
        _log_decision_event(
            session,
            project_id=project_id,
            band_id=band.band_id,
            chapter_number=chapter_number,
            event_family="audit_action",
            event_type=DecisionEventType.MANUAL_CHECKPOINT_CREATED,
            actor_type="manual_ui",
            scope="band",
            summary="已插入人工 checkpoint。",
            reason=reason,
            related_object_type="band_checkpoint",
            related_object_id=row.id,
        )
        session.commit()
        session.refresh(row)
        return _serialize_band_checkpoint(row, session=session)
    finally:
        session.close()


@app.get("/api/projects/{project_id}/bands/{band_id}/checkpoint", response_model=BandCheckpointDetail)
def get_band_checkpoint(project_id: str, band_id: str):
    session = _get_session()
    try:
        row = _latest_band_checkpoint_row(session, project_id=project_id, band_id=band_id)
        if row is None:
            raise HTTPException(404, "band checkpoint 不存在")
        return _serialize_band_checkpoint(row, session=session)
    finally:
        session.close()


@app.post("/api/projects/{project_id}/bands/{band_id}/checkpoint/approve", response_model=BandCheckpointDetail)
def approve_band_checkpoint(project_id: str, band_id: str, req: BandCheckpointApproveRequest):
    session = _get_session()
    try:
        row = _latest_band_checkpoint_row(session, project_id=project_id, band_id=band_id)
        if row is None:
            raise HTTPException(404, "band checkpoint 不存在")
        parent = _latest_related_decision_event(
            session,
            project_id=project_id,
            related_object_type="band_checkpoint",
            related_object_id=row.id,
        )
        next_status = str(req.status or "overridden").strip() or "overridden"
        reason = _require_reason(
            req.reason,
            action="pass checkpoint" if next_status == "pass" else "override checkpoint",
        )
        row.status = next_status
        row.reason = reason
        session.add(row)
        session.flush()
        _log_decision_event(
            session,
            project_id=project_id,
            band_id=band_id,
            chapter_number=int(row.boundary_chapter or 0),
            event_family="audit_action",
            event_type=DecisionEventType.BAND_CHECKPOINT_APPROVED if next_status == "pass" else DecisionEventType.BAND_CHECKPOINT_OVERRIDDEN,
            actor_type="manual_ui",
            scope="band",
            summary="band checkpoint 已人工放行。",
            reason=reason,
            related_object_type="band_checkpoint",
            related_object_id=row.id,
            parent_event_id=str(parent.id if parent is not None else ""),
            causal_root_id=str(parent.causal_root_id if parent is not None else ""),
        )
        session.commit()
        session.refresh(row)
        return _serialize_band_checkpoint(row, session=session)
    finally:
        session.close()


@app.get(
    "/api/projects/{project_id}/chapters/{chapter_number}/task-contract",
    response_model=TaskContractResponse,
)
def get_chapter_task_contract(project_id: str, chapter_number: int):
    session = _get_session()
    try:
        plan = session.execute(
            select(ChapterPlan).where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number == chapter_number,
            )
        ).scalar_one_or_none()
        if plan is None:
            raise HTTPException(404, f"第{chapter_number}章不存在")
        return TaskContractResponse(
            project_id=project_id,
            scope="chapter",
            chapter_number=chapter_number,
            items=load_plan_task_contract(getattr(plan, "task_contract_json", "[]")),
            message="已读取 chapter task contract。",
        )
    finally:
        session.close()


@app.put(
    "/api/projects/{project_id}/chapters/{chapter_number}/task-contract",
    response_model=TaskContractResponse,
)
def update_chapter_task_contract(project_id: str, chapter_number: int, req: TaskContractUpdateRequest):
    session = _get_session()
    try:
        plan = session.execute(
            select(ChapterPlan).where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number == chapter_number,
            )
        ).scalar_one_or_none()
        if plan is None:
            raise HTTPException(404, f"第{chapter_number}章不存在")
        reason = _require_reason(req.reason, action="更新 chapter task contract")
        plan.task_contract_json = plan_task_contract_to_json(req.items)
        session.add(plan)
        session.flush()
        _log_decision_event(
            session,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="audit_action",
            event_type=DecisionEventType.PLAN_TASK_CONTRACT_UPDATED,
            actor_type="manual_ui",
            scope="chapter",
            summary=f"第{chapter_number}章 task contract 已更新。",
            reason=reason,
            payload={"scope": "chapter", "item_count": len(req.items)},
            related_object_type="chapter_plan",
            related_object_id=plan.id,
        )
        session.commit()
        return TaskContractResponse(
            project_id=project_id,
            scope="chapter",
            chapter_number=chapter_number,
            items=list(req.items),
            message="chapter task contract 已保存。",
        )
    finally:
        session.close()


@app.get(
    "/api/projects/{project_id}/bands/{band_id}/task-contract",
    response_model=TaskContractResponse,
)
def get_band_task_contract(project_id: str, band_id: str):
    session = _get_session()
    try:
        row = session.execute(
            select(BandExperiencePlan)
            .where(
                BandExperiencePlan.project_id == project_id,
                BandExperiencePlan.band_id == band_id,
            )
            .order_by(BandExperiencePlan.created_at.desc(), BandExperiencePlan.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "band 不存在")
        return TaskContractResponse(
            project_id=project_id,
            scope="band",
            band_id=band_id,
            items=load_plan_task_contract(getattr(row, "task_contract_json", "[]")),
            message="已读取 band task contract。",
        )
    finally:
        session.close()


@app.put(
    "/api/projects/{project_id}/bands/{band_id}/task-contract",
    response_model=TaskContractResponse,
)
def update_band_task_contract(project_id: str, band_id: str, req: TaskContractUpdateRequest):
    session = _get_session()
    try:
        row = session.execute(
            select(BandExperiencePlan)
            .where(
                BandExperiencePlan.project_id == project_id,
                BandExperiencePlan.band_id == band_id,
            )
            .order_by(BandExperiencePlan.created_at.desc(), BandExperiencePlan.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "band 不存在")
        reason = _require_reason(req.reason, action="更新 band task contract")
        row.task_contract_json = plan_task_contract_to_json(req.items)
        session.add(row)
        session.flush()
        _log_decision_event(
            session,
            project_id=project_id,
            band_id=band_id,
            event_family="audit_action",
            event_type=DecisionEventType.PLAN_TASK_CONTRACT_UPDATED,
            actor_type="manual_ui",
            scope="band",
            summary=f"{band_id} task contract 已更新。",
            reason=reason,
            payload={
                "scope": "band",
                "item_count": len(req.items),
                "chapter_start": int(row.chapter_start or 0),
                "chapter_end": int(row.chapter_end or 0),
            },
            related_object_type="band_experience_plan",
            related_object_id=row.id,
        )
        session.commit()
        return TaskContractResponse(
            project_id=project_id,
            scope="band",
            band_id=band_id,
            items=list(req.items),
            message="band task contract 已保存。",
        )
    finally:
        session.close()


@app.get("/api/projects/{project_id}/constraints", response_model=NarrativeConstraintsResponse)
def list_project_constraints(project_id: str):
    session = _get_session()
    try:
        rows = session.execute(
            select(NarrativeConstraint)
            .where(NarrativeConstraint.project_id == project_id)
            .order_by(NarrativeConstraint.created_at.desc(), NarrativeConstraint.id.desc())
        ).scalars().all()
        return NarrativeConstraintsResponse(items=[_serialize_constraint(row) for row in rows])
    finally:
        session.close()


@app.post("/api/projects/{project_id}/constraints", response_model=NarrativeConstraintInfo)
def create_project_constraint(project_id: str, req: NarrativeConstraintCreateRequest):
    session = _get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        reason = _require_reason(req.reason, action="创建 narrative constraint")
        constraint_type, level, status = _validate_constraint_payload(
            constraint_type=req.constraint_type,
            level=req.level,
            status=req.status,
        )
        row = NarrativeConstraint(
            project_id=project_id,
            arc_id=str(req.arc_id or "").strip(),
            band_id=str(req.band_id or "").strip(),
            constraint_type=constraint_type,
            level=level,
            subject_name=str(req.subject_name or "").strip(),
            description=str(req.description or "").strip(),
            payload_json=json.dumps(req.payload or {}, ensure_ascii=False),
            effective_from_chapter=max(1, int(req.effective_from_chapter or 1)),
            protect_until_chapter=max(0, int(req.protect_until_chapter or 0)),
            status=status,
        )
        session.add(row)
        session.flush()
        _log_decision_event(
            session,
            project_id=project_id,
            band_id=row.band_id,
            event_family="audit_action",
            event_type=DecisionEventType.CONSTRAINT_CREATED,
            actor_type="manual_ui",
            scope="project",
            summary="已新增 narrative constraint。",
            reason=reason,
            related_object_type="narrative_constraint",
            related_object_id=row.id,
        )
        session.commit()
        session.refresh(row)
        return _serialize_constraint(row)
    finally:
        session.close()


@app.patch("/api/projects/{project_id}/constraints/{constraint_id}", response_model=NarrativeConstraintInfo)
def update_project_constraint(project_id: str, constraint_id: str, req: NarrativeConstraintUpdateRequest):
    session = _get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        row = session.get(NarrativeConstraint, constraint_id)
        if row is None or row.project_id != project_id:
            raise HTTPException(404, "narrative constraint 不存在")
        reason = _require_reason(req.reason, action="更新 narrative constraint")
        old_status = str(row.status or "")
        changes: dict[str, Any] = {}
        next_constraint_type, next_level, next_status = _validate_constraint_payload(
            constraint_type=req.constraint_type if req.constraint_type is not None else row.constraint_type,
            level=req.level if req.level is not None else row.level,
            status=req.status if req.status is not None else row.status,
        )

        def _set_if_present(attr: str, value: Any, transform=lambda item: item):
            if value is None:
                return
            next_value = transform(value)
            if getattr(row, attr) != next_value:
                changes[attr] = {"from": getattr(row, attr), "to": next_value}
                setattr(row, attr, next_value)

        _set_if_present("constraint_type", req.constraint_type, lambda _item: next_constraint_type)
        _set_if_present("level", req.level, lambda _item: next_level)
        _set_if_present("subject_name", req.subject_name, lambda item: str(item or "").strip())
        _set_if_present("description", req.description, lambda item: str(item or "").strip())
        if req.payload is not None:
            payload_json = json.dumps(req.payload or {}, ensure_ascii=False)
            if row.payload_json != payload_json:
                changes["payload"] = {"from": _json_load_object(row.payload_json), "to": req.payload or {}}
                row.payload_json = payload_json
        _set_if_present("arc_id", req.arc_id, lambda item: str(item or "").strip())
        _set_if_present("band_id", req.band_id, lambda item: str(item or "").strip())
        _set_if_present("effective_from_chapter", req.effective_from_chapter, lambda item: max(1, int(item or 1)))
        _set_if_present("protect_until_chapter", req.protect_until_chapter, lambda item: max(0, int(item or 0)))
        _set_if_present("status", req.status, lambda _item: next_status)
        event_type = (
            DecisionEventType.CONSTRAINT_ARCHIVED
            if old_status == "active" and str(row.status or "") in {"inactive", "archived"}
            else DecisionEventType.CONSTRAINT_UPDATED
        )
        session.add(row)
        session.flush()
        _log_decision_event(
            session,
            project_id=project_id,
            band_id=row.band_id,
            event_family="audit_action",
            event_type=event_type,
            actor_type="manual_ui",
            scope="project",
            summary="已停用 narrative constraint。" if event_type == DecisionEventType.CONSTRAINT_ARCHIVED else "已更新 narrative constraint。",
            reason=reason,
            payload={"changes": changes},
            related_object_type="narrative_constraint",
            related_object_id=row.id,
        )
        session.commit()
        session.refresh(row)
        return _serialize_constraint(row)
    finally:
        session.close()


@app.get("/api/projects/{project_id}/decision-events", response_model=DecisionEventsResponse)
def list_project_decision_events(
    project_id: str,
    scope: str = "",
    band_id: str = "",
    chapter_number: int = 0,
    task_id: str = "",
    event_family: str = "",
    related_object_type: str = "",
    related_object_id: str = "",
    causal_root_id: str = "",
):
    session = _get_session()
    try:
        rows = _list_decision_event_rows(
            session,
            project_id=project_id,
            scope=scope,
            band_id=band_id,
            chapter_number=chapter_number,
            task_id=task_id,
            event_family=event_family,
            related_object_type=related_object_type,
            related_object_id=related_object_id,
            causal_root_id=causal_root_id,
            limit=200,
            ascending=False,
        )
        return DecisionEventsResponse(items=[_serialize_decision_event(row) for row in rows])
    finally:
        session.close()


@app.get("/api/projects/{project_id}/causal-replay", response_model=CausalReplayResponse)
def get_project_causal_replay(
    project_id: str,
    scope: str = "project",
    arc_id: str = "",
    band_id: str = "",
    chapter_number: int = 0,
    task_id: str = "",
):
    session = _get_session()
    try:
        return _build_causal_replay(
            session,
            project_id=project_id,
            scope=str(scope or "project").strip() or "project",
            arc_id=str(arc_id or "").strip(),
            band_id=band_id,
            chapter_number=chapter_number,
            task_id=task_id,
        )
    finally:
        session.close()


@app.get("/api/projects/{project_id}/governance-insights", response_model=GovernanceInsightsResponse)
def get_project_governance_insights(project_id: str):
    session = _get_session()
    try:
        return _build_governance_insights(session, project_id=project_id)
    finally:
        session.close()


@app.get(
    "/api/projects/{project_id}/provisional/latest",
    response_model=ProvisionalBandDetail,
)
def get_latest_provisional_band(project_id: str):
    session = _get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")

        latest = latest_provisional_band_execution(session, project_id)
        if latest is None:
            raise HTTPException(404, "项目暂无 provisional 预演记录")
        return build_provisional_band_detail(
            session=session,
            project_id=project_id,
            latest=latest,
            display_datetime=_display_datetime,
        )
    finally:
        session.close()


@app.get("/api/projects/{project_id}/chapters", response_model=list[ChapterInfo])
def list_chapters(project_id: str):
    session = _get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")

        plans = session.execute(
            select(ChapterPlan).where(ChapterPlan.project_id == project_id).order_by(ChapterPlan.chapter_number)
        ).scalars().all()
        draft_map = load_latest_drafts_by_plan_id(session, [plan.id for plan in plans])
        review_draft_ids = {
            draft_id
            for draft_id in session.execute(
                select(ChapterReview.draft_id)
                .where(ChapterReview.draft_id.in_([draft.id for draft in draft_map.values()]))
                .distinct()
            ).scalars().all()
        } if draft_map else set()

        result = []
        for p in plans:
            draft = draft_map.get(p.id)
            result.append(ChapterInfo(
                chapter_number=p.chapter_number,
                title=p.title,
                status=p.status,
                char_count=draft.char_count if draft else 0,
                summary=draft.summary if draft else "",
                has_draft=draft is not None,
                has_review=bool(draft and draft.id in review_draft_ids),
            ))
        return result
    finally:
        session.close()


@app.get("/api/projects/{project_id}/chapters/{chapter_number}", response_model=ChapterDetail)
def get_chapter(project_id: str, chapter_number: int):
    session = _get_session()
    try:
        plan = session.execute(
            select(ChapterPlan).where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number == chapter_number,
            )
        ).scalar_one_or_none()


        if plan is None:
            raise HTTPException(404, f"第{chapter_number}章不存在")

        draft = session.execute(
            select(ChapterDraft).where(ChapterDraft.chapter_plan_id == plan.id).order_by(ChapterDraft.version.desc()).limit(1)
        ).scalar_one_or_none()
        if draft is None:
            raise HTTPException(404, f"第{chapter_number}章尚未生成")

        return ChapterDetail(
            chapter_number=chapter_number,
            title=plan.title,
            body=draft.body_text,
            char_count=draft.char_count,
            summary=draft.summary,
            status=plan.status,
            version=draft.version,
        )
    finally:
        session.close()


@app.post(
    "/api/projects/{project_id}/publishers/upload-jobs",
    response_model=PublisherUploadJobResponse,
)
def create_project_chapter_upload_job(
    project_id: str,
    req: ProjectChapterPublishRequest,
):
    session = _get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        plan = session.execute(
            select(ChapterPlan).where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number == req.chapter_number,
            )
        ).scalar_one_or_none()
        if plan is None:
            raise HTTPException(404, f"第{req.chapter_number}章不存在")
        draft = session.execute(
            select(ChapterDraft)
            .where(ChapterDraft.chapter_plan_id == plan.id)
            .order_by(ChapterDraft.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        if draft is None:
            raise HTTPException(404, f"第{req.chapter_number}章尚未生成")
    finally:
        session.close()

    try:
        payload = _publisher_manager.create_upload_job(
            project_id=project_id,
            platform=req.platform,
            book_name=req.book_name,
            chapter_title=plan.title,
            body=draft.body_text,
            upload_url=req.upload_url,
            publish=req.publish,
            create_if_missing=req.create_if_missing,
            book_meta=req.book_meta.model_dump() if req.book_meta else None,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return PublisherUploadJobResponse(**payload)


@app.get("/api/projects/{project_id}/chapters/{chapter_number}/review", response_model=ChapterReviewDetail)
def get_chapter_review(project_id: str, chapter_number: int):
    session = _get_session()
    try:
        plan = session.execute(
            select(ChapterPlan).where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number == chapter_number,
            )
        ).scalar_one_or_none()
        if plan is None:
            raise HTTPException(404, f"第{chapter_number}章不存在")

        draft = session.execute(
            select(ChapterDraft)
            .where(ChapterDraft.chapter_plan_id == plan.id)
            .order_by(ChapterDraft.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        if draft is None:
            raise HTTPException(404, f"第{chapter_number}章尚未生成 draft")

        review = session.execute(
            select(ChapterReview)
            .where(ChapterReview.draft_id == draft.id)
            .order_by(ChapterReview.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if review is None:
            raise HTTPException(404, f"第{chapter_number}章尚未生成 review")

        issues = json.loads(review.issues_json or "[]")
        review_meta = json.loads(review.review_meta_json or "{}") if review.review_meta_json else {}
        if not isinstance(review_meta, dict):
            review_meta = {}
        rewrite_attempts = session.execute(
            select(ChapterRewriteAttempt)
            .where(
                ChapterRewriteAttempt.project_id == project_id,
                ChapterRewriteAttempt.chapter_number == chapter_number,
            )
            .order_by(ChapterRewriteAttempt.attempt_no.desc(), ChapterRewriteAttempt.created_at.desc())
        ).scalars().all()
        decision_refs = _decision_refs_for_chapter_review(
            session,
            project_id=project_id,
            chapter_number=chapter_number,
            review_id=review.id,
        )
        return ChapterReviewDetail(
            project_id=project_id,
            chapter_number=chapter_number,
            title=plan.title,
            status=plan.status,
            draft_id=draft.id,
            version=draft.version,
            body=draft.body_text,
            summary=draft.summary,
            verdict=review.verdict,
            issues=[
                ChapterReviewIssueInfo.model_validate(issue)
                for issue in issues
                if isinstance(issue, dict)
            ],
            artifact_meta_path=draft.llm_raw_response,
            recommended_action=str(review_meta.get("recommended_action") or ""),
            review_summary=str(review_meta.get("review_summary") or ""),
            planned_reward_tags=[
                str(item)
                for item in (review_meta.get("planned_reward_tags") or [])
                if str(item).strip()
            ],
            delivered_reward_tags=[
                str(item)
                for item in (review_meta.get("delivered_reward_tags") or [])
                if str(item).strip()
            ],
            experience_scores={
                str(key): float(value)
                for key, value in (review_meta.get("experience_scores") or {}).items()
            },
            review_notes=[
                str(item)
                for item in (review_meta.get("review_notes") or [])
                if str(item).strip()
            ],
            lint_signals=[
                LintSignalInfo.model_validate(item)
                for item in (review_meta.get("lint_signals") or [])
                if isinstance(item, dict)
            ],
            evidence_refs=[
                str(item)
                for item in (review_meta.get("evidence_refs") or [])
                if str(item).strip()
            ],
            confirmed_signal_refs=[
                str(item)
                for item in (review_meta.get("confirmed_signal_refs") or [])
                if str(item).strip()
            ],
            reviewer_mode=str(review_meta.get("reviewer_mode") or ""),
            proposed_design_patch=(
                dict((review_meta.get("repair_instruction") or {}).get("design_patch") or {})
                if isinstance(review_meta.get("repair_instruction"), dict)
                else {}
            ),
            rewrite_attempt_count=len(rewrite_attempts),
            latest_repair_scope=(
                str(rewrite_attempts[0].repair_scope or "") if rewrite_attempts else ""
            ),
            forced_accept_applied=bool(review_meta.get("forced_accept_applied")),
            decision_refs=decision_refs,
        )
    finally:
        session.close()


@app.get("/api/tropes/templates", response_model=list[TropeTemplateInfo])
def get_trope_templates(
    category: str = "",
    q: str = "",
    limit: int = 0,
) -> list[TropeTemplateInfo]:
    normalized_category = str(category or "").strip()
    normalized_query = str(q or "").strip().lower()
    templates = list(TROPE_TEMPLATE_LIBRARY)
    if normalized_category:
        templates = [
            template
            for template in templates
            if str(template.category) == normalized_category
        ]
    if normalized_query:
        templates = [
            template
            for template in templates
            if normalized_query
            in " ".join(
                [
                    template.template_id,
                    template.display_name,
                    template.setup_requirement,
                    template.payoff_shape,
                    " ".join(template.risk_flags),
                    " ".join(template.recommended_hook_types),
                ]
            ).lower()
        ]
    if limit > 0:
        templates = templates[:limit]
    return [
        TropeTemplateInfo.model_validate(template.model_dump(mode="json"))
        for template in templates
    ]


@app.get("/api/tropes/templates/summary", response_model=TropeRegistrySummaryResponse)
def get_trope_template_summary() -> TropeRegistrySummaryResponse:
    return TropeRegistrySummaryResponse.model_validate(
        trope_registry_summary().model_dump(mode="json")
    )


@app.post("/api/tropes/templates/validate", response_model=TropeTemplateValidationResponse)
def validate_trope_templates(req: TropeTemplateValidationRequest) -> TropeTemplateValidationResponse:
    templates, errors = validate_trope_template_payload(
        req.templates,
        require_full=bool(req.require_full),
    )
    category_counts: dict[str, int] = {}
    for template in templates:
        category_counts[str(template.category)] = category_counts.get(str(template.category), 0) + 1
    return TropeTemplateValidationResponse(
        ok=not errors,
        total_count=len(templates),
        category_counts=category_counts,
        errors=errors,
    )


@app.post(
    "/api/projects/{project_id}/bands/{band_id}/experience",
    response_model=BandExperienceOverrideResponse,
)
def override_band_experience(
    project_id: str,
    band_id: str,
    req: BandExperienceOverrideRequest,
) -> BandExperienceOverrideResponse:
    session = _get_session()
    try:
        repo = StateRepository(session)
        active_arc = repo.get_active_arc_plan(project_id)
        if active_arc is None:
            raise HTTPException(404, "当前项目没有 active arc")
        band_row = session.execute(
            select(BandExperiencePlan)
            .where(
                BandExperiencePlan.project_id == project_id,
                BandExperiencePlan.arc_id == active_arc.id,
                BandExperiencePlan.band_id == band_id,
            )
            .order_by(BandExperiencePlan.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if band_row is None:
            raise HTTPException(404, f"band 不存在: {band_id}")

        current_payload = json.loads(band_row.schedule_json or "{}") if band_row.schedule_json else {}
        if not isinstance(current_payload, dict):
            current_payload = {}
        if req.scheduled_rewards:
            current_payload["scheduled_rewards"] = req.scheduled_rewards
        if req.curiosity_beats:
            current_payload["curiosity_beats"] = req.curiosity_beats
        if req.immersion_anchor_scene_goal.strip():
            current_payload["immersion_anchor_scene_goal"] = req.immersion_anchor_scene_goal.strip()
        current_payload.setdefault("band_id", band_row.band_id)
        current_payload.setdefault("chapter_start", band_row.chapter_start)
        current_payload.setdefault("chapter_end", band_row.chapter_end)
        current_payload.setdefault("stall_guard_max_gap", band_row.stall_guard_max_gap)

        schedule = BandDelightSchedule.model_validate(current_payload)
        band_row.schedule_json = json.dumps(schedule.model_dump(mode="json"), ensure_ascii=False)
        band_row.stall_guard_max_gap = schedule.stall_guard_max_gap
        session.add(band_row)

        if _orchestrator is not None:
            arc_structure = repo.get_latest_arc_structure_draft(project_id)
            structure_data = _orchestrator._structure_data_from_row(arc_structure)
            for chapter_number in range(schedule.chapter_start, schedule.chapter_end + 1):
                chapter_plan = repo.get_chapter_plan(project_id, chapter_number)
                if chapter_plan is None:
                    continue
                experience_plan = _orchestrator.arc_envelope_manager._derive_chapter_experience_plan(
                    chapter_number=chapter_number,
                    structure=structure_data,
                    schedule=schedule,
                    chapter_plan=chapter_plan,
                )
                chapter_plan.experience_plan_json = json.dumps(
                    experience_plan.model_dump(mode="json"),
                    ensure_ascii=False,
                )
                session.add(chapter_plan)
        session.commit()
        return BandExperienceOverrideResponse(
            ok=True,
            project_id=project_id,
            band_id=band_id,
            chapter_start=schedule.chapter_start,
            chapter_end=schedule.chapter_end,
            message="band experience overlay 已更新并重生成 chapter experience plans",
        )
    finally:
        session.close()


@app.post(
    "/api/projects/{project_id}/chapters/{chapter_number}/review/approve",
    response_model=ChapterReviewApproveResponse,
)
def approve_chapter_review(
    project_id: str,
    chapter_number: int,
    req: ChapterReviewApproveRequest,
):
    if _config is None or _orchestrator is None:
        raise HTTPException(500, "服务尚未完成初始化")

    runtime_config = build_saved_runtime_config(
        base_config=_config,
        runtime_settings=_runtime_settings,
    )
    reason = _require_reason(req.reason, action="接受 review")
    try:
        accept_review_parameters = inspect.signature(_orchestrator.accept_review).parameters
        if "reason" in accept_review_parameters:
            result = _orchestrator.accept_review(project_id, chapter_number, reason=reason)
        else:
            result = _orchestrator.accept_review(project_id, chapter_number)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc

    task_id = ""
    message = result["message"]
    if req.continue_generation:
        session = _get_session()
        try:
            project = session.get(Project, project_id)
            if project is None:
                raise HTTPException(404, "项目不存在")
            governance = _resolve_project_governance(project, base_config=_config)
            runtime_config = copy_config(
                runtime_config,
                operation_mode=governance.default_operation_mode,
                review_interval_chapters=governance.review_interval_chapters,
                progression_mode=governance.progression_mode,
                auto_band_checkpoint=governance.auto_band_checkpoint,
                band_warn_action=governance.band_warn_action,
                manual_checkpoints_enabled=governance.manual_checkpoints_enabled,
                future_constraints_enabled=governance.future_constraints_enabled,
            )
            if _project_has_active_generation_task(project_id, session=session):
                raise HTTPException(409, _generation_task_conflict_message(project_id))
            project_detail = build_project_detail(
                session=session,
                project=project,
                display_datetime=_display_datetime,
                review_interval_chapters=governance.review_interval_chapters,
            )
            if project_detail.blocking_reason.code:
                _log_decision_event(
                    session,
                    project_id=project_id,
                    event_family="evaluation_verdict",
                    event_type=DecisionEventType.HARD_GATE_HIT,
                    actor_type="api",
                    scope="project",
                    summary=project_detail.blocking_reason.message or project_detail.blocking_reason.code,
                    payload={"blocking_reason": project_detail.blocking_reason.code},
                    band_id=project_detail.blocking_reason.band_id,
                    chapter_number=int(project_detail.blocking_reason.chapter_number or 0),
                    related_object_type="project",
                    related_object_id=project_id,
                )
                session.commit()
                raise HTTPException(409, project_detail.blocking_reason.message)
            total_chapters = session.execute(
                select(func.count(ChapterPlan.id)).where(ChapterPlan.project_id == project_id)
            ).scalar_one()
        finally:
            session.close()
        try:
            task_id = _create_continue_generation_task(
                project_id=project_id,
                runtime_config=runtime_config,
                requested_chapters=int(total_chapters or 0),
                message=f"已接受第{chapter_number}章，准备继续后续章节。",
            )
        except ActiveGenerationTaskError as exc:
            raise HTTPException(409, str(exc)) from exc
        _update_task(
            task_id,
            frozen_artifacts=[result["frozen_artifact"]] if result["frozen_artifact"] else [],
        )
        message = f"{message} 已启动后续章节继续执行。"

    return ChapterReviewApproveResponse(
        ok=True,
        project_id=project_id,
        chapter_number=chapter_number,
        status="accepted",
        message=message,
        task_id=task_id,
        frozen_artifact=result.get("frozen_artifact") or "",
    )
