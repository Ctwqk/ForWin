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
from forwin.api_artifacts import build_artifact_store
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
from forwin.models.base import Base, get_engine, get_session_factory, init_db
from forwin.models.genesis import BookGenesisRevision
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
from forwin.skills import build_skill_runtime_components
from forwin.state.query_helpers import load_latest_drafts_by_plan_id
from forwin.state.updater import StateUpdater
from forwin.writer.llm_client import LLMClient
from forwin.llm.factory import maybe_wrap_with_codex_router

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
_task_center_service: TaskCenterService | None = None
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
    "running_scenario_rehearsal",
    "scenario_rehearsal_patch_required",
    "scenario_rehearsal_blocked",
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


def _resolve_runtime_profile(requested_profile_id: str = "") -> dict[str, str]:
    stored = _runtime_settings.get() if _runtime_settings else {}
    profiles = [
        item for item in stored.get("profiles", [])
        if isinstance(item, dict)
    ]
    target_id = str(requested_profile_id or "").strip() or str(stored.get("default_profile_id", "")).strip()
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
            "name": "",
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


def _saved_runtime_config_or_default(model_profile_id: str = "") -> Config:
    if not _config:
        return Config(db_path=":memory:", minimax_api_key="")
    if model_profile_id:
        return build_runtime_config(
            GenerateRequest(
                premise="Genesis model selection",
                model_profile_id=model_profile_id,
            ),
            base_config=_config,
            runtime_settings=_runtime_settings,
        )
    return build_saved_runtime_config(
        base_config=_config,
        runtime_settings=_runtime_settings,
    )


def _build_genesis_service(
    runtime_config: Config | None = None,
    *,
    model_profile_id: str = "",
) -> BookGenesisService:
    resolved_profile = _resolve_runtime_profile(model_profile_id)
    resolved = runtime_config or _saved_runtime_config_or_default(model_profile_id)
    llm_client = LLMClient(
        api_key=str(resolved.minimax_api_key or ""),
        base_url=str(resolved.minimax_base_url or ""),
        model=str(resolved.minimax_model or ""),
        fallback_profiles=getattr(resolved, "llm_fallback_profiles", None),
    )
    setattr(llm_client, "profile_id", resolved_profile.get("id", ""))
    setattr(llm_client, "profile_name", resolved_profile.get("name", ""))
    llm_client = maybe_wrap_with_codex_router(llm_client, resolved)
    _registry, router, prompt_layer_builder = build_skill_runtime_components(
        root=resolved.skill_registry_path,
        enabled=resolved.skill_runtime_enabled,
        strictness=resolved.skill_strictness,
        enabled_skill_groups=resolved.enabled_skill_groups,
        disabled_skill_ids=resolved.disabled_skill_ids,
    )
    return BookGenesisService(
        llm_client=llm_client,
        skill_router=router,
        skill_prompt_layer_builder=prompt_layer_builder,
        artifact_store=build_artifact_store(resolved),
    )


def _close_genesis_service(service: BookGenesisService | None) -> None:
    client = getattr(service, "llm_client", None)
    close = getattr(client, "client", None)
    if close is None:
        return
    try:
        close.close()
    except Exception:  # noqa: BLE001
        logger.debug("BookGenesisService client close failed", exc_info=True)


def _active_genesis_revision(session: Session, project: Project) -> BookGenesisRevision | None:
    revision_id = str(getattr(project, "active_genesis_revision_id", "") or "").strip()
    if not revision_id:
        return None
    return session.get(BookGenesisRevision, revision_id)


def _require_genesis_project(project: Project) -> None:
    if str(getattr(project, "creation_status", "") or "").strip() == "legacy":
        raise HTTPException(400, "旧项目未启用 Genesis 工作流。")


def _genesis_patch_payload(req: BookGenesisPatchRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in (
        "book_brief",
        "world",
        "book_arc_blueprint",
        "subworld_policy",
        "execution_bootstrap",
        "stage_states",
    ):
        value = getattr(req, key)
        if value is None:
            continue
        payload[key] = value
    return payload


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


def _get_task_center_service() -> TaskCenterService:
    global _task_center_service
    if _task_center_service is not None:
        return _task_center_service

    def _iter_cached_generation_tasks() -> list[tuple[str, dict[str, Any]]]:
        with _tasks_lock:
            return [(task_id, dict(task)) for task_id, task in _tasks.items()]

    _task_center_service = TaskCenterService(
        get_session=_get_session,
        has_db_session=lambda: _SessionFactory is not None,
        prune_tasks=_prune_tasks,
        utcnow=_utcnow,
        display_datetime=_display_datetime,
        coerce_task_datetime=_coerce_task_datetime,
        new_stage_history_entry=_new_stage_history_entry,
        cached_generation_task=_cached_generation_task,
        iter_cached_generation_tasks=_iter_cached_generation_tasks,
        prefer_cached_generation_task=_prefer_cached_generation_task,
        generation_task_from_row=_generation_task_from_row,
        config_provider=lambda: _config,
        terminal_statuses=_GENERATION_TERMINAL_STATUSES,
        terminal_stage_by_status=_GENERATION_TERMINAL_STAGE_BY_STATUS,
    )
    return _task_center_service


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


def _prune_tasks(*, include_db: bool = True) -> None:
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

    if include_db:
        _prune_generation_tasks_db(now)


def _load_generation_task(task_id: str, *, include_deleted: bool = False) -> dict[str, Any] | None:
    try:
        return _get_task_center_service().load_generation_task(
            task_id,
            include_deleted=include_deleted,
        )
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
    _prune_tasks(include_db=False)
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
    return api_governance_support.validate_constraint_payload(
        constraint_type=constraint_type,
        level=level,
        status=status,
    )


def _persist_project_automation(
    session,
    project: Project,
    automation: ProjectAutomationSettings,
) -> ProjectAutomationSettings:
    return api_governance_support.persist_project_automation(session, project, automation)


def _governance_request_payload(req: object) -> dict[str, object]:
    return api_governance_support.governance_request_payload(req)


def _resolve_project_governance(
    project: Project | None,
    *,
    overrides: dict[str, object] | None = None,
    base_config: Config | None = None,
) -> object:
    return api_governance_support.resolve_project_governance(
        project,
        overrides=overrides,
        base_config=base_config,
    )


def _persist_project_governance(
    session,
    project: Project,
    governance,
) -> object:
    return api_governance_support.persist_project_governance(
        session,
        project,
        governance,
        base_config=_config,
    )


def _log_decision_event(session, **kwargs):
    return api_governance_support.log_decision_event(session, **kwargs)


def _latest_band_checkpoint_row(session, *, project_id: str, band_id: str = ""):
    return api_governance_support.latest_band_checkpoint_row(
        session,
        project_id=project_id,
        band_id=band_id,
    )


def _serialize_band_checkpoint(row: BandCheckpoint, *, session=None) -> BandCheckpointDetail:
    return api_governance_support.serialize_band_checkpoint(row, session=session)


def _serialize_constraint(row: NarrativeConstraint) -> NarrativeConstraintInfo:
    return api_governance_support.serialize_constraint(row)


def _serialize_decision_event(row: DecisionEvent) -> DecisionEventInfo:
    return api_governance_support.serialize_decision_event(row)


def _decision_event_stmt(**kwargs):
    return api_governance_support.decision_event_stmt(**kwargs)


def _list_decision_event_rows(session, **kwargs) -> list[DecisionEvent]:
    return api_governance_support.list_decision_event_rows(session, **kwargs)


def _latest_related_decision_event(session, **kwargs) -> DecisionEvent | None:
    return api_governance_support.latest_related_decision_event(session, **kwargs)


def _decision_refs_for_checkpoint(session, row: BandCheckpoint) -> list[DecisionEventInfo]:
    return api_governance_support.decision_refs_for_checkpoint(session, row)


def _decision_refs_for_chapter_review(
    session,
    *,
    project_id: str,
    chapter_number: int,
    review_id: str,
) -> list[DecisionEventInfo]:
    return api_governance_support.decision_refs_for_chapter_review(
        session,
        project_id=project_id,
        chapter_number=chapter_number,
        review_id=review_id,
    )


def _counter_rows(counter: Counter[str], *, limit: int = 5) -> list[dict[str, Any]]:
    return api_governance_support.counter_rows(counter, limit=limit)


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
    return api_governance_support.build_causal_replay(
        session,
        project_id=project_id,
        scope=scope,
        arc_id=arc_id,
        band_id=band_id,
        chapter_number=chapter_number,
        task_id=task_id,
    )


def _build_governance_insights(session, *, project_id: str) -> GovernanceInsightsResponse:
    return api_governance_support.build_governance_insights(session, project_id=project_id)


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
        active_task_ids = session.execute(
            select(GenerationTask.id).where(
                GenerationTask.deleted_at.is_(None),
                GenerationTask.project_id == normalized_project_id,
                GenerationTask.status.notin_(tuple(_GENERATION_TERMINAL_STATUSES)),
            )
        ).scalars().all()
        for task_id in active_task_ids:
            cached = _cached_generation_task(str(task_id))
            if cached is not None:
                if cached.get("deleted"):
                    continue
                if _task_is_terminal(str(cached.get("status", "")).strip()):
                    continue
            return True
        with _tasks_lock:
            cached_tasks = list(_tasks.values())
        for task in cached_tasks:
            if task.get("deleted"):
                continue
            if str(task.get("task_kind", "generation")) != "generation":
                continue
            if str(task.get("project_id", "") or "").strip() != normalized_project_id:
                continue
            if _task_is_terminal(str(task.get("status", "")).strip()):
                continue
            return True
        return False
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
    return api_automation.automation_daily_start_minutes(automation)


def _load_automation_scheduler_metrics(
    session,
    project_ids: list[str],
) -> tuple[dict[str, int], dict[str, int], dict[str, list[int]], set[str]]:
    return api_automation.load_automation_scheduler_metrics(
        session,
        project_ids,
        terminal_statuses=_GENERATION_TERMINAL_STATUSES,
    )


def _run_automation_scheduler_pass() -> None:
    return api_automation.run_automation_scheduler_pass(
        session_factory=_SessionFactory,
        config=_config,
        saved_runtime_config_or_503=_saved_runtime_config_or_503,
        utcnow=_utcnow,
        display_tz=_DISPLAY_TZ,
        display_datetime=_display_datetime,
        get_session=_get_session,
        persist_project_automation=_persist_project_automation,
        create_generation_task=_create_generation_task,
        create_continue_generation_task=_create_continue_generation_task,
        active_generation_task_error_cls=ActiveGenerationTaskError,
        terminal_statuses=_GENERATION_TERMINAL_STATUSES,
    )


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
    _prune_tasks(include_db=False)
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
    global _engine, _SessionFactory, _orchestrator, _publisher_manager, _runtime_settings, _task_center_service

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
    _task_center_service = None
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
            default_skill_runtime_enabled=_config.skill_runtime_enabled,
            default_skill_registry_path=_config.skill_registry_path,
            default_skill_strictness=_config.skill_strictness,
            default_enabled_skill_groups=_config.enabled_skill_groups,
            default_disabled_skill_ids=_config.disabled_skill_ids,
            env_llm_profiles=_config.llm_env_profiles,
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

_observability_handlers = api_observability_routes.build_handlers(
    get_config=lambda: _config,
    get_session=_get_session,
    list_decision_event_rows=lambda session, **kwargs: _list_decision_event_rows(session, **kwargs),
    serialize_decision_event=lambda row: _serialize_decision_event(row),
    display_datetime=_display_datetime,
    json_load_object=lambda raw: _json_load_object(raw),
    json_load_list=lambda raw: _json_load_list(raw),
)
get_task_timeline = _observability_handlers["get_task_timeline"]
get_chapter_observability_ledger = _observability_handlers["get_chapter_observability_ledger"]
get_prompt_trace_detail = _observability_handlers["get_prompt_trace_detail"]
read_artifact_preview = _observability_handlers["read_artifact_preview"]

globals().update(
    api_route_registry.register_api_routes(
        app,
        deps=api_route_registry.ApiRouteDeps(
            get_config=lambda: _config,
            get_runtime_settings=lambda: _runtime_settings,
            get_publisher_manager=lambda: _publisher_manager,
            get_orchestrator=lambda: _orchestrator,
            get_session=_get_session,
            render_home_page=render_home_page,
            render_publishers_page=render_publishers_page,
            build_home_page_settings=build_home_page_settings,
            build_runtime_config=build_runtime_config,
            copy_config=copy_config,
            create_generation_task=lambda **kwargs: _create_generation_task(**kwargs),
            serialize_task=lambda task_id, task: _serialize_task(task_id, task),
            get_generation_task_or_404=lambda task_id: _get_generation_task_or_404(task_id),
            project_has_active_generation_task=lambda project_id, *, session=None: _project_has_active_generation_task(project_id, session=session),
            generation_task_conflict_message=lambda project_id: _generation_task_conflict_message(project_id),
            resolve_project_governance=lambda project, *, overrides=None, base_config=None: _resolve_project_governance(project, overrides=overrides, base_config=base_config),
            governance_request_payload=lambda req: _governance_request_payload(req),
            serialize_llm_settings=lambda payload, *, message: _serialize_llm_settings(payload, message=message),
            active_generation_task_error_cls=ActiveGenerationTaskError,
            list_generation_tasks=lambda limit: _list_generation_tasks(limit),
            serialize_generation_task_center_item=lambda task_id, task: _serialize_generation_task_center_item(task_id, task),
            serialize_upload_task_center_item=lambda payload: _serialize_upload_task_center_item(payload),
            list_project_backed_task_items=lambda limit: _list_project_backed_task_items(limit),
            parse_project_task_id=lambda task_id: _parse_project_task_id(task_id),
            get_project_backed_task_item_or_404=lambda task_id: _get_project_backed_task_item_or_404(task_id),
            task_is_terminal=lambda status: _task_is_terminal(status),
            task_is_terminable=lambda task: _task_is_terminable(task),
            task_is_pausable=lambda task: _task_is_pausable(task),
            task_is_deletable=lambda task: _task_is_deletable(task),
            latest_related_decision_event=lambda session, **kwargs: _latest_related_decision_event(session, **kwargs),
            log_decision_event=lambda session, **kwargs: _log_decision_event(session, **kwargs),
            update_task=lambda task_id, **changes: _update_task(task_id, **changes),
            display_datetime=_display_datetime,
            build_genesis_service=lambda *args, **kwargs: _build_genesis_service(*args, **kwargs),
            close_genesis_service=lambda service=None: _close_genesis_service(service),
            require_genesis_project=lambda project: _require_genesis_project(project),
            active_genesis_revision=lambda session, project: _active_genesis_revision(session, project),
            genesis_patch_payload=lambda req: _genesis_patch_payload(req),
            delete_project_impl=lambda session, project_id: _delete_project(session, project_id),
            project_delete_blockers=lambda project_id, *, session: _project_delete_blockers(project_id, session=session),
            project_delete_conflict_message=lambda blockers: _project_delete_conflict_message(blockers),
            saved_runtime_config_or_default=lambda model_profile_id='': _saved_runtime_config_or_default(model_profile_id),
            create_continue_generation_task=lambda **kwargs: _create_continue_generation_task(**kwargs),
            persist_project_automation=lambda session, project, automation: _persist_project_automation(session, project, automation),
            require_reason=lambda reason, *, action: _require_reason(reason, action=action),
            decision_refs_for_chapter_review=lambda session, *, project_id, chapter_number, review_id: _decision_refs_for_chapter_review(session, project_id=project_id, chapter_number=chapter_number, review_id=review_id),
            validate_constraint_payload=lambda **kwargs: _validate_constraint_payload(**kwargs),
            serialize_band_checkpoint=lambda row, *, session=None: _serialize_band_checkpoint(row, session=session),
            serialize_constraint=lambda row: _serialize_constraint(row),
            list_decision_event_rows=lambda session, **kwargs: _list_decision_event_rows(session, **kwargs),
            serialize_decision_event=lambda row: _serialize_decision_event(row),
            build_causal_replay=lambda session, **kwargs: _build_causal_replay(session, **kwargs),
            build_governance_insights=lambda session, *, project_id: _build_governance_insights(session, project_id=project_id),
            latest_band_checkpoint_row=lambda session, *, project_id, band_id='': _latest_band_checkpoint_row(session, project_id=project_id, band_id=band_id),
            persist_project_governance=lambda session, project, governance: _persist_project_governance(session, project, governance),
            json_load_object=lambda raw: _json_load_object(raw),
            get_task_timeline=get_task_timeline,
            get_chapter_observability_ledger=get_chapter_observability_ledger,
            get_prompt_trace_detail=get_prompt_trace_detail,
            read_artifact_preview=read_artifact_preview,
        ),
    )
)
