"""ForWin Web API – FastAPI interface for the novel generation system."""
from __future__ import annotations

import logging
import os
import threading
import uuid
import json
import io
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import delete, select, func

from forwin.api_pages import render_home_page, render_publishers_page
from forwin.api_project_payloads import (
    build_project_detail,
    build_project_summaries,
    build_provisional_band_detail,
    latest_provisional_band_execution,
)
from forwin.api_runtime import (
    build_home_page_settings,
    build_runtime_config,
    build_saved_runtime_config,
    run_continue_project_with_config,
    run_generation_with_config,
)
from forwin.api_schemas import (
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
    LLMDefaultProfileRequest,
    LLMPreferencesRequest,
    LLMProfileUpsertRequest,
    LLMSettingsRequest,
    LLMSettingsResponse,
    ModelProfile,
    ProjectArcSnapshotFields,
    ProjectChapterPublishRequest,
    ProjectDeleteResponse,
    ProjectDetail,
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
    TaskMutationResponse,
    TaskSummaryResponse,
    ThreadInfo,
    UploadJobResultRequest,
)
from forwin.config import Config
from forwin.models.base import Base, get_engine, get_session_factory, init_db
from forwin.models.project import Project, ChapterPlan
from forwin.models.entity import Entity
from forwin.models.event import CanonEvent, EventEntityLink
from forwin.models.publisher import PublisherCommentSyncJob, PublisherConnectionState, PublisherExtensionClient, PublisherRawComment, PublisherUploadJob
from forwin.models.thread import PlotThread
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.timeline import ChapterTimeline, StoryTimePoint
from forwin.models.phase4 import NPCIntentSnapshot
import forwin.models.phase  # noqa: F401
from forwin.state.repo import StateRepository
from forwin.orchestrator.loop import WritingOrchestrator
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

# Simple in-memory task tracking (no Redis for Phase 0.5)
_tasks: dict[str, dict] = {}
_tasks_lock = threading.Lock()
_TASK_RETENTION_SECONDS = 6 * 60 * 60
_MAX_TASKS = 256
_DISPLAY_TZ = ZoneInfo("America/Los_Angeles")
_GENERATION_TERMINAL_STATUSES = {"completed", "partial_failed", "failed", "needs_review", "cancelled"}
_UPLOAD_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
_GENERATION_STAGE_ORDER = [
    "queued",
    "planning_arc",
    "creating_project",
    "resolving_arc_envelope",
    "running_provisional_preview",
    "assembling_context",
    "writing_chapter",
    "continuity_review",
    "applying_canon",
    "running_post_acceptance",
    "paused_for_review",
    "completed",
    "failed",
    "terminating",
    "cancelled",
]


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


def _prune_tasks() -> None:
    with _tasks_lock:
        if not _tasks:
            return

        now = _utcnow()
        terminal_statuses = _GENERATION_TERMINAL_STATUSES
        stale_ids = [
            task_id
            for task_id, task in _tasks.items()
            if task.get("deleted")
            or (
                task.get("status") in terminal_statuses
                and (now - task.get("updated_at", now)).total_seconds() > _TASK_RETENTION_SECONDS
            )
        ]
        for task_id in stale_ids:
            _tasks.pop(task_id, None)

        if len(_tasks) <= _MAX_TASKS:
            return

        ordered = sorted(
            _tasks.items(),
            key=lambda item: item[1].get("updated_at", now),
        )
        overflow = len(_tasks) - _MAX_TASKS
        for task_id, _task in ordered[:overflow]:
            _tasks.pop(task_id, None)


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


def _task_is_deletable(task: dict[str, Any]) -> bool:
    return not task.get("deleted") and _task_is_terminal(str(task.get("status", "")))


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
        terminable=_task_is_terminable(task),
        deletable=_task_is_deletable(task),
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


def _build_project_task_center_item(session, project: Project) -> TaskCenterItemResponse:
    plans = session.execute(
        select(ChapterPlan).where(ChapterPlan.project_id == project.id).order_by(ChapterPlan.chapter_number)
    ).scalars().all()
    requested = len(plans)
    completed = [
        int(plan.chapter_number)
        for plan in plans
        if plan.status in {"accepted", "drafted"}
    ]
    failed = [int(plan.chapter_number) for plan in plans if plan.status == "failed"]
    paused = [int(plan.chapter_number) for plan in plans if plan.status == "needs_review"]
    if paused:
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
    message = "项目入口（当前没有活跃生成任务）"
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
        subtitle=f"项目入口 · {project.genre}",
        project_id=project.id,
        message=message,
        current_stage=current_stage,
        stage_history=stage_history,
        requested_chapters=requested,
        current_chapter=current_chapter,
        completed_chapters=completed,
        failed_chapters=failed,
        paused_chapters=paused,
        created_at=_display_datetime(project.created_at),
        updated_at=_display_datetime(project.updated_at),
        terminable=False,
        deletable=False,
    )


def _list_project_backed_task_items(limit: int) -> list[TaskCenterItemResponse]:
    with _tasks_lock:
        live_project_ids = {
            str(task.get("project_id", "")).strip()
            for task in _tasks.values()
            if not task.get("deleted") and str(task.get("project_id", "")).strip()
        }
    session = _get_session()
    try:
        projects = session.execute(
            select(Project).order_by(Project.updated_at.desc()).limit(max(1, min(int(limit or 50), 200)))
        ).scalars().all()
        items: list[TaskCenterItemResponse] = []
        for project in projects:
            if project.id in live_project_ids:
                continue
            items.append(_build_project_task_center_item(session, project))
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
        return _build_project_task_center_item(session, project)
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
    with _tasks_lock:
        task = _tasks.get(task_id)
        if task is None or task.get("deleted"):
            return
        normalized = dict(changes)
        if task.get("cancel_requested") and normalized.get("status") in {"starting", "running", "needs_review"}:
            normalized.pop("status", None)
        if task.get("cancel_requested") and normalized.get("current_stage") not in {"terminating", "cancelled"}:
            normalized.pop("current_stage", None)
        if "message" in normalized:
            normalized["message"] = str(normalized.get("message") or "")
        if "status" in normalized and normalized["status"] == "cancelled":
            normalized["current_stage"] = "cancelled"
        elif "status" in normalized and normalized["status"] == "terminating":
            normalized["current_stage"] = "terminating"
        if "current_chapter" in normalized:
            try:
                normalized["current_chapter"] = int(normalized["current_chapter"] or 0)
            except (TypeError, ValueError):
                normalized["current_chapter"] = 0

        now = _utcnow()
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


def _task_should_abort(task_id: str) -> bool:
    with _tasks_lock:
        task = _tasks.get(task_id)
        if task is None or task.get("deleted"):
            return True
        return bool(task.get("cancel_requested"))


def _get_generation_task_or_404(task_id: str) -> dict[str, Any]:
    _prune_tasks()
    with _tasks_lock:
        task_row = _tasks.get(task_id)
        task = dict(task_row) if task_row is not None else None
    if task is None or task.get("deleted"):
        raise HTTPException(404, "任务不存在")
    return task


def _list_generation_tasks(limit: int) -> list[tuple[str, dict[str, Any]]]:
    _prune_tasks()
    normalized_limit = max(1, min(int(limit or 30), 100))
    with _tasks_lock:
        ordered = [
            (task_id, dict(task))
            for task_id, task in sorted(
                _tasks.items(),
                key=lambda item: item[1].get("updated_at", _utcnow()),
                reverse=True,
            )
            if not task.get("deleted")
        ][:normalized_limit]
    return ordered


def _shutdown_runtime_state() -> None:
    global _engine, _SessionFactory, _orchestrator, _publisher_manager, _runtime_settings

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

    _config = Config.from_env()
    # Allow override via env
    db_path = os.environ.get("FORWIN_DB_PATH", _config.db_path)
    _config = _config.model_copy(update={"db_path": db_path})

    Path(_config.db_path).parent.mkdir(parents=True, exist_ok=True)
    _engine = get_engine(_config.db_path)
    init_db(_engine)
    _SessionFactory = get_session_factory(_engine)
    _orchestrator = WritingOrchestrator(_config)
    _publisher_manager = PublisherManager(
        _SessionFactory,
        extension_api_key=_config.publisher_extension_api_key,
        preferred_client_id=_config.publisher_preferred_client_id,
    )
    with _SessionFactory() as bootstrap_session:
        created_envelopes = _orchestrator.arc_envelope_manager.backfill_missing_resolutions(
            session=bootstrap_session
        )
        if created_envelopes:
            bootstrap_session.commit()
            logger.info("Backfilled %d active arc envelopes.", created_envelopes)
        else:
            bootstrap_session.rollback()
    _publisher_manager.requeue_interrupted_upload_jobs()
    _runtime_settings = RuntimeSettingsStore(
        _config.runtime_settings_path,
        default_api_key=_config.minimax_api_key,
        default_base_url=_config.minimax_base_url,
        default_model=_config.minimax_model,
        default_operation_mode=_config.operation_mode,
        default_freeze_failed_candidates=_config.freeze_failed_candidates,
    )
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

    task_id = uuid.uuid4().hex[:12]
    task_record = _create_task_record(
        message=f"开始生成 {req.num_chapters} 章。",
        title=(req.premise or "").strip()[:36] or "未命名生成任务",
        subtitle=f"{req.genre} · {req.num_chapters} 章",
        requested_chapters=req.num_chapters,
    )
    with _tasks_lock:
        _tasks[task_id] = task_record

    t = threading.Thread(
        target=run_generation_with_config,
        args=(
            task_id,
            req.premise,
            req.genre,
            req.num_chapters,
            runtime_config,
            _update_task,
            logger,
            lambda: _task_should_abort(task_id),
        ),
        daemon=True,
    )
    t.start()

    return _serialize_task(task_id, task_record)


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
        _delete_project(session, project_id)
        session.commit()
        return ProjectDeleteResponse(
            ok=True,
            project_id=project_id,
            message=f"项目《{project.title}》已删除。",
        )
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
        )
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
            issues=[ChapterReviewIssueInfo.model_validate(issue) for issue in issues],
            artifact_meta_path=draft.llm_raw_response,
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
    try:
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
            total_chapters = session.execute(
                select(func.count(ChapterPlan.id)).where(ChapterPlan.project_id == project_id)
            ).scalar_one()
        finally:
            session.close()
        task_id = uuid.uuid4().hex[:12]
        task_record = _create_task_record(
            message=f"已接受第{chapter_number}章，准备继续后续章节。",
            title=f"继续生成 {project_id}",
            subtitle=f"项目 {project_id}",
            requested_chapters=int(total_chapters or 0),
        )
        with _tasks_lock:
            _tasks[task_id] = task_record
        _update_task(
            task_id,
            project_id=project_id,
            frozen_artifacts=[result["frozen_artifact"]] if result["frozen_artifact"] else [],
        )
        thread = threading.Thread(
            target=run_continue_project_with_config,
            args=(task_id, project_id, runtime_config, _update_task, logger, lambda: _task_should_abort(task_id)),
            daemon=True,
        )
        thread.start()
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
