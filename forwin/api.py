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
from sqlalchemy import select, func

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
    EntityInfo,
    ExtensionClaimUploadJobRequest,
    ExtensionClaimUploadJobResponse,
    ExtensionCommentsBatchRequest,
    ExtensionCommentsBatchResponse,
    ExtensionHeartbeatRequest,
    ExtensionHeartbeatResponse,
    ExtensionPlatformHeartbeat,
    ExtensionSessionSyncRequest,
    ExtensionSessionSyncResponse,
    GenerateRequest,
    LLMSettingsRequest,
    LLMSettingsResponse,
    ProjectArcSnapshotFields,
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
    ThreadInfo,
    UploadJobResultRequest,
)
from forwin.config import Config
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.project import Project, ChapterPlan
from forwin.models.entity import Entity
from forwin.models.event import CanonEvent
from forwin.models.publisher import PublisherCommentSyncJob, PublisherConnectionState, PublisherExtensionClient, PublisherRawComment, PublisherUploadJob
from forwin.models.thread import PlotThread
from forwin.models.draft import ChapterDraft, ChapterReview
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
    _config = _config.model_copy(update={"db_path": db_path})

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
    return HTMLResponse(
        render_home_page(
            has_api_key=bool(settings["api_key"]),
            base_url=str(settings["base_url"]),
            model=str(settings["model"]),
            operation_mode=str(settings["operation_mode"]),
            freeze_failed_candidates=bool(settings["freeze_failed_candidates"]),
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
    task_record = _create_task_record()
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
        ),
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
            target=run_continue_project_with_config,
            args=(task_id, project_id, runtime_config, _update_task, logger),
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
