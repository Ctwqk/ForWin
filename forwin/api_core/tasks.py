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

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from forwin.api_pages import render_home_page, render_publishers_page
from forwin import (
    api_automation,
    api_governance_ops,
    api_governance_routes,
    api_governance_support,
    api_observability_routes,
    api_project_ops,
    api_project_routes,
    api_publisher_ops,
    api_publisher_routes,
    api_route_registry,
    api_system_routes,
    api_task_routes,
)
from forwin.api_task_center_service import TaskCenterService
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
from forwin.api_task_history import augment_task_with_rehearsal_history
from forwin.api_auth import basic_auth_enabled, make_basic_auth_middleware
from forwin.api_schemas import (
    BandCheckpointApproveRequest,
    BandCheckpointDetail,
    BandExperienceOverrideRequest,
    BandExperienceOverrideResponse,
    ActiveGenerationTaskCheckResponse,
    BookGenesisDetail,
    BookGenesisPatchRequest,
    BookGenesisRefineRequest,
    BookGenesisStageRunRequest,
    CausalReplayResponse,
    CandidateDraftDetail,
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
    StartWritingResponse,
)
from forwin.book_genesis import BookGenesisService, GENESIS_STAGE_ORDER, StaleGenesisRevisionError
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
from forwin.models.base import Base, get_session_factory
from forwin.models.genesis import BookGenesisRevision
from forwin.models.project import Project, ChapterPlan, ArcPlanVersion
from forwin.models.entity import Entity
from forwin.models.event import CanonEvent, EventEntityLink
from forwin.models.governance import BandCheckpoint, DecisionEvent, NarrativeConstraint
from forwin.models.publisher import PublisherCommentSyncJob, PublisherConnectionState, PublisherExtensionClient, PublisherRawComment, PublisherUploadJob
from forwin.models.thread import PlotThread
from forwin.models.task import GenerationTask
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.phase import (
    BandExperiencePlan,
    ChapterRewriteAttempt,
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
from forwin.publisher_runtime.codex_intervention import build_codex_intervention_handler
from forwin.publishers import PublisherManager
from forwin.runtime.container import RuntimeContainer
from forwin.runtime_settings import RuntimeSettingsStore
from forwin.state.query_helpers import load_latest_drafts_by_plan_id
from forwin.state.updater import StateUpdater

logger = logging.getLogger(__name__)

# Backwards-compatible aliases for tests and local integrations while api.py is being split.
_build_runtime_config = build_runtime_config
_build_saved_runtime_config = build_saved_runtime_config
_run_generation_with_config = run_generation_with_config
_run_continue_project_with_config = run_continue_project_with_config

from forwin.api_core import state as api_state
from forwin.api_core.runtime import *

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
    with api_state._tasks_lock:
        if task is None or task.get("deleted"):
            api_state._tasks.pop(task_id, None)
        else:
            api_state._tasks[task_id] = dict(task)


def _cached_generation_task(task_id: str) -> dict[str, Any] | None:
    with api_state._tasks_lock:
        task = api_state._tasks.get(task_id)
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


def _get_task_center_service() -> TaskCenterService:
    if api_state._task_center_service is not None:
        return api_state._task_center_service

    def _iter_cached_generation_tasks() -> list[tuple[str, dict[str, Any]]]:
        with api_state._tasks_lock:
            return [(task_id, dict(task)) for task_id, task in api_state._tasks.items()]

    api_state._task_center_service = TaskCenterService(
        get_session=_get_session,
        has_db_session=lambda: api_state._SessionFactory is not None,
        prune_tasks=_prune_tasks,
        utcnow=_utcnow,
        display_datetime=_display_datetime,
        coerce_task_datetime=_coerce_task_datetime,
        new_stage_history_entry=_new_stage_history_entry,
        cached_generation_task=_cached_generation_task,
        iter_cached_generation_tasks=_iter_cached_generation_tasks,
        prefer_cached_generation_task=_prefer_cached_generation_task,
        generation_task_from_row=_generation_task_from_row,
        config_provider=lambda: api_state._config,
        terminal_statuses=api_state._GENERATION_TERMINAL_STATUSES,
        terminal_stage_by_status=api_state._GENERATION_TERMINAL_STAGE_BY_STATUS,
    )
    return api_state._task_center_service


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
    return _get_task_center_service().task_has_stage(task, stage)


def _normalize_loaded_generation_task(task: dict[str, Any]) -> dict[str, Any]:
    return _get_task_center_service().normalize_loaded_generation_task(task)


def _apply_task_visibility_rules(
    task: dict[str, Any] | None,
    *,
    include_deleted: bool,
) -> dict[str, Any] | None:
    return _get_task_center_service().apply_task_visibility_rules(
        task,
        include_deleted=include_deleted,
    )


def _augment_task_with_provisional_history(session, task: dict[str, Any]) -> dict[str, Any]:
    return augment_task_with_rehearsal_history(
        session,
        task,
        display_datetime=_display_datetime,
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
        getattr(orig, "sqlstate", "")
        or getattr(orig, "pgcode", "")
        or ""
    ).strip()
    if sqlstate in {"40001", "40P01", "55P03", "57014", "08000", "08003", "08006", "08001"}:
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


def _mark_task_persistence_degraded(task_id: str, task: dict[str, Any], exc: Exception) -> None:
    task["persistence_degraded"] = True
    task["persistence_error"] = str(exc)
    task["updated_at"] = _utcnow()
    _sync_task_cache(task_id, task)


def _clear_task_persistence_degraded(task_id: str, task: dict[str, Any]) -> None:
    if task.get("persistence_degraded") or task.get("persistence_error"):
        task["persistence_degraded"] = False
        task["persistence_error"] = None
        _sync_task_cache(task_id, task)


def _prune_generation_tasks_db(now: datetime | None = None) -> None:
    if api_state._SessionFactory is None:
        return

    current = now or _utcnow()
    if api_state._last_generation_task_db_prune_at is not None:
        elapsed = (current - api_state._last_generation_task_db_prune_at).total_seconds()
        if elapsed < api_state._TASK_DB_PRUNE_INTERVAL_SECONDS:
            return

    api_state._last_generation_task_db_prune_at = current
    cutoff = current - timedelta(seconds=api_state._TASK_RETENTION_SECONDS)

    def _operation() -> None:
        with _get_session() as session:
            session.execute(
                delete(GenerationTask).where(
                    or_(
                        GenerationTask.deleted_at.is_not(None),
                        (
                            GenerationTask.status.in_(tuple(api_state._GENERATION_TERMINAL_STATUSES))
                            & (GenerationTask.updated_at < cutoff)
                        ),
                    )
                )
            )
            total_rows = session.execute(
                select(func.count(GenerationTask.id)).where(GenerationTask.deleted_at.is_(None))
            ).scalar_one()
            overflow = max(0, int(total_rows or 0) - api_state._MAX_TASKS)
            if overflow:
                overflow_ids = session.execute(
                    select(GenerationTask.id)
                    .where(
                        GenerationTask.deleted_at.is_(None),
                        GenerationTask.status.in_(tuple(api_state._GENERATION_TERMINAL_STATUSES)),
                    )
                    .order_by(GenerationTask.updated_at.asc())
                    .limit(overflow)
                ).scalars().all()
                if overflow_ids:
                    session.execute(delete(GenerationTask).where(GenerationTask.id.in_(overflow_ids)))
            session.commit()

    _run_generation_task_db_write(
        _operation,
        context="generation_task_prune",
        attempts=2,
        delay=0.15,
        raise_on_failure=False,
    )


def _prune_tasks(*, include_db: bool = True) -> None:
    now = _utcnow()
    with api_state._tasks_lock:
        stale_ids = [
            task_id
            for task_id, task in api_state._tasks.items()
            if task.get("deleted")
            or (
                task.get("status") in api_state._GENERATION_TERMINAL_STATUSES
                and (now - task.get("updated_at", now)).total_seconds() > api_state._TASK_RETENTION_SECONDS
            )
        ]
        for task_id in stale_ids:
            api_state._tasks.pop(task_id, None)

    if include_db:
        _prune_generation_tasks_db(now)


def _load_generation_task(task_id: str, *, include_deleted: bool = False) -> dict[str, Any] | None:
    try:
        return _get_task_center_service().load_generation_task(
            task_id,
            include_deleted=include_deleted,
        )
    except OperationalError as exc:
        if not _is_retryable_generation_task_db_error(exc):
            raise
        logger.warning("Generation task read fell back to cache due to DB retryable error for %s", task_id)
        return _apply_task_visibility_rules(
            _cached_generation_task(task_id),
            include_deleted=include_deleted,
        )


def _persist_generation_task(task_id: str, task: dict[str, Any]) -> None:
    _sync_task_cache(task_id, task)
    if api_state._SessionFactory is None:
        return

    def _operation() -> None:
        with _get_session() as session:
            row = session.get(GenerationTask, task_id)
            if row is None:
                row = GenerationTask(id=task_id)
            _apply_generation_task_to_row(row, task)
            session.add(row)
            session.commit()

    try:
        _run_generation_task_db_write(_operation, context=f"persist_generation_task:{task_id}")
    except IntegrityError as exc:
        _sync_task_cache(task_id, None)
        project_id = str(task.get("project_id", "") or "").strip()
        if "ux_generation_tasks_one_active_per_project" in str(exc):
            raise ActiveGenerationTaskError(
                _generation_task_conflict_message(project_id)
                if project_id
                else "已有运行中的生成任务，请先终止或等待当前任务完成。"
            ) from exc
        raise
    except GenerationTaskPersistenceError as exc:
        if task.get("status") in api_state._GENERATION_TERMINAL_STATUSES:
            raise
        _mark_task_persistence_degraded(task_id, task, exc)
    else:
        _clear_task_persistence_degraded(task_id, task)


def _recover_interrupted_generation_tasks() -> list[str]:
    if api_state._SessionFactory is None:
        return []

    now = _utcnow()
    recovered_ids: list[str] = []
    with _get_session() as session:
        rows = session.execute(
            select(GenerationTask).where(
                GenerationTask.deleted_at.is_(None),
                GenerationTask.status.notin_(tuple(api_state._GENERATION_TERMINAL_STATUSES)),
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
    return status in api_state._GENERATION_TERMINAL_STATUSES


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
        "persistence_degraded": False,
        "persistence_error": None,
        "created_at": now,
        "updated_at": now,
    }


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
    return _get_task_center_service().project_task_id(project_id)


def _parse_project_task_id(task_id: str) -> str | None:
    return _get_task_center_service().parse_project_task_id(task_id)


def _load_project_task_center_plans(session, project_ids: list[str]) -> dict[str, list[ChapterPlan]]:
    return _get_task_center_service()._load_project_task_center_plans(session, project_ids)


def _build_project_task_center_item(
    project: Project,
    plans: list[ChapterPlan],
) -> TaskCenterItemResponse:
    return _get_task_center_service()._build_project_task_center_item(
        project,
        plans,
        latest_band_checkpoint=None,
        decision_events=[],
    )


def _list_project_backed_task_items(limit: int) -> list[TaskCenterItemResponse]:
    return _get_task_center_service().list_project_backed_task_items(limit)


def _get_project_backed_task_item_or_404(task_id: str) -> TaskCenterItemResponse:
    return _get_task_center_service().get_project_backed_task_item_or_404(task_id)


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



__all__ = [name for name in globals() if not name.startswith("__")]
