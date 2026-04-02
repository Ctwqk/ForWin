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

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func

from forwin.config import Config
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.project import Project, ChapterPlan
from forwin.models.entity import Entity
from forwin.models.event import CanonEvent
from forwin.models.phase import (
    ProjectReplanEvent,
    ProjectStageAnalysis,
    ProvisionalBandExecution,
    ProvisionalChapterLedger,
)
from forwin.models.phase4 import NPCIntentSnapshot, WorldSimulationTurn
from forwin.models.publisher import PublisherCommentSyncJob, PublisherConnectionState, PublisherExtensionClient, PublisherRawComment, PublisherUploadJob
from forwin.models.thread import PlotThread
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.state.repo import StateRepository
from forwin.orchestrator.loop import WritingOrchestrator
from forwin.publishers import PublisherManager
from forwin.runtime_settings import RuntimeSettingsStore
from forwin.state.query_helpers import (
    load_latest_active_arc_envelope_by_project,
    load_latest_drafts_by_plan_id,
    load_latest_replan_event_by_project,
    load_latest_stage_analysis_by_project,
    load_latest_world_turn_by_project,
)

logger = logging.getLogger(__name__)

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
_backend_upload_inflight: set[str] = set()
_backend_upload_lock = threading.Lock()
_TASK_RETENTION_SECONDS = 6 * 60 * 60
_MAX_TASKS = 256


def _get_session():
    return _SessionFactory()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _prune_tasks() -> None:
    with _tasks_lock:
        if not _tasks:
            return

        now = _utcnow()
        terminal_statuses = {"completed", "partial_failed", "failed", "needs_review"}
        stale_ids = [
            task_id
            for task_id, task in _tasks.items()
            if task.get("status") in terminal_statuses
            and (now - task.get("updated_at", now)).total_seconds() > _TASK_RETENTION_SECONDS
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


def _create_task_record(message: str = "") -> dict[str, Any]:
    now = _utcnow()
    _prune_tasks()
    return {
        "status": "starting",
        "project_id": None,
        "error": None,
        "message": message,
        "failed_chapters": [],
        "paused_chapters": [],
        "frozen_artifacts": [],
        "created_at": now,
        "updated_at": now,
    }


def _update_task(task_id: str, **changes: Any) -> None:
    with _tasks_lock:
        task = _tasks.get(task_id)
        if task is None:
            return
        task.update(changes)
        task["updated_at"] = _utcnow()


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
    if hasattr(_config, "model_copy"):
        _config = _config.model_copy(update={"db_path": db_path})
    else:
        _config = Config(**{**_config.model_dump(), "db_path": db_path})

    Path(_config.db_path).parent.mkdir(parents=True, exist_ok=True)
    _engine = get_engine(_config.db_path)
    init_db(_engine)
    _SessionFactory = get_session_factory(_engine)
    _orchestrator = WritingOrchestrator(_config)
    _publisher_manager = PublisherManager(
        _SessionFactory,
        extension_api_key=_config.publisher_extension_api_key,
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
    recovered_platforms = _publisher_manager.requeue_interrupted_upload_jobs()
    _runtime_settings = RuntimeSettingsStore(
        _config.runtime_settings_path,
        default_api_key=_config.minimax_api_key,
        default_base_url=_config.minimax_base_url,
        default_model=_config.minimax_model,
        default_operation_mode=_config.operation_mode,
        default_freeze_failed_candidates=_config.freeze_failed_candidates,
    )
    for platform_id in recovered_platforms:
        _start_pending_backend_uploads(platform_id)

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
# Request / Response models
# ---------------------------------------------------------------------------

class GenerateRequest(BaseModel):
    premise: str
    genre: str = "玄幻"
    num_chapters: int = 3
    api_key: str | None = None
    base_url: str = "https://api.minimaxi.com/v1"
    model: str = "MiniMax-M2.7"
    operation_mode: str | None = None
    freeze_failed_candidates: bool | None = None


class LLMSettingsRequest(BaseModel):
    api_key: str = ""
    base_url: str = "https://api.minimaxi.com/v1"
    model: str = "MiniMax-M2.7"
    operation_mode: str = "blackbox"
    freeze_failed_candidates: bool = True


class LLMSettingsResponse(BaseModel):
    has_api_key: bool
    base_url: str
    model: str
    operation_mode: str = "blackbox"
    freeze_failed_candidates: bool = True
    message: str = ""


class TaskResponse(BaseModel):
    task_id: str
    status: str  # running / completed / partial_failed / failed
    project_id: str | None = None
    error: str | None = None
    message: str = ""
    failed_chapters: list[int] = Field(default_factory=list)
    paused_chapters: list[int] = Field(default_factory=list)
    frozen_artifacts: list[str] = Field(default_factory=list)


class ProjectSummary(BaseModel):
    id: str
    title: str
    genre: str
    premise: str = ""
    created_at: str = ""
    latest_stage: str = ""
    pacing_verdict: str = ""
    pacing_summary: str = ""
    last_replan_status: str = ""
    last_replan_strategy: str = ""
    last_replan_reason: str = ""
    current_time_label: str = ""
    world_pressure_level: str = ""
    world_pressure_summary: str = ""
    chapters: list[dict[str, object]] = Field(default_factory=list)
    active_arc_policy_tier: str = ""
    active_arc_target_size: int = 0
    active_arc_soft_min: int = 0
    active_arc_soft_max: int = 0
    active_arc_detailed_band_size: int = 0
    active_arc_frozen_zone_size: int = 0
    active_arc_confidence: float = 0.0


class EntityInfo(BaseModel):
    id: str
    kind: str
    name: str
    description: str
    importance: int


class ThreadInfo(BaseModel):
    id: str
    name: str
    description: str
    status: str
    priority: int


class ChapterInfo(BaseModel):
    chapter_number: int
    title: str
    status: str
    char_count: int = 0
    summary: str = ""


class ProjectDetail(BaseModel):
    id: str
    title: str
    premise: str
    genre: str
    setting_summary: str
    characters: list[EntityInfo] = []
    locations: list[EntityInfo] = []
    factions: list[EntityInfo] = []
    threads: list[ThreadInfo] = []
    chapters: list[ChapterInfo] = []
    latest_stage: str = ""
    progress_ratio: float = 0.0
    pacing_verdict: str = ""
    pacing_summary: str = ""
    current_time_label: str = ""
    recent_replans: list[dict[str, object]] = []
    world_pressure_level: str = ""
    world_pressure_summary: str = ""
    npc_intent_count: int = 0
    recent_npc_intents: list[dict[str, object]] = []
    active_arc_policy_tier: str = ""
    active_arc_target_size: int = 0
    active_arc_soft_min: int = 0
    active_arc_soft_max: int = 0
    active_arc_detailed_band_size: int = 0
    active_arc_frozen_zone_size: int = 0
    active_arc_confidence: float = 0.0


class ChapterDetail(BaseModel):
    chapter_number: int
    title: str
    body: str
    char_count: int
    summary: str
    status: str
    version: int = 1


class ChapterReviewIssueInfo(BaseModel):
    rule_name: str
    severity: str
    description: str
    entity_names: list[str] = Field(default_factory=list)


class ChapterReviewDetail(BaseModel):
    project_id: str
    chapter_number: int
    title: str
    status: str
    draft_id: str
    version: int
    body: str
    summary: str
    verdict: str
    issues: list[ChapterReviewIssueInfo] = Field(default_factory=list)
    artifact_meta_path: str = ""


class ChapterReviewApproveRequest(BaseModel):
    continue_generation: bool = False


class ChapterReviewApproveResponse(BaseModel):
    ok: bool
    project_id: str
    chapter_number: int
    status: str
    message: str
    task_id: str = ""
    frozen_artifact: str = ""


class ProvisionalChapterLedgerInfo(BaseModel):
    chapter_number: int
    title: str
    summary: str = ""
    verdict: str
    char_count: int = 0
    artifact_meta_path: str = ""
    draft_blob_path: str = ""
    current_time_label: str = ""
    projected_time_label: str = ""
    state_changes: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    thread_beats: list[dict[str, Any]] = Field(default_factory=list)
    time_advance: dict[str, Any] = Field(default_factory=dict)
    issues: list[dict[str, Any]] = Field(default_factory=list)
    error: str = ""
    created_at: str = ""


class ProvisionalBandDetail(BaseModel):
    project_id: str
    arc_id: str
    band_id: str
    aggregate_verdict: str
    preview_char_count: int = 0
    issue_count: int = 0
    failure_count: int = 0
    artifact_path: str = ""
    chapter_numbers: list[int] = Field(default_factory=list)
    created_at: str = ""
    chapters: list[ProvisionalChapterLedgerInfo] = Field(default_factory=list)


class PublisherPlatformInfo(BaseModel):
    platform_id: str
    display_name: str
    login_url: str
    dashboard_url: str
    publish_url: str
    supported_login_methods: list[str] = Field(default_factory=list)
    supported_actions: list[str] = Field(default_factory=list)
    connected: bool = False
    extension_online: bool = False
    last_heartbeat_at: str = ""
    last_error: str = ""
    extension_client_id: str = ""


class PublisherUploadJobCreateRequest(BaseModel):
    platform: str
    book_name: str
    chapter_title: str
    body: str
    upload_url: str | None = None
    publish: bool = True


class PublisherUploadJobResponse(BaseModel):
    job_id: str
    platform: str
    display_name: str
    status: str
    book_name: str
    chapter_title: str
    body: str
    upload_url: str | None = None
    publish: bool
    extension_client_id: str = ""
    current_url: str = ""
    message: str
    error: str = ""
    result_payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    claimed_at: str = ""
    started_at: str = ""
    finished_at: str = ""


class ExtensionPlatformHeartbeat(BaseModel):
    platform: str
    connected: bool = False
    login_method: str = "scan"
    last_error: str = ""
    cookies: list[ExtensionBrowserCookie] = Field(default_factory=list)
    raw_state: dict[str, Any] = Field(default_factory=dict)


class ExtensionHeartbeatRequest(BaseModel):
    client_id: str
    extension_version: str = ""
    browser_name: str = ""
    browser_version: str = ""
    backend_base_url: str = ""
    platforms: list[ExtensionPlatformHeartbeat] = Field(default_factory=list)


class ExtensionHeartbeatResponse(BaseModel):
    ok: bool
    message: str
    server_time: str


class ExtensionBrowserCookie(BaseModel):
    name: str
    value: str = ""
    domain: str = ""
    path: str = "/"
    secure: bool = False
    httpOnly: bool = False
    sameSite: str = "Lax"
    expirationDate: float | None = None


class ExtensionSessionSyncRequest(BaseModel):
    client_id: str
    platform: str
    cookies: list[ExtensionBrowserCookie] = Field(default_factory=list)


class ExtensionSessionSyncResponse(BaseModel):
    ok: bool
    message: str
    server_time: str
    cookie_count: int = 0


class ExtensionClaimUploadJobRequest(BaseModel):
    client_id: str
    connected_platforms: list[str] = Field(default_factory=list)


class ExtensionClaimUploadJobResponse(BaseModel):
    found: bool
    job: PublisherUploadJobResponse | None = None


class UploadJobResultRequest(BaseModel):
    client_id: str
    status: str
    message: str = ""
    current_url: str = ""
    error: str = ""
    result_payload: dict[str, Any] = Field(default_factory=dict)


class PublisherCommentSyncJobRequest(BaseModel):
    platform: str
    work_id: str = ""
    work_name: str = ""
    chapter_id: str = ""
    chapter_title: str = ""
    limit: int = 100


class PublisherCommentSyncJobResponse(BaseModel):
    job_id: str
    platform: str
    status: str
    work_id: str = ""
    work_name: str = ""
    chapter_id: str = ""
    chapter_title: str = ""
    limit: int
    created_at: str


class PublisherRawCommentInput(BaseModel):
    remote_comment_id: str
    work_id: str = ""
    work_name: str = ""
    chapter_id: str = ""
    chapter_title: str = ""
    author_id: str = ""
    author_name: str = ""
    body: str = ""
    parent_remote_comment_id: str = ""
    created_at: str = ""
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class ExtensionCommentsBatchRequest(BaseModel):
    client_id: str
    platform: str
    job_id: str = ""
    comments: list[PublisherRawCommentInput] = Field(default_factory=list)


class ExtensionCommentsBatchResponse(BaseModel):
    ok: bool
    message: str
    inserted: int
    updated: int
    synced_at: str


def _copy_config(base_config: Config, **updates: object) -> Config:
    if hasattr(base_config, "model_copy"):
        return base_config.model_copy(update=updates)
    return Config(**{**base_config.model_dump(), **updates})


def _build_runtime_config(req: GenerateRequest) -> Config:
    stored = _runtime_settings.get() if _runtime_settings else {}
    api_key = (req.api_key or "").strip() or stored.get("api_key", _config.minimax_api_key)
    base_url = (req.base_url or "").strip() or stored.get("base_url", _config.minimax_base_url)
    model = (req.model or "").strip() or stored.get("model", _config.minimax_model)
    operation_mode = (req.operation_mode or "").strip() or str(stored.get("operation_mode", _config.operation_mode))
    freeze_failed_candidates = (
        req.freeze_failed_candidates
        if req.freeze_failed_candidates is not None
        else bool(stored.get("freeze_failed_candidates", _config.freeze_failed_candidates))
    )
    return _copy_config(
        _config,
        minimax_api_key=api_key,
        minimax_base_url=base_url,
        minimax_model=model,
        operation_mode=operation_mode,
        freeze_failed_candidates=freeze_failed_candidates,
    )


def _build_saved_runtime_config() -> Config:
    stored = _runtime_settings.get() if _runtime_settings else {}
    return _copy_config(
        _config,
        minimax_api_key=str(stored.get("api_key", _config.minimax_api_key)),
        minimax_base_url=str(stored.get("base_url", _config.minimax_base_url)),
        minimax_model=str(stored.get("model", _config.minimax_model)),
        operation_mode=str(stored.get("operation_mode", _config.operation_mode)),
        freeze_failed_candidates=bool(
            stored.get("freeze_failed_candidates", _config.freeze_failed_candidates)
        ),
    )


def _run_generation_with_config(
    task_id: str,
    premise: str,
    genre: str,
    num_chapters: int,
    config: Config,
):
    orchestrator = WritingOrchestrator(config)
    try:
        _update_task(task_id, status="running")
        result = orchestrator.run(
            premise=premise,
            genre=genre,
            num_chapters=num_chapters,
        )
        _update_task(
            task_id,
            status=result.status,
            project_id=result.project_id,
            failed_chapters=result.failed_chapters,
            paused_chapters=result.paused_chapters,
            frozen_artifacts=result.frozen_artifacts,
        )
        if result.failed_chapters:
            failed_str = ", ".join(str(chapter) for chapter in result.failed_chapters)
            _update_task(
                task_id,
                error=f"以下章节生成失败: {failed_str}",
                message=(
                f"已完成 {len(result.completed_chapters)} / {result.requested_chapters} 章，"
                f"失败章节: {failed_str}"
                ),
            )
        elif result.paused_chapters:
            paused_str = ", ".join(str(chapter) for chapter in result.paused_chapters)
            _update_task(task_id, message=f"已进入人工检查点，暂停章节: {paused_str}")
        else:
            _update_task(task_id, message=f"已完成 {result.requested_chapters} / {result.requested_chapters} 章")
    except Exception as exc:
        logger.exception("Generation failed for task %s", task_id)
        _update_task(
            task_id,
            status="failed",
            project_id=getattr(exc, "project_id", None),
            error=str(exc),
            message="生成任务失败",
        )
    finally:
        orchestrator.llm_client.close()
        orchestrator.engine.dispose()


def _run_continue_project_with_config(
    task_id: str,
    project_id: str,
    config: Config,
):
    orchestrator = WritingOrchestrator(config)
    try:
        _update_task(task_id, status="running")
        result = orchestrator.continue_project(project_id)
        _update_task(
            task_id,
            status=result.status,
            project_id=result.project_id,
            failed_chapters=result.failed_chapters,
            paused_chapters=result.paused_chapters,
            frozen_artifacts=result.frozen_artifacts,
        )
        if result.failed_chapters:
            failed_str = ", ".join(str(chapter) for chapter in result.failed_chapters)
            _update_task(
                task_id,
                error=f"以下章节生成失败: {failed_str}",
                message=(
                f"继续执行后完成 {len(result.completed_chapters)} 章，"
                f"失败章节: {failed_str}"
                ),
            )
        elif result.paused_chapters:
            paused_str = ", ".join(str(chapter) for chapter in result.paused_chapters)
            _update_task(task_id, message=f"继续执行后再次进入人工检查点，暂停章节: {paused_str}")
        elif result.completed_chapters:
            completed_str = ", ".join(str(chapter) for chapter in result.completed_chapters)
            _update_task(task_id, message=f"继续执行完成章节: {completed_str}")
        else:
            _update_task(task_id, message="没有剩余章节需要继续执行。")
    except Exception as exc:
        logger.exception("Continue generation failed for task %s", task_id)
        _update_task(
            task_id,
            status="failed",
            project_id=project_id,
            error=str(exc),
            message="继续生成失败",
        )
    finally:
        orchestrator.llm_client.close()
        orchestrator.engine.dispose()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def home_page():
    settings = (
        _runtime_settings.get()
        if _runtime_settings
        else {
            "api_key": "",
            "base_url": _config.minimax_base_url if _config else "https://api.minimaxi.com/v1",
            "model": _config.minimax_model if _config else "MiniMax-M2.7",
            "operation_mode": _config.operation_mode if _config else "blackbox",
            "freeze_failed_candidates": _config.freeze_failed_candidates if _config else True,
        }
    )
    base_url = settings["base_url"]
    model = settings["model"]
    operation_mode = settings["operation_mode"]
    freeze_failed_candidates = settings["freeze_failed_candidates"]
    default_genre = "玄幻"
    default_chapters = 3
    return HTMLResponse(
        f"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ForWin 创作台</title>
  <style>
    :root {{
      --paper:#f7f1e7;
      --panel:#fffaf3;
      --ink:#1d2522;
      --muted:#6e6250;
      --accent:#b24a28;
      --accent-2:#284d46;
      --line:#e7d8c4;
      --shadow:0 24px 70px rgba(68,46,24,.10);
    }}
    * {{ box-sizing:border-box; }}
    body {{
      margin:0;
      color:var(--ink);
      font-family:"Noto Serif SC","Source Han Serif SC",serif;
      background:
        radial-gradient(circle at top left, rgba(255,248,224,.95), transparent 33%),
        radial-gradient(circle at 85% 15%, rgba(219,118,63,.13), transparent 22%),
        linear-gradient(140deg, #f2eadb, #eadfcb 58%, #efe7da);
    }}
    .wrap {{ max-width:1180px; margin:0 auto; padding:36px 20px 72px; }}
    .hero {{
      display:grid; grid-template-columns:1.2fr .95fr; gap:22px; align-items:stretch;
    }}
    .panel {{
      background:rgba(255,250,243,.9);
      border:1px solid var(--line);
      border-radius:24px;
      box-shadow:var(--shadow);
      backdrop-filter:blur(8px);
    }}
    .lead {{
      padding:34px 32px;
      min-height:320px;
      display:flex;
      flex-direction:column;
      justify-content:space-between;
      background:
        linear-gradient(180deg, rgba(255,255,255,.28), rgba(255,255,255,0)),
        radial-gradient(circle at bottom right, rgba(178,74,40,.11), transparent 30%);
    }}
    .eyebrow {{
      display:inline-flex;
      align-items:center;
      gap:8px;
      border:1px solid rgba(178,74,40,.2);
      border-radius:999px;
      padding:7px 12px;
      color:var(--accent);
      font-size:13px;
      letter-spacing:.06em;
    }}
    h1 {{ margin:16px 0 10px; font-size:44px; line-height:1.06; }}
    .lead p {{ margin:0; font-size:16px; line-height:1.8; color:var(--muted); }}
    .hero-actions {{ display:flex; gap:12px; flex-wrap:wrap; margin-top:24px; }}
    a.button, button {{
      appearance:none;
      border:0;
      border-radius:999px;
      padding:12px 18px;
      cursor:pointer;
      font:inherit;
      text-decoration:none;
      transition:transform .18s ease, box-shadow .18s ease, opacity .18s ease;
    }}
    a.button:hover, button:hover {{ transform:translateY(-1px); }}
    .primary {{
      color:#fff;
      background:linear-gradient(135deg, var(--accent), #cc6a3d);
      box-shadow:0 16px 30px rgba(178,74,40,.24);
    }}
    .secondary {{
      color:var(--accent-2);
      background:rgba(40,77,70,.08);
      border:1px solid rgba(40,77,70,.16);
    }}
    .form {{
      padding:28px 24px;
      display:grid;
      gap:14px;
      align-content:start;
    }}
    .form h2 {{ margin:0 0 4px; font-size:24px; }}
    .hint {{ color:var(--muted); font-size:14px; line-height:1.6; }}
    .grid {{
      display:grid;
      grid-template-columns:repeat(2, minmax(0, 1fr));
      gap:14px;
    }}
    label {{ display:block; font-size:14px; margin-bottom:6px; color:var(--muted); }}
    input, textarea, select {{
      width:100%;
      border:1px solid var(--line);
      border-radius:16px;
      padding:12px 14px;
      background:#fffdfa;
      color:var(--ink);
      font:inherit;
    }}
    textarea {{ min-height:180px; resize:vertical; }}
    .stats {{
      display:grid;
      grid-template-columns:repeat(3, minmax(0,1fr));
      gap:14px;
      margin-top:22px;
    }}
    .stat {{
      padding:16px 18px;
      border-radius:18px;
      background:rgba(255,255,255,.45);
      border:1px solid rgba(231,216,196,.8);
    }}
    .stat b {{ display:block; font-size:26px; margin-bottom:6px; }}
    .row {{
      display:flex;
      gap:12px;
      flex-wrap:wrap;
      align-items:center;
    }}
    .status {{
      margin-top:8px;
      padding:14px 16px;
      border-radius:16px;
      background:rgba(255,255,255,.54);
      border:1px solid var(--line);
      color:var(--muted);
      min-height:56px;
      line-height:1.6;
      white-space:pre-wrap;
    }}
    .projects {{
      margin-top:26px;
      padding:24px;
    }}
    .projects-head {{
      display:flex;
      justify-content:space-between;
      align-items:center;
      gap:12px;
      margin-bottom:12px;
    }}
    .project-list {{
      display:grid;
      grid-template-columns:repeat(auto-fit, minmax(240px, 1fr));
      gap:14px;
    }}
    .project-card {{
      padding:18px;
      border-radius:18px;
      border:1px solid var(--line);
      background:rgba(255,255,255,.58);
    }}
    .project-card h3 {{ margin:0 0 8px; font-size:19px; }}
    .project-card p {{ margin:0 0 8px; color:var(--muted); line-height:1.6; }}
    .muted {{ color:var(--muted); }}
    @media (max-width: 900px) {{
      .hero {{ grid-template-columns:1fr; }}
      .grid, .stats {{ grid-template-columns:1fr; }}
      h1 {{ font-size:36px; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <article class="panel lead">
        <div>
          <span class="eyebrow">FORWIN · Longform Web Novel Studio</span>
          <h1>从设定到章节，把长篇连载真正跑起来</h1>
          <p>这里可以直接发起生成任务，也可以跳到平台发布页做番茄 / 起点登录与上传。默认已经指向 MiniMax 的 OpenAI 兼容接口，你只需要在网页上填自己的 API Key 就能开始。</p>
          <div class="stats">
            <div class="stat">
              <b>MiniMax</b>
              <span class="muted">默认 Base URL 已预填</span>
            </div>
            <div class="stat">
              <b>Single</b>
              <span class="muted">阶段 0.5 默认先走单章草稿流程，优先保证生成闭环</span>
            </div>
            <div class="stat">
              <b>Phase 2</b>
              <span class="muted">支持项目生成、查询与发布联调</span>
            </div>
          </div>
        </div>
        <div class="hero-actions">
          <a class="button primary" href="#generator">开始生成</a>
          <a class="button secondary" href="/publishers">去平台登录与上传</a>
        </div>
      </article>
      <section class="panel form" id="generator">
        <div>
          <h2>生成新项目</h2>
          <div class="hint">你可以先把 API Key 保存成默认配置，也可以只在这一次生成里临时覆盖。Base URL 与模型名已经按 MiniMax M2.7 预填好了。</div>
        </div>
        <div class="panel" style="padding:18px 18px 12px; border-radius:18px; box-shadow:none; background:rgba(255,255,255,.45);">
          <div class="row" style="justify-content:space-between; align-items:flex-start;">
            <div>
              <h3 style="margin:0 0 6px; font-size:18px;">默认模型设置</h3>
              <div class="hint">保存后，后续生成会自动使用这份配置。这样你设置完以后，我也可以直接继续联调测试。</div>
            </div>
            <div id="saved_badge" class="hint">已保存 API Key：{"是" if settings["api_key"] else "否"}</div>
          </div>
          <div class="grid" style="margin-top:12px;">
            <div>
              <label for="saved_api_key">已保存 API Key</label>
              <input id="saved_api_key" type="password" placeholder="sk-..." autocomplete="off">
            </div>
            <div>
              <label for="saved_model">已保存 Model</label>
              <input id="saved_model" value="{model}" spellcheck="false">
            </div>
          </div>
          <div class="grid" style="margin-top:6px;">
            <div>
              <label for="saved_operation_mode">运行模式</label>
              <select id="saved_operation_mode">
                <option value="blackbox" {"selected" if operation_mode == "blackbox" else ""}>黑箱模式</option>
                <option value="checkpoint" {"selected" if operation_mode == "checkpoint" else ""}>检查点模式</option>
                <option value="copilot" {"selected" if operation_mode == "copilot" else ""}>共驾模式</option>
              </select>
              <div id="saved_mode_hint" class="hint" style="margin-top:6px;"></div>
            </div>
            <div style="display:flex;align-items:end;">
              <label style="display:flex; gap:10px; align-items:flex-start; margin:0;">
                <input id="saved_freeze_failed_candidates" type="checkbox" style="width:auto; margin-top:4px;" {"checked" if freeze_failed_candidates else ""}>
                <span>状态写入失败时冻结 candidate artifact</span>
              </label>
            </div>
          </div>
          <div style="margin-top:6px;">
            <label for="saved_base_url">已保存 Base URL</label>
            <input id="saved_base_url" value="{base_url}" spellcheck="false">
          </div>
          <div class="row" style="margin-top:10px;">
            <button class="secondary" onclick="saveSettings()">保存默认配置</button>
            <button class="secondary" onclick="loadSettings()">重新读取配置</button>
          </div>
          <div id="settings_status" class="status" style="margin-top:12px;">默认配置尚未修改。</div>
        </div>
        <div>
          <label for="api_key">MiniMax API Key</label>
          <input id="api_key" type="password" placeholder="sk-..." autocomplete="off">
        </div>
        <div class="grid">
          <div>
            <label for="base_url">Base URL</label>
            <input id="base_url" value="{base_url}" spellcheck="false">
          </div>
          <div>
            <label for="model">Model</label>
            <input id="model" value="{model}" spellcheck="false">
          </div>
        </div>
        <div class="grid">
          <div>
            <label for="genre">题材</label>
            <select id="genre">
              <option selected>{default_genre}</option>
              <option>仙侠</option>
              <option>都市</option>
              <option>悬疑</option>
              <option>科幻</option>
              <option>历史</option>
            </select>
          </div>
          <div>
            <label for="num_chapters">章节数</label>
            <input id="num_chapters" type="number" min="1" max="20" value="{default_chapters}">
          </div>
        </div>
        <div class="grid">
          <div>
            <label for="operation_mode">本次运行模式</label>
            <select id="operation_mode">
              <option value="blackbox" {"selected" if operation_mode == "blackbox" else ""}>黑箱模式</option>
              <option value="checkpoint" {"selected" if operation_mode == "checkpoint" else ""}>检查点模式</option>
              <option value="copilot" {"selected" if operation_mode == "copilot" else ""}>共驾模式</option>
            </select>
            <div id="mode_hint" class="hint" style="margin-top:6px;"></div>
          </div>
          <div style="display:flex;align-items:end;">
            <label style="display:flex; gap:10px; align-items:flex-start; margin:0 0 10px;">
              <input id="freeze_failed_candidates" type="checkbox" style="width:auto; margin-top:4px;" {"checked" if freeze_failed_candidates else ""}>
              <span>状态写入失败时冻结 candidate artifact</span>
            </label>
          </div>
        </div>
        <div>
          <label for="premise">故事前提</label>
          <textarea id="premise" placeholder="例如：废土纪元三百年后，失忆少年在边境黑井苏醒，身上带着一枚会记录他死亡次数的青铜环。"></textarea>
        </div>
        <div class="row">
          <button class="primary" onclick="startGeneration()">提交生成任务</button>
          <button class="secondary" onclick="loadProjects()">刷新项目列表</button>
        </div>
        <div id="task_status" class="status">等待提交任务。</div>
      </section>
    </section>

    <section class="panel projects">
      <div class="projects-head">
        <div>
          <h2 style="margin:0 0 6px; font-size:24px;">已有项目</h2>
          <div class="hint">任务完成后会出现在这里，方便你继续查看章节或去发布页操作。</div>
        </div>
        <a class="button secondary" href="/publishers">平台发布页</a>
      </div>
      <div id="project_list" class="project-list"></div>
    </section>
  </div>
  <script>
    let currentTaskId = null;

    function setStatus(text) {{
      document.getElementById('task_status').textContent = text;
    }}

    function clearNode(node) {{
      if (!node) return;
      node.replaceChildren();
    }}

    function createNode(tag, text = '', className = '') {{
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text) node.textContent = text;
      return node;
    }}

    function createButton(label, onClick, className = '') {{
      const button = document.createElement('button');
      if (className) button.className = className;
      button.textContent = label;
      button.addEventListener('click', onClick);
      return button;
    }}

    function modeHintText(mode) {{
      if (mode === 'checkpoint') {{
        return '检查点模式：写完初稿和 review 后暂停，等你确认再继续写入 canon。';
      }}
      if (mode === 'copilot') {{
        return '共驾模式：默认自动跑，但 review 不是 pass 时会停下来给你接管。';
      }}
      return '黑箱模式：默认一路自动执行，只在任务结束或严重失败时停下。';
    }}

    function updateModeHints() {{
      document.getElementById('saved_mode_hint').textContent = modeHintText(document.getElementById('saved_operation_mode').value);
      document.getElementById('mode_hint').textContent = modeHintText(document.getElementById('operation_mode').value);
    }}

    async function startGeneration() {{
      const premise = document.getElementById('premise').value.trim();
      if (!premise) {{
        setStatus('请先填写故事前提。');
        return;
      }}

      const payload = {{
        premise,
        genre: document.getElementById('genre').value,
        num_chapters: Number(document.getElementById('num_chapters').value || '3'),
        api_key: document.getElementById('api_key').value.trim() || null,
        base_url: document.getElementById('base_url').value.trim(),
        model: document.getElementById('model').value.trim(),
        operation_mode: document.getElementById('operation_mode').value,
        freeze_failed_candidates: document.getElementById('freeze_failed_candidates').checked,
      }};

      setStatus('正在提交任务...');
      const res = await fetch('/api/generate', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload),
      }});
      const data = await res.json();
      if (!res.ok) {{
        setStatus(data.detail || '提交失败');
        return;
      }}

      currentTaskId = data.task_id;
      setStatus(`任务已创建：${{data.task_id}}\\n${{data.message}}`);
      pollTask();
    }}

    async function loadSettings() {{
      const res = await fetch('/api/settings/llm');
      const data = await res.json();
      if (!res.ok) {{
        document.getElementById('settings_status').textContent = data.detail || '读取配置失败';
        return;
      }}
      document.getElementById('saved_api_key').value = '';
      document.getElementById('saved_base_url').value = data.base_url;
      document.getElementById('saved_model').value = data.model;
      document.getElementById('saved_operation_mode').value = data.operation_mode;
      document.getElementById('saved_freeze_failed_candidates').checked = Boolean(data.freeze_failed_candidates);
      document.getElementById('base_url').value = data.base_url;
      document.getElementById('model').value = data.model;
      document.getElementById('operation_mode').value = data.operation_mode;
      document.getElementById('freeze_failed_candidates').checked = Boolean(data.freeze_failed_candidates);
      document.getElementById('saved_badge').textContent = `已保存 API Key：${{data.has_api_key ? '是' : '否'}}`;
      document.getElementById('settings_status').textContent = data.message || '已读取默认配置';
      updateModeHints();
    }}

    async function saveSettings() {{
      const payload = {{
        api_key: document.getElementById('saved_api_key').value.trim(),
        base_url: document.getElementById('saved_base_url').value.trim(),
        model: document.getElementById('saved_model').value.trim(),
        operation_mode: document.getElementById('saved_operation_mode').value,
        freeze_failed_candidates: document.getElementById('saved_freeze_failed_candidates').checked,
      }};
      const res = await fetch('/api/settings/llm', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload),
      }});
      const data = await res.json();
      if (!res.ok) {{
        document.getElementById('settings_status').textContent = data.detail || '保存失败';
        return;
      }}
      document.getElementById('saved_api_key').value = '';
      document.getElementById('base_url').value = data.base_url;
      document.getElementById('model').value = data.model;
      document.getElementById('operation_mode').value = data.operation_mode;
      document.getElementById('freeze_failed_candidates').checked = Boolean(data.freeze_failed_candidates);
      document.getElementById('saved_badge').textContent = `已保存 API Key：${{data.has_api_key ? '是' : '否'}}`;
      document.getElementById('settings_status').textContent = data.message || '已保存默认配置';
      updateModeHints();
    }}

    async function pollTask() {{
      if (!currentTaskId) return;
      const res = await fetch(`/api/tasks/${{currentTaskId}}`);
      const data = await res.json();
      const lines = [
        `任务 ID：${{data.task_id}}`,
        `状态：${{data.status}}`,
      ];
      if (data.project_id) lines.push(`项目 ID：${{data.project_id}}`);
      if (Array.isArray(data.paused_chapters) && data.paused_chapters.length) lines.push(`暂停章节：${{data.paused_chapters.join(', ')}}`);
      if (Array.isArray(data.frozen_artifacts) && data.frozen_artifacts.length) lines.push(`冻结 artifact：${{data.frozen_artifacts.join(', ')}}`);
      if (data.status === 'needs_review') lines.push('提示：当前任务是按运行模式主动暂停，不是崩溃。');
      if (data.message) lines.push(`说明：${{data.message}}`);
      if (data.error) lines.push(`错误：${{data.error}}`);
      setStatus(lines.join('\\n'));

      if (data.status === 'running' || data.status === 'starting') {{
        window.setTimeout(pollTask, 1500);
        return;
      }}
      loadProjects();
    }}

    async function showReview(projectId, chapterNumber) {{
      const res = await fetch(`/api/projects/${{projectId}}/chapters/${{chapterNumber}}/review`);
      const data = await res.json();
      if (!res.ok) {{
        setStatus(data.detail || '读取 review 失败');
        return;
      }}
      const lines = [
        `项目：${{projectId}}`,
        `章节：第${{chapterNumber}}章《${{data.title}}》`,
        `状态：${{data.status}}`,
        `verdict：${{data.verdict}}`,
      ];
      if (Array.isArray(data.issues) && data.issues.length) {{
        lines.push('问题列表：');
        data.issues.forEach((issue, index) => {{
          lines.push(`${{index + 1}}. [${{issue.severity}}] ${{issue.description}}`);
        }});
      }} else {{
        lines.push('问题列表：无');
      }}
      window.alert(lines.join('\\n'));
    }}

    async function approveReview(projectId, chapterNumber, continueGeneration) {{
      setStatus(`正在处理第${{chapterNumber}}章 review...`);
      const res = await fetch(`/api/projects/${{projectId}}/chapters/${{chapterNumber}}/review/approve`, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ continue_generation: Boolean(continueGeneration) }}),
      }});
      const data = await res.json();
      if (!res.ok) {{
        setStatus(data.detail || '处理 review 失败');
        return;
      }}
      if (data.task_id) {{
        currentTaskId = data.task_id;
        setStatus(`已接受第${{chapterNumber}}章并继续执行。\\n任务 ID：${{data.task_id}}\\n${{data.message}}`);
        pollTask();
      }} else {{
        setStatus(data.message || `已接受第${{chapterNumber}}章。`);
      }}
      loadProjects();
    }}

    async function loadProjects() {{
      const list = document.getElementById('project_list');
      const res = await fetch('/api/projects');
      const data = await res.json();
      clearNode(list);
      if (!Array.isArray(data) || data.length === 0) {{
        const emptyCard = createNode('div', '', 'project-card');
        emptyCard.appendChild(createNode('p', '还没有项目。先在上面提交一个生成任务。'));
        list.appendChild(emptyCard);
        return;
      }}
      for (const item of data) {{
        const chapters = Array.isArray(item.chapters) ? item.chapters : [];
        const card = document.createElement('article');
        card.className = 'project-card';
        card.appendChild(createNode('h3', item.title || '未命名项目'));
        card.appendChild(createNode('p', item.premise || ''));
        card.appendChild(createNode('p', `题材：${{item.genre}}`, 'muted'));
        card.appendChild(createNode('p', `创建时间：${{item.created_at || ''}}`, 'muted'));
        if (item.latest_stage || item.pacing_verdict || item.current_time_label) {{
          const phaseMeta = [];
          if (item.latest_stage) phaseMeta.push(`阶段：${{item.latest_stage}}`);
          if (item.pacing_verdict) phaseMeta.push(`节奏：${{item.pacing_verdict}}`);
          if (item.current_time_label) phaseMeta.push(`时间：${{item.current_time_label}}`);
          card.appendChild(createNode('p', phaseMeta.join(' | '), 'muted'));
        }}
        if (item.active_arc_target_size) {{
          card.appendChild(createNode(
            'p',
            `Arc Envelope：${{item.active_arc_policy_tier}} | target ${{item.active_arc_target_size}}章 | range ${{item.active_arc_soft_min}}~${{item.active_arc_soft_max}} | band ${{item.active_arc_detailed_band_size}} | frozen ${{item.active_arc_frozen_zone_size}}`,
            'muted'
          ));
        }}
        if (item.pacing_summary) {{
          card.appendChild(createNode('p', `分析：${{item.pacing_summary}}`, 'muted'));
        }}
        if (item.world_pressure_level || item.world_pressure_summary) {{
          card.appendChild(createNode('p', `世界压力：${{item.world_pressure_level || 'steady'}} | ${{item.world_pressure_summary || ''}}`, 'muted'));
        }}
        if (item.last_replan_status && item.last_replan_reason) {{
          card.appendChild(createNode('p', `最近 replan(${{item.last_replan_status}})：${{item.last_replan_reason}}`, 'muted'));
        }}
        const chapterLabel = createNode('div', '章节状态', 'muted');
        chapterLabel.style.margin = '12px 0 4px';
        card.appendChild(chapterLabel);
        const chapterSummary = document.createElement('div');
        if (chapters.length) {{
          chapters.forEach((chapter) => {{
            const base = `第${{chapter.chapter_number}}章：${{chapter.status}}`;
            if (chapter.status === 'needs_review') {{
              const block = document.createElement('div');
              block.style.marginTop = '8px';
              block.style.paddingTop = '8px';
              block.style.borderTop = '1px dashed #d9c8ad';
              block.appendChild(createNode('div', base));
              const row = createNode('div', '', 'row');
              row.style.marginTop = '8px';
              row.appendChild(createButton('查看 review', () => showReview(item.id, chapter.chapter_number), 'secondary'));
              row.appendChild(createButton('接受', () => approveReview(item.id, chapter.chapter_number, false), 'secondary'));
              row.appendChild(createButton('接受并继续', () => approveReview(item.id, chapter.chapter_number, true), 'secondary'));
              block.appendChild(row);
              chapterSummary.appendChild(block);
              return;
            }}
            const line = createNode('div', base);
            line.style.marginTop = '6px';
            chapterSummary.appendChild(line);
          }});
        }} else {{
          chapterSummary.appendChild(createNode('div', '还没有章节。', 'muted'));
        }}
        card.appendChild(chapterSummary);
        const projectLink = document.createElement('a');
        projectLink.className = 'button secondary';
        projectLink.href = `/api/projects/${{item.id}}`;
        projectLink.target = '_blank';
        projectLink.textContent = '查看项目 JSON';
        card.appendChild(projectLink);
        list.appendChild(card);
      }}
    }}

    document.getElementById('saved_operation_mode').addEventListener('change', updateModeHints);
    document.getElementById('operation_mode').addEventListener('change', updateModeHints);
    updateModeHints();
    loadSettings();
    loadProjects();
  </script>
</body>
</html>
        """
    )


@app.get("/publishers", response_class=HTMLResponse)
def publishers_page():
    backend_ready = (
        _publisher_manager.backend_ready_payload()
        if _publisher_manager is not None
        else {"extension_api_key_configured": False}
    )
    extension_install_path = "browser_extension/forwin-publisher"
    return HTMLResponse(
        f"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>平台发布管理</title>
  <style>
    :root {{ --bg:#f4efe6; --card:#fffaf1; --ink:#1f2a24; --accent:#b64b2a; --line:#e7d7bf; --muted:#755b3d; --ok:#226b37; --warn:#a25920; }}
    body {{ margin:0; font-family:"Noto Serif SC","Source Han Serif SC",serif; background:
      radial-gradient(circle at top left,#fff6df 0,transparent 35%),
      linear-gradient(135deg,#f4efe6,#efe2cf); color:var(--ink); }}
    .wrap {{ max-width:1100px; margin:0 auto; padding:40px 20px 80px; }}
    h1 {{ margin:0 0 8px; font-size:40px; }}
    p {{ line-height:1.6; }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:18px; margin-top:28px; }}
    .card {{ background:var(--card); border:1px solid var(--line); border-radius:20px; padding:22px; box-shadow:0 18px 50px rgba(73,52,28,.08); }}
    .actions {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:14px; }}
    button {{ border:0; background:var(--accent); color:#fff; border-radius:999px; padding:10px 16px; cursor:pointer; }}
    button.secondary {{ background:#e8d9c1; color:#5b4630; }}
    .muted {{ color:var(--muted); }}
    input, textarea, select {{ width:100%; margin-top:8px; margin-bottom:12px; border:1px solid var(--line); border-radius:12px; padding:10px 12px; background:#fff; font:inherit; box-sizing:border-box; }}
    textarea {{ min-height:180px; resize:vertical; }}
    .status {{ margin-top:10px; font-size:14px; color:var(--muted); white-space:pre-wrap; }}
    .ok {{ color:var(--ok); }}
    .warn {{ color:var(--warn); }}
    .summary-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:16px; margin-top:18px; }}
    code {{ font-family:"SFMono-Regular","Consolas",monospace; font-size:13px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>平台发布管理</h1>
    <p>ForWin 现在把登录、上传和后续评论采集都交给浏览器扩展执行。后端只负责保存任务、接收扩展心跳和持久化结果，不再在服务器里偷偷跑浏览器。</p>
    <div class="summary-grid">
      <div class="card">
        <h2 style="margin-top:0;">扩展状态</h2>
        <div id="extension_summary" class="status">正在检测浏览器扩展...</div>
        <div class="actions">
          <button class="secondary" onclick="openExtensionOptions()">打开扩展设置</button>
          <a href="/api/publishers/extension-package" style="display:inline-flex;align-items:center;text-decoration:none;border:0;background:#e8d9c1;color:#5b4630;border-radius:999px;padding:10px 16px;">下载扩展包</a>
        </div>
      </div>
      <div class="card">
        <h2 style="margin-top:0;">安装提示</h2>
        <p class="muted">如果你是用 macOS 浏览器访问这台 Linux 后端，请先把扩展包下载到你的 Mac，再解压后用开发者模式加载。不要直接把 Linux 服务器路径当成本机路径使用。</p>
        <p><code>{extension_install_path}</code></p>
        <p class="muted">扩展里要填当前 ForWin 后端 URL 和共享 API Key。页面只通过扩展桥接登录，不再回退到旧的服务端扫码逻辑。首次安装时，请先在浏览器扩展管理页手动打开扩展选项；页面里的“打开扩展设置”按钮要等扩展已经完成首次配置后才会生效。</p>
      </div>
    </div>
    <div id="platforms" class="grid"></div>
    <div class="card" style="margin-top:22px;">
      <h2>上传任务</h2>
      <label>平台</label>
      <select id="platform"></select>
      <label>作品名</label>
      <input id="book_name" placeholder="输入平台里的作品名">
      <label>章节标题</label>
      <input id="chapter_title" placeholder="例如：第一百二十三章 风雪夜归人">
      <label>正文</label>
      <textarea id="body" placeholder="粘贴章节正文"></textarea>
      <label>可选上传页 URL</label>
      <input id="upload_url" placeholder="如果你知道某个平台的具体章节编辑页，可以填这里">
      <div class="actions">
        <button onclick="upload(true)">直接发布</button>
        <button class="secondary" onclick="upload(false)">保存草稿</button>
      </div>
      <div id="upload_status" class="status"></div>
    </div>
  </div>
  <script>
    const EXTENSION_BRIDGE_CHANNEL = 'forwin-publisher-extension';
    const BACKEND_EXTENSION_KEY_READY = {json.dumps(bool(backend_ready.get("extension_api_key_configured")))};
    const EXTENSION_INSTALL_PATH = {json.dumps(extension_install_path, ensure_ascii=False)};
    const pendingBridgeRequests = new Map();
    let uploadPollTimer = null;

    function bridgeId() {{
      if (window.crypto && typeof window.crypto.randomUUID === 'function') {{
        return window.crypto.randomUUID();
      }}
      return `forwin-${{Date.now()}}-${{Math.random().toString(16).slice(2)}}`;
    }}

    function clearNode(node) {{
      if (!node) return;
      node.replaceChildren();
    }}

    function createNode(tag, text = '', className = '') {{
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text) node.textContent = text;
      return node;
    }}

    function createButton(label, onClick, className = '') {{
      const button = document.createElement('button');
      if (className) button.className = className;
      button.textContent = label;
      button.addEventListener('click', onClick);
      return button;
    }}

    function normalizeOrigin(value) {{
      try {{
        return new URL(value).origin;
      }} catch (_error) {{
        return '';
      }}
    }}

    function showManualExtensionSetup(extraMessage = '') {{
      const currentOrigin = window.location.origin;
      const lines = [
        '首次安装请手动完成这几步：',
        '1. 点击页面里的“下载扩展包”，把 zip 下载到你当前这台浏览器所在的电脑。',
        '2. 解压 zip，得到 forwin-publisher 文件夹。',
        '3. 打开 chrome://extensions 或 edge://extensions。',
        '4. 开启“开发者模式”，点击“加载已解压的扩展程序”，选择解压后的 forwin-publisher 文件夹。',
        '5. 打开扩展卡片的“详情”，进入“扩展程序选项”。',
        `6. 在扩展里把 ForWin Backend URL 填成：${{currentOrigin}}`,
        '7. Extension API Key 填你服务器 .env 里的 FORWIN_PUBLISHER_EXTENSION_API_KEY。',
        '8. 保存后刷新当前 /publishers 页面。',
      ];
      if (extraMessage) {{
        lines.push('', `补充信息：${{extraMessage}}`);
      }}
      window.alert(lines.join('\\n'));
    }}

    function setPlatformStatus(platform, text, kind = 'warn') {{
      const el = document.getElementById(`status_${{platform}}`);
      if (!el) return;
      el.textContent = text;
      el.className = `status ${{kind}}`;
    }}

    function renderExtensionSummary(details = null, error = '') {{
      const el = document.getElementById('extension_summary');
      if (!BACKEND_EXTENSION_KEY_READY) {{
        el.textContent = '后端还没有配置扩展 API Key。请先设置 FORWIN_PUBLISHER_EXTENSION_API_KEY，再让扩展连接这个实例。';
        el.className = 'status warn';
        return;
      }}
      if (!details) {{
        el.textContent = `未检测到浏览器扩展。请确认你已经用开发者模式加载：\\n${{EXTENSION_INSTALL_PATH}}\\n并且在扩展设置里把当前 ForWin 地址加入桥接。${{error ? `\\n${{error}}` : ''}}`;
        el.className = 'status warn';
        return;
      }}
      const configuredOrigin = normalizeOrigin(details.backendBaseUrl);
      const currentOrigin = window.location.origin;
      const sameBackend = configuredOrigin === currentOrigin;
      el.textContent = [
        '浏览器扩展已连接',
        `客户端：${{details.browserName || 'unknown'}} / ${{details.extensionVersion || 'dev'}}`,
        `扩展配置的后端：${{details.backendBaseUrl || '未配置'}}`,
        `当前页面后端：${{currentOrigin}}`,
        sameBackend ? '后端地址匹配，可以直接登录和上传。' : '扩展里的后端地址和当前页面不一致，请先去扩展设置里改成当前地址。',
      ].join('\\n');
      el.className = `status ${{sameBackend ? 'ok' : 'warn'}}`;
    }}

    function bridgeRequest(action, payload = {{}}, timeoutMs = 1800) {{
      return new Promise((resolve, reject) => {{
        const correlationId = bridgeId();
        const timer = window.setTimeout(() => {{
          pendingBridgeRequests.delete(correlationId);
          reject(new Error('浏览器扩展未响应。'));
        }}, timeoutMs);
        pendingBridgeRequests.set(correlationId, {{ resolve, reject, timer }});
        window.postMessage(
          {{
            channel: EXTENSION_BRIDGE_CHANNEL,
            direction: 'page-to-extension',
            kind: 'request',
            correlationId,
            action,
            payload,
          }},
          window.location.origin,
        );
      }});
    }}

    async function pingExtension() {{
      try {{
        const payload = await bridgeRequest('ping');
        renderExtensionSummary(payload);
        return payload;
      }} catch (error) {{
        renderExtensionSummary(null, error.message || String(error));
        return null;
      }}
    }}

    async function loadPlatforms() {{
      const res = await fetch('/api/publishers/platforms');
      const data = await res.json();
      const grid = document.getElementById('platforms');
      const select = document.getElementById('platform');
      clearNode(grid);
      clearNode(select);
      data.forEach((item) => {{
        const option = document.createElement('option');
        option.value = item.platform_id;
        option.textContent = item.display_name;
        select.appendChild(option);

        const heartbeat = item.last_heartbeat_at ? `最近心跳：${{item.last_heartbeat_at}}` : '还没有收到扩展心跳';
        const online = item.extension_online ? '扩展在线' : '扩展离线';
        const card = document.createElement('div');
        card.className = 'card';
        card.appendChild(createNode('h2', item.display_name));
        const loginText = document.createElement('p');
        loginText.appendChild(document.createTextNode('登录入口：'));
        const loginLink = document.createElement('a');
        loginLink.href = item.login_url;
        loginLink.target = '_blank';
        loginLink.rel = 'noreferrer';
        loginLink.textContent = item.login_url;
        loginText.appendChild(loginLink);
        card.appendChild(loginText);
        card.appendChild(createNode('p', `支持登录：${{item.supported_login_methods.join(' / ') || 'scan'}}`, 'muted'));
        card.appendChild(createNode('p', `支持动作：${{item.supported_actions.join(' / ')}}`, 'muted'));
        const actions = createNode('div', '', 'actions');
        actions.appendChild(createButton(item.connected ? '重新连接' : '连接平台', () => connectPlatform(item.platform_id)));
        actions.appendChild(createButton('仅打开官网', () => openOfficialSite(item.login_url, item.platform_id), 'secondary'));
        card.appendChild(actions);
        const status = createNode(
          'div',
          `${{item.connected ? '已连接' : '未连接'}} | ${{online}}\\n${{heartbeat}}`,
          `status ${{item.connected ? 'ok' : 'warn'}}`,
        );
        status.id = `status_${{item.platform_id}}`;
        card.appendChild(status);
        if (item.last_error) {{
          card.appendChild(createNode('p', `最近错误：${{item.last_error}}`, 'status warn'));
        }}
        grid.appendChild(card);
      }});
    }}

    function openOfficialSite(url, platform) {{
      window.open(url, '_blank', 'noopener,noreferrer');
      setPlatformStatus(platform, '已在浏览器里打开平台官网，但这一步不会自动通知后端登录成功。请优先用“连接平台”让扩展接管整个登录流程。', 'warn');
    }}

    async function connectPlatform(platform) {{
      if (!BACKEND_EXTENSION_KEY_READY) {{
        setPlatformStatus(platform, '后端未配置扩展 API Key，暂时无法接收扩展心跳和任务回写。', 'warn');
        return;
      }}
      setPlatformStatus(platform, '正在请求扩展打开登录弹窗...', 'warn');
      try {{
        const response = await bridgeRequest('open-login', {{ platform }}, 2500);
        setPlatformStatus(platform, response.message || '登录弹窗已打开，请在弹窗里完成扫码。', 'warn');
      }} catch (error) {{
        setPlatformStatus(platform, error.message || String(error), 'warn');
      }}
    }}

    function stopUploadPolling() {{
      if (uploadPollTimer) {{
        window.clearTimeout(uploadPollTimer);
        uploadPollTimer = null;
      }}
    }}

    function renderUploadJob(data) {{
      const lines = [
        `任务状态：${{data.status}}`,
        `平台：${{data.display_name}}`,
        `作品：${{data.book_name}}`,
        `章节：${{data.chapter_title}}`,
        data.message ? `说明：${{data.message}}` : '',
        data.error ? `错误：${{data.error}}` : '',
        data.current_url ? `当前页面：${{data.current_url}}` : '',
        data.started_at ? `开始时间：${{data.started_at}}` : '',
        data.finished_at ? `结束时间：${{data.finished_at}}` : '',
      ].filter(Boolean);
      const el = document.getElementById('upload_status');
      el.textContent = lines.join('\\n');
      el.className = `status ${{data.status === 'succeeded' ? 'ok' : (data.status === 'failed' ? 'warn' : '')}}`;
    }}

    async function pollUploadJob(jobId, immediate = false) {{
      stopUploadPolling();
      const run = async () => {{
        const res = await fetch(`/api/publishers/upload-jobs/${{jobId}}`);
        const data = await res.json();
        renderUploadJob(data);
        if (data.status === 'succeeded' || data.status === 'failed') {{
          await loadPlatforms();
          return;
        }}
        uploadPollTimer = window.setTimeout(run, 1500);
      }};
      if (immediate) {{
        await run();
      }} else {{
        uploadPollTimer = window.setTimeout(run, 0);
      }}
    }}

    async function upload(publish) {{
      const payload = {{
        platform: document.getElementById('platform').value,
        book_name: document.getElementById('book_name').value,
        chapter_title: document.getElementById('chapter_title').value,
        body: document.getElementById('body').value,
        upload_url: document.getElementById('upload_url').value || null,
        publish,
      }};
      const res = await fetch('/api/publishers/upload-jobs', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload),
      }});
      const data = await res.json();
      if (!res.ok) {{
        document.getElementById('upload_status').textContent = data.detail || '创建上传任务失败';
        document.getElementById('upload_status').className = 'status warn';
        return;
      }}
      renderUploadJob(data);
      try {{
        const response = await bridgeRequest('execute-upload-job', {{ jobId: data.job_id }}, 2500);
        document.getElementById('upload_status').textContent += `\\n${{response.message || '浏览器扩展已接管任务。'}}`;
      }} catch (error) {{
        document.getElementById('upload_status').textContent += `\\n${{error.message || String(error)}}`;
      }}
      await pollUploadJob(data.job_id, true);
    }}

    window.addEventListener('message', (event) => {{
      if (event.source !== window) return;
      if (event.origin !== window.location.origin) return;
      const data = event.data;
      if (!data || typeof data !== 'object') return;
      if (data.channel !== EXTENSION_BRIDGE_CHANNEL) return;
      if (data.direction !== 'extension-to-page') return;
      if (data.kind === 'response' && data.correlationId) {{
        const pending = pendingBridgeRequests.get(data.correlationId);
        if (!pending) return;
        window.clearTimeout(pending.timer);
        pendingBridgeRequests.delete(data.correlationId);
        if (data.ok) {{
          pending.resolve(data.payload);
        }} else {{
          pending.reject(new Error(data.error || '浏览器扩展返回了失败响应。'));
        }}
        return;
      }}
      if (data.kind !== 'event') return;
      if (data.event === 'login-status' && data.payload && data.payload.platform) {{
        setPlatformStatus(
          data.payload.platform,
          data.payload.message || (data.payload.connected ? '登录成功，后端状态正在刷新。' : '登录状态有更新。'),
          data.payload.connected ? 'ok' : 'warn',
        );
        loadPlatforms();
        return;
      }}
      if (data.event === 'upload-status' && data.payload && data.payload.jobId) {{
        pollUploadJob(data.payload.jobId, true);
      }}
    }});

    async function openExtensionOptions() {{
      try {{
        await bridgeRequest('open-options', {{}}, 1500);
      }} catch (error) {{
        renderExtensionSummary(null, error.message || String(error));
        showManualExtensionSetup(error.message || String(error));
      }}
    }}

    async function boot() {{
      await loadPlatforms();
      await pingExtension();
      window.setInterval(loadPlatforms, 5000);
    }}

    boot();
  </script>
</body>
</html>
        """
    )


@app.post("/api/generate", response_model=TaskResponse)
def generate(req: GenerateRequest):
    if not _config:
        raise HTTPException(503, "服务尚未初始化")

    runtime_config = _build_runtime_config(req)
    if not runtime_config.minimax_api_key:
        raise HTTPException(400, "MINIMAX_API_KEY 未设置。请在页面填写 API Key，或通过环境变量配置。")

    task_id = uuid.uuid4().hex[:12]
    task_record = _create_task_record()
    with _tasks_lock:
        _tasks[task_id] = task_record

    t = threading.Thread(
        target=_run_generation_with_config,
        args=(task_id, req.premise, req.genre, req.num_chapters, runtime_config),
        daemon=True,
    )
    t.start()

    return TaskResponse(
        task_id=task_id,
        status="running",
        message=f"开始生成 {req.num_chapters} 章，请通过 /api/tasks/{task_id} 查询进度",
    )


@app.get("/api/settings/llm", response_model=LLMSettingsResponse)
def get_llm_settings():
    if not _runtime_settings:
        raise HTTPException(503, "服务尚未初始化")
    payload = _runtime_settings.get()
    return LLMSettingsResponse(
        has_api_key=bool(payload["api_key"]),
        base_url=str(payload["base_url"]),
        model=str(payload["model"]),
        operation_mode=str(payload["operation_mode"]),
        freeze_failed_candidates=bool(payload["freeze_failed_candidates"]),
        message="已读取当前默认模型配置",
    )


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
    return LLMSettingsResponse(
        has_api_key=bool(payload["api_key"]),
        base_url=str(payload["base_url"]),
        model=str(payload["model"]),
        operation_mode=str(payload["operation_mode"]),
        freeze_failed_candidates=bool(payload["freeze_failed_candidates"]),
        message="默认模型配置已保存",
    )


@app.get("/api/tasks/{task_id}", response_model=TaskResponse)
def get_task(task_id: str):
    _prune_tasks()
    with _tasks_lock:
        task_row = _tasks.get(task_id)
        task = dict(task_row) if task_row is not None else None
    if task is None:
        raise HTTPException(404, "任务不存在")
    return TaskResponse(
        task_id=task_id,
        status=task["status"],
        project_id=task.get("project_id"),
        error=task.get("error"),
        message=task.get("message", ""),
        failed_chapters=task.get("failed_chapters", []),
        paused_chapters=task.get("paused_chapters", []),
        frozen_artifacts=task.get("frozen_artifacts", []),
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


def _run_backend_upload_job(job_id: str) -> None:
    from forwin.publishers.server_uploader import ServerPublisherUploader

    if not _publisher_manager:
        return
    try:
        job = _publisher_manager.get_upload_job(job_id)
        session_payload = _publisher_manager.get_browser_session(job["platform"])
        if not session_payload:
            return
        _publisher_manager.update_upload_job_result(
            job_id=job_id,
            client_id=session_payload.get("client_id", ""),
            status="running",
            message="后端正在使用已同步会话执行上传。",
            current_url="",
            error="",
            result_payload={"executor": "server", "phase": "starting"},
        )
        uploader = ServerPublisherUploader()
        result = uploader.upload(
            platform=job["platform"],
            cookies=session_payload.get("cookies", []),
            book_name=job["book_name"],
            chapter_title=job["chapter_title"],
            body=job["body"],
            publish=job["publish"],
            upload_url=job.get("upload_url"),
        )
        _publisher_manager.mark_browser_session_result(
            platform=job["platform"],
            last_error=result.error,
            verified=result.ok,
        )
        _publisher_manager.update_upload_job_result(
            job_id=job_id,
            client_id=session_payload.get("client_id", ""),
            status="succeeded" if result.ok else "failed",
            message=result.message,
            current_url=result.current_url,
            error=result.error,
            result_payload=result.result_payload or {"executor": "server"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Backend upload failed for job %s", job_id)
        try:
            if _publisher_manager:
                payload = _publisher_manager.get_upload_job(job_id)
                _publisher_manager.mark_browser_session_result(
                    platform=payload["platform"],
                    last_error=str(exc),
                    verified=False,
                )
                _publisher_manager.update_upload_job_result(
                    job_id=job_id,
                    client_id="",
                    status="failed",
                    message="后端上传失败。",
                    current_url="",
                    error=str(exc),
                    result_payload={"executor": "server", "phase": "exception"},
                )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to persist backend upload failure for job %s", job_id)
    finally:
        with _backend_upload_lock:
            _backend_upload_inflight.discard(job_id)


def _start_backend_upload_thread(job_id: str) -> None:
    with _backend_upload_lock:
        if job_id in _backend_upload_inflight:
            return
        _backend_upload_inflight.add(job_id)
    thread = threading.Thread(
        target=_run_backend_upload_job,
        args=(job_id,),
        daemon=True,
    )
    thread.start()


def _maybe_start_backend_upload(job_id: str, platform: str) -> dict[str, Any] | None:
    if not _publisher_manager or not _publisher_manager.has_browser_session(platform):
        return None
    session_payload = _publisher_manager.get_browser_session(platform)
    if not session_payload:
        return None
    claimed = _publisher_manager.claim_upload_job_for_server(
        job_id=job_id,
        client_id=session_payload.get("client_id", ""),
    )
    if not claimed:
        return None
    _start_backend_upload_thread(job_id)
    return claimed


def _start_pending_backend_uploads(platform: str) -> None:
    if not _publisher_manager or not _publisher_manager.has_browser_session(platform):
        return
    session_payload = _publisher_manager.get_browser_session(platform)
    if not session_payload:
        return
    claimed_jobs = _publisher_manager.claim_pending_upload_jobs_for_server(
        platform=platform,
        client_id=session_payload.get("client_id", ""),
    )
    for job in claimed_jobs:
        _start_backend_upload_thread(job["job_id"])


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
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    claimed = _maybe_start_backend_upload(payload["job_id"], req.platform)
    if claimed:
        payload = claimed
    return PublisherUploadJobResponse(**payload)


@app.get("/api/publishers/upload-jobs/{job_id}", response_model=PublisherUploadJobResponse)
def get_publisher_upload_job(job_id: str):
    try:
        payload = _publisher_manager.get_upload_job(job_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return PublisherUploadJobResponse(**payload)


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
    _start_pending_backend_uploads(req.platform)
    return ExtensionSessionSyncResponse(**payload)


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


def _latest_stage_analysis(session, project_id: str) -> ProjectStageAnalysis | None:
    return session.execute(
        select(ProjectStageAnalysis)
        .where(ProjectStageAnalysis.project_id == project_id)
        .order_by(
            ProjectStageAnalysis.chapter_number.desc(),
            ProjectStageAnalysis.created_at.desc(),
        )
        .limit(1)
    ).scalar_one_or_none()


def _recent_replan_events(
    session,
    project_id: str,
    limit: int = 3,
) -> list[ProjectReplanEvent]:
    return list(
        session.execute(
            select(ProjectReplanEvent)
            .where(ProjectReplanEvent.project_id == project_id)
            .order_by(
                ProjectReplanEvent.trigger_chapter.desc(),
                ProjectReplanEvent.created_at.desc(),
            )
            .limit(limit)
        ).scalars().all()
    )


def _latest_world_turn(session, project_id: str) -> WorldSimulationTurn | None:
    return session.execute(
        select(WorldSimulationTurn)
        .where(WorldSimulationTurn.project_id == project_id)
        .order_by(
            WorldSimulationTurn.chapter_number.desc(),
            WorldSimulationTurn.created_at.desc(),
        )
        .limit(1)
    ).scalar_one_or_none()


def _latest_active_arc_envelope(session, project_id: str):
    return load_latest_active_arc_envelope_by_project(session, [project_id]).get(project_id)


def _latest_provisional_band_execution(
    session,
    project_id: str,
    arc_id: str | None = None,
) -> ProvisionalBandExecution | None:
    stmt = select(ProvisionalBandExecution).where(
        ProvisionalBandExecution.project_id == project_id
    )
    if arc_id:
        stmt = stmt.where(ProvisionalBandExecution.arc_id == arc_id)
    return session.execute(
        stmt.order_by(ProvisionalBandExecution.created_at.desc()).limit(1)
    ).scalar_one_or_none()


def _provisional_chapter_ledgers(
    session,
    *,
    project_id: str,
    arc_id: str,
    band_id: str,
) -> list[ProvisionalChapterLedger]:
    return list(
        session.execute(
            select(ProvisionalChapterLedger)
            .where(
                ProvisionalChapterLedger.project_id == project_id,
                ProvisionalChapterLedger.arc_id == arc_id,
                ProvisionalChapterLedger.band_id == band_id,
            )
            .order_by(
                ProvisionalChapterLedger.chapter_number.asc(),
                ProvisionalChapterLedger.created_at.asc(),
            )
        ).scalars().all()
    )


def _recent_npc_intents(
    session,
    project_id: str,
    limit: int = 5,
) -> list[NPCIntentSnapshot]:
    return list(
        session.execute(
            select(NPCIntentSnapshot)
            .where(NPCIntentSnapshot.project_id == project_id)
            .order_by(
                NPCIntentSnapshot.chapter_number.desc(),
                NPCIntentSnapshot.urgency.desc(),
                NPCIntentSnapshot.created_at.desc(),
            )
            .limit(limit)
        ).scalars().all()
    )


def _latest_draft_map(session, chapter_plan_ids: list[str]) -> dict[str, ChapterDraft]:
    return load_latest_drafts_by_plan_id(session, chapter_plan_ids)


@app.get("/api/projects", response_model=list[ProjectSummary])
def list_projects():
    session = _get_session()
    try:
        projects = session.execute(
            select(Project).order_by(Project.created_at.desc())
        ).scalars().all()
        project_ids = [project.id for project in projects]
        plans = session.execute(
            select(ChapterPlan)
            .where(ChapterPlan.project_id.in_(project_ids))
            .order_by(ChapterPlan.project_id, ChapterPlan.chapter_number)
        ).scalars().all() if project_ids else []
        draft_map = _latest_draft_map(session, [plan.id for plan in plans])
        latest_stage_map = load_latest_stage_analysis_by_project(session, project_ids)
        last_replan_map = load_latest_replan_event_by_project(session, project_ids)
        latest_world_map = load_latest_world_turn_by_project(session, project_ids)
        latest_arc_envelope_map = load_latest_active_arc_envelope_by_project(session, project_ids)
        chapters_by_project: dict[str, list[dict[str, object]]] = {}
        for plan in plans:
            draft = draft_map.get(plan.id)
            chapters_by_project.setdefault(plan.project_id, []).append(
                {
                    "chapter_number": plan.chapter_number,
                    "title": plan.title,
                    "status": plan.status,
                    "char_count": draft.char_count if draft else 0,
                    "summary": draft.summary if draft else "",
                }
            )
        payload: list[ProjectSummary] = []
        for p in projects:
            latest_stage = latest_stage_map.get(p.id)
            last_replan = last_replan_map.get(p.id)
            latest_world = latest_world_map.get(p.id)
            latest_arc_envelope = latest_arc_envelope_map.get(p.id)
            payload.append(
                ProjectSummary(
                    id=p.id,
                    title=p.title,
                    genre=p.genre,
                    premise=p.premise[:100] + "..." if len(p.premise) > 100 else p.premise,
                    created_at=str(p.created_at),
                    latest_stage=latest_stage.stage_label if latest_stage else "",
                    pacing_verdict=latest_stage.pacing_verdict if latest_stage else "",
                    pacing_summary=latest_stage.pacing_summary if latest_stage else "",
                    last_replan_status=last_replan.status if last_replan else "",
                    last_replan_strategy=last_replan.strategy if last_replan else "",
                    last_replan_reason=last_replan.reason if last_replan else "",
                    current_time_label=latest_stage.timeline_label if latest_stage else "",
                    world_pressure_level=latest_world.pressure_level if latest_world else "",
                    world_pressure_summary=latest_world.pressure_summary if latest_world else "",
                    chapters=chapters_by_project.get(p.id, []),
                    active_arc_policy_tier=latest_arc_envelope.source_policy_tier if latest_arc_envelope else "",
                    active_arc_target_size=latest_arc_envelope.resolved_target_size if latest_arc_envelope else 0,
                    active_arc_soft_min=latest_arc_envelope.resolved_soft_min if latest_arc_envelope else 0,
                    active_arc_soft_max=latest_arc_envelope.resolved_soft_max if latest_arc_envelope else 0,
                    active_arc_detailed_band_size=latest_arc_envelope.detailed_band_size if latest_arc_envelope else 0,
                    active_arc_frozen_zone_size=latest_arc_envelope.frozen_zone_size if latest_arc_envelope else 0,
                    active_arc_confidence=latest_arc_envelope.current_confidence if latest_arc_envelope else 0.0,
                )
            )
        return payload
    finally:
        session.close()


@app.get("/api/projects/{project_id}", response_model=ProjectDetail)
def get_project(project_id: str):
    session = _get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")

        # Entities
        entities = session.execute(
            select(Entity).where(Entity.project_id == project_id, Entity.is_active == True)
        ).scalars().all()

        characters = [EntityInfo(id=e.id, kind=e.kind, name=e.name, description=e.description, importance=e.importance) for e in entities if e.kind == "character"]
        locations = [EntityInfo(id=e.id, kind=e.kind, name=e.name, description=e.description, importance=e.importance) for e in entities if e.kind == "location"]
        factions = [EntityInfo(id=e.id, kind=e.kind, name=e.name, description=e.description, importance=e.importance) for e in entities if e.kind == "faction"]

        # Threads
        threads = session.execute(
            select(PlotThread).where(PlotThread.project_id == project_id)
        ).scalars().all()
        thread_infos = [ThreadInfo(id=t.id, name=t.name, description=t.description, status=t.status, priority=t.priority) for t in threads]

        # Chapters
        plans = session.execute(
            select(ChapterPlan).where(ChapterPlan.project_id == project_id).order_by(ChapterPlan.chapter_number)
        ).scalars().all()
        draft_map = _latest_draft_map(session, [plan.id for plan in plans])

        chapter_infos = []
        for p in plans:
            draft = draft_map.get(p.id)
            chapter_infos.append(ChapterInfo(
                chapter_number=p.chapter_number,
                title=p.title,
                status=p.status,
                char_count=draft.char_count if draft else 0,
                summary=draft.summary if draft else "",
            ))

        latest_stage = _latest_stage_analysis(session, project_id)
        replan_events = _recent_replan_events(session, project_id, limit=5)
        latest_world = _latest_world_turn(session, project_id)
        latest_arc_envelope = _latest_active_arc_envelope(session, project_id)
        npc_intents = _recent_npc_intents(session, project_id, limit=6)

        return ProjectDetail(
            id=project.id,
            title=project.title,
            premise=project.premise,
            genre=project.genre,
            setting_summary=project.setting_summary,
            characters=characters,
            locations=locations,
            factions=factions,
            threads=thread_infos,
            chapters=chapter_infos,
            latest_stage=latest_stage.stage_label if latest_stage else "",
            progress_ratio=latest_stage.progress_ratio if latest_stage else 0.0,
            pacing_verdict=latest_stage.pacing_verdict if latest_stage else "",
            pacing_summary=latest_stage.pacing_summary if latest_stage else "",
            current_time_label=latest_stage.timeline_label if latest_stage else "",
            world_pressure_level=latest_world.pressure_level if latest_world else "",
            world_pressure_summary=latest_world.pressure_summary if latest_world else "",
            npc_intent_count=len(npc_intents),
            active_arc_policy_tier=latest_arc_envelope.source_policy_tier if latest_arc_envelope else "",
            active_arc_target_size=latest_arc_envelope.resolved_target_size if latest_arc_envelope else 0,
            active_arc_soft_min=latest_arc_envelope.resolved_soft_min if latest_arc_envelope else 0,
            active_arc_soft_max=latest_arc_envelope.resolved_soft_max if latest_arc_envelope else 0,
            active_arc_detailed_band_size=latest_arc_envelope.detailed_band_size if latest_arc_envelope else 0,
            active_arc_frozen_zone_size=latest_arc_envelope.frozen_zone_size if latest_arc_envelope else 0,
            active_arc_confidence=latest_arc_envelope.current_confidence if latest_arc_envelope else 0.0,
            recent_npc_intents=[
                {
                    "chapter_number": item.chapter_number,
                    "entity_name": item.entity_name,
                    "intent_kind": item.intent_kind,
                    "objective": item.objective,
                    "tactic": item.tactic,
                    "urgency": item.urgency,
                    "notes": item.notes,
                }
                for item in npc_intents
            ],
            recent_replans=[
                {
                    "trigger_chapter": item.trigger_chapter,
                    "risk_level": item.risk_level,
                    "strategy": item.strategy,
                    "status": item.status,
                    "reason": item.reason,
                    "cooldown_until_chapter": item.cooldown_until_chapter,
                    "created_at": str(item.created_at),
                }
                for item in replan_events
            ],
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

        latest = _latest_provisional_band_execution(session, project_id)
        if latest is None:
            raise HTTPException(404, "项目暂无 provisional 预演记录")

        ledgers = _provisional_chapter_ledgers(
            session,
            project_id=project_id,
            arc_id=latest.arc_id,
            band_id=latest.band_id,
        )
        chapter_numbers = []
        try:
            chapter_numbers = json.loads(latest.chapter_numbers_json or "[]") or []
        except (json.JSONDecodeError, TypeError):
            chapter_numbers = []

        def _load_json_list(raw: str) -> list[dict[str, Any]]:
            try:
                payload = json.loads(raw or "[]") or []
            except (json.JSONDecodeError, TypeError):
                return []
            return [item for item in payload if isinstance(item, dict)]

        def _load_json_dict(raw: str) -> dict[str, Any]:
            try:
                payload = json.loads(raw or "{}") or {}
            except (json.JSONDecodeError, TypeError):
                return {}
            return payload if isinstance(payload, dict) else {}

        return ProvisionalBandDetail(
            project_id=project_id,
            arc_id=latest.arc_id,
            band_id=latest.band_id,
            aggregate_verdict=latest.aggregate_verdict,
            preview_char_count=latest.preview_char_count,
            issue_count=latest.issue_count,
            failure_count=latest.failure_count,
            artifact_path=latest.artifact_path,
            chapter_numbers=[int(item) for item in chapter_numbers if isinstance(item, int)],
            created_at=str(latest.created_at),
            chapters=[
                ProvisionalChapterLedgerInfo(
                    chapter_number=row.chapter_number,
                    title=row.title,
                    summary=row.summary,
                    verdict=row.verdict,
                    char_count=row.char_count,
                    artifact_meta_path=row.artifact_meta_path,
                    draft_blob_path=row.draft_blob_path,
                    current_time_label=row.current_time_label,
                    projected_time_label=row.projected_time_label,
                    state_changes=_load_json_list(row.state_changes_json),
                    events=_load_json_list(row.events_json),
                    thread_beats=_load_json_list(row.thread_beats_json),
                    time_advance=_load_json_dict(row.time_advance_json),
                    issues=_load_json_list(row.issues_json),
                    error=row.error_text,
                    created_at=str(row.created_at),
                )
                for row in ledgers
            ],
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
        draft_map = _latest_draft_map(session, [plan.id for plan in plans])

        result = []
        for p in plans:
            draft = draft_map.get(p.id)
            result.append(ChapterInfo(
                chapter_number=p.chapter_number,
                title=p.title,
                status=p.status,
                char_count=draft.char_count if draft else 0,
                summary=draft.summary if draft else "",
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

    runtime_config = _build_saved_runtime_config()
    try:
        result = _orchestrator.accept_review(project_id, chapter_number)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc

    task_id = ""
    message = result["message"]
    if req.continue_generation:
        task_id = uuid.uuid4().hex[:12]
        task_record = _create_task_record(
            message=f"已接受第{chapter_number}章，准备继续后续章节。"
        )
        with _tasks_lock:
            _tasks[task_id] = task_record
        _update_task(
            task_id,
            project_id=project_id,
            frozen_artifacts=[result["frozen_artifact"]] if result["frozen_artifact"] else [],
        )
        thread = threading.Thread(
            target=_run_continue_project_with_config,
            args=(task_id, project_id, runtime_config),
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
        frozen_artifact=result["frozen_artifact"],
    )
