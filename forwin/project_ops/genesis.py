from __future__ import annotations

import inspect
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from forwin.api_project_payloads import build_project_detail, build_project_summaries, normalize_project_automation
from forwin.api_runtime import build_saved_runtime_config, copy_config
from forwin.candidate_drafts import CandidateDraftRepository
from forwin.api_schemas import (
    BookGenesisDetail,
    BookGenesisNameGenerateRequest,
    BookGenesisNameGenerateResponse,
    BookGenesisPatchRequest,
    BookGenesisRefineRequest,
    BookGenesisStageRunRequest,
    BulkDeleteResponse,
    CandidateDraftDetail,
    ChapterDetail,
    ChapterListResponse,
    ChapterReviewApproveRequest,
    ChapterReviewApproveResponse,
    ChapterReviewDetail,
    ChapterReviewRetryRequest,
    ChapterRewriteAttemptInfo,
    ChapterReviewIssueInfo,
    ChapterInfo,
    FinalGateDecisionInfo,
    LintSignalInfo,
    ProjectAutomationUpdateRequest,
    ProjectAutomationUpdateResponse,
    ProjectBulkDeleteRequest,
    ProjectChapterPublishRequest,
    ProjectContinueGenerationRequest,
    ProjectCreateRequest,
    ProjectCreateResponse,
    ProjectDeleteResponse,
    ProjectDetail,
    ProjectExtendGenerationRequest,
    ProjectSummary,
    PublisherUploadJobResponse,
    RepairVerificationInfo,
    StartWritingRequest,
    StartWritingResponse,
    TaskResponse,
)
from forwin.book_genesis import GENESIS_STAGE_ORDER, StaleGenesisRevisionError
from forwin.generation.continue_workset import (
    ContinueGenerationWorkset,
    build_continue_generation_workset,
)
from forwin.genesis_handoff import StartWritingCommand
from forwin.governance import (
    DecisionEventType,
    derive_chapter_task_contract,
    new_project_governance,
    plan_task_contract_to_json,
)
from forwin.map.genesis_adapter import build_subworld_map_specs_from_genesis
from forwin.map.models import MapNodeRow
from forwin.map.service import build_interconnections_from_genesis_atlas, create_or_update_book_map
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.genesis import PromptTrace
from forwin.observability.payloads import audit_payload
from forwin.models.phase import ChapterRewriteAttempt
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.task import GenerationTask
from forwin.protocol.experience import ChapterExperiencePlan
from forwin.protocol.review import normalize_repair_scope
from forwin.state.query_helpers import load_latest_drafts_by_plan_id, load_latest_rewrite_attempts_by_chapter
from forwin.state.updater import StateUpdater


_DEFAULT_CHAPTER_PAGE_LIMIT = 60
_MAX_CHAPTER_PAGE_LIMIT = 200
_GENERATION_TASK_TERMINAL_STATUSES = {
    "completed",
    "partial_failed",
    "failed",
    "needs_review",
    "cancelled",
    "paused",
}

from .common import *
from forwin.generation.run_target import resolve_generation_run_target


def get_project_genesis(
    project_id: str,
    *,
    get_session,
    build_genesis_service,
    close_genesis_service,
    require_genesis_project,
) -> BookGenesisDetail:
    session = get_session()
    genesis_service = None
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        require_genesis_project(project)
        genesis_service = build_genesis_service()
        return BookGenesisDetail.model_validate(
            genesis_service.build_detail(session=session, project=project)
        )
    finally:
        close_genesis_service(genesis_service)
        session.close()

def patch_project_genesis(
    project_id: str,
    req: BookGenesisPatchRequest,
    *,
    get_session,
    build_genesis_service,
    close_genesis_service,
    require_genesis_project,
    active_genesis_revision,
    genesis_patch_payload,
) -> BookGenesisDetail:
    session = get_session()
    genesis_service = None
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        require_genesis_project(project)
        revision = active_genesis_revision(session, project)
        if revision is None:
            raise HTTPException(409, "项目 Genesis revision 不存在")
        patch_payload = genesis_patch_payload(req)
        if not patch_payload:
            raise HTTPException(400, "没有可更新的 Genesis 字段")
        genesis_service = build_genesis_service()
        updater = StateUpdater(session)
        try:
            genesis_service.patch_pack(
                session=session,
                updater=updater,
                project=project,
                revision=revision,
                patch=patch_payload,
                reason=req.reason,
            )
        except StaleGenesisRevisionError as exc:
            raise HTTPException(409, str(exc)) from exc
        session.commit()
        session.refresh(project)
        return BookGenesisDetail.model_validate(
            genesis_service.build_detail(session=session, project=project)
        )
    finally:
        close_genesis_service(genesis_service)
        session.close()

def generate_project_genesis_stage(
    project_id: str,
    stage_key: str,
    req: BookGenesisStageRunRequest | None = None,
    *,
    get_session,
    build_genesis_service,
    close_genesis_service,
    require_genesis_project,
    active_genesis_revision,
) -> BookGenesisDetail:
    session = get_session()
    genesis_service = None
    try:
        normalized_stage = str(stage_key or "").strip()
        if normalized_stage not in GENESIS_STAGE_ORDER:
            raise HTTPException(404, "Genesis stage 不存在")
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        require_genesis_project(project)
        revision = active_genesis_revision(session, project)
        if revision is None:
            raise HTTPException(409, "项目 Genesis revision 不存在")
        genesis_service = build_genesis_service(
            model_profile_id=str((req.model_profile_id if req else "") or "").strip()
        )
        updater = StateUpdater(session)
        try:
            genesis_service.generate_stage(
                session=session,
                updater=updater,
                project=project,
                revision=revision,
                stage_key=normalized_stage,
            )
        except StaleGenesisRevisionError as exc:
            raise HTTPException(409, str(exc)) from exc
        session.commit()
        session.refresh(project)
        return BookGenesisDetail.model_validate(
            genesis_service.build_detail(session=session, project=project)
        )
    finally:
        close_genesis_service(genesis_service)
        session.close()

def lock_project_genesis_stage(
    project_id: str,
    stage_key: str,
    *,
    get_session,
    build_genesis_service,
    close_genesis_service,
    require_genesis_project,
    active_genesis_revision,
) -> BookGenesisDetail:
    session = get_session()
    genesis_service = None
    try:
        normalized_stage = str(stage_key or "").strip()
        if normalized_stage not in GENESIS_STAGE_ORDER:
            raise HTTPException(404, "Genesis stage 不存在")
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        require_genesis_project(project)
        revision = active_genesis_revision(session, project)
        if revision is None:
            raise HTTPException(409, "项目 Genesis revision 不存在")
        genesis_service = build_genesis_service()
        updater = StateUpdater(session)
        try:
            genesis_service.lock_stage(
                session=session,
                updater=updater,
                project=project,
                revision=revision,
                stage_key=normalized_stage,
            )
        except StaleGenesisRevisionError as exc:
            raise HTTPException(409, str(exc)) from exc
        session.commit()
        session.refresh(project)
        return BookGenesisDetail.model_validate(
            genesis_service.build_detail(session=session, project=project)
        )
    finally:
        close_genesis_service(genesis_service)
        session.close()

def rerun_project_genesis_stage(
    project_id: str,
    stage_key: str,
    req: BookGenesisStageRunRequest | None = None,
    *,
    get_session,
    build_genesis_service,
    close_genesis_service,
    require_genesis_project,
    active_genesis_revision,
) -> BookGenesisDetail:
    session = get_session()
    genesis_service = None
    try:
        normalized_stage = str(stage_key or "").strip()
        if normalized_stage not in GENESIS_STAGE_ORDER:
            raise HTTPException(404, "Genesis stage 不存在")
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        require_genesis_project(project)
        revision = active_genesis_revision(session, project)
        if revision is None:
            raise HTTPException(409, "项目 Genesis revision 不存在")
        genesis_service = build_genesis_service(
            model_profile_id=str((req.model_profile_id if req else "") or "").strip()
        )
        updater = StateUpdater(session)
        try:
            genesis_service.generate_stage(
                session=session,
                updater=updater,
                project=project,
                revision=revision,
                stage_key=normalized_stage,
                event_type=DecisionEventType.GENESIS_STAGE_RERUN,
            )
        except StaleGenesisRevisionError as exc:
            raise HTTPException(409, str(exc)) from exc
        session.commit()
        session.refresh(project)
        return BookGenesisDetail.model_validate(
            genesis_service.build_detail(session=session, project=project)
        )
    finally:
        close_genesis_service(genesis_service)
        session.close()

def refine_project_genesis_stage(
    project_id: str,
    stage_key: str,
    req: BookGenesisRefineRequest,
    *,
    get_session,
    build_genesis_service,
    close_genesis_service,
    require_genesis_project,
    active_genesis_revision,
) -> BookGenesisDetail:
    session = get_session()
    genesis_service = None
    try:
        normalized_stage = str(stage_key or "").strip()
        if normalized_stage not in GENESIS_STAGE_ORDER:
            raise HTTPException(404, "Genesis stage 不存在")
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        require_genesis_project(project)
        revision = active_genesis_revision(session, project)
        if revision is None:
            raise HTTPException(409, "项目 Genesis revision 不存在")
        genesis_service = build_genesis_service(
            model_profile_id=str(req.model_profile_id or "").strip()
        )
        updater = StateUpdater(session)
        try:
            genesis_service.refine_stage(
                session=session,
                updater=updater,
                project=project,
                revision=revision,
                stage_key=normalized_stage,
                instruction=req.instruction,
                target_path=req.target_path,
                reason=req.reason,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except StaleGenesisRevisionError as exc:
            raise HTTPException(409, str(exc)) from exc
        session.commit()
        session.refresh(project)
        return BookGenesisDetail.model_validate(
            genesis_service.build_detail(session=session, project=project)
        )
    finally:
        close_genesis_service(genesis_service)
        session.close()

def generate_project_genesis_name(
    project_id: str,
    req: BookGenesisNameGenerateRequest,
    *,
    get_session,
    build_genesis_service,
    close_genesis_service,
    require_genesis_project,
    active_genesis_revision,
) -> BookGenesisNameGenerateResponse:
    session = get_session()
    genesis_service = None
    try:
        normalized_stage = str(req.stage_key or "").strip()
        if normalized_stage not in GENESIS_STAGE_ORDER:
            raise HTTPException(404, "Genesis stage 不存在")
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        require_genesis_project(project)
        revision = active_genesis_revision(session, project)
        if revision is None:
            raise HTTPException(409, "项目 Genesis revision 不存在")
        genesis_service = build_genesis_service()
        try:
            payload = genesis_service.generate_name_suggestions(
                project=project,
                revision=revision,
                stage_key=normalized_stage,
                target_path=req.target_path,
                field_path=req.field_path,
                kind=req.kind,
                count=req.count,
                nonce=req.nonce,
                stage_payload_override=req.stage_payload_override,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return BookGenesisNameGenerateResponse.model_validate(payload)
    finally:
        close_genesis_service(genesis_service)
        session.close()

def start_project_writing(
    project_id: str,
    req: StartWritingRequest | None = None,
    *,
    get_session,
    config,
    saved_runtime_config_or_default,
    build_genesis_service,
    close_genesis_service,
    require_genesis_project,
    active_genesis_revision,
    project_has_active_generation_task,
    generation_task_conflict_message,
    create_continue_generation_task,
) -> StartWritingResponse:
    if not config:
        raise HTTPException(503, "服务尚未初始化")
    auto_continue = True if req is None or req.auto_continue is None else bool(req.auto_continue)
    run_until_chapter = req.run_until_chapter if req is not None else None
    max_chapters = req.max_chapters if req is not None else None
    runtime_config = saved_runtime_config_or_default()
    if not runtime_config.minimax_api_key and not bool(getattr(runtime_config, "codex_enabled", False)):
        raise HTTPException(400, "MINIMAX_API_KEY 未设置。请先配置模型，再启动写作。")
    session = get_session()
    genesis_service = None
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        require_genesis_project(project)
        if str(project.creation_status or "") != "genesis_ready":
            raise HTTPException(409, "Genesis 尚未完成锁定，不能启动写作。")
        if project_has_active_generation_task(project_id, session=session):
            raise HTTPException(409, generation_task_conflict_message(project_id))
        revision = active_genesis_revision(session, project)
        if revision is None:
            raise HTTPException(409, "Genesis revision 不存在。")
        genesis_service = build_genesis_service(runtime_config)
        updater = StateUpdater(session)
        try:
            handoff_result = genesis_service.handoff.start_writing(
                session=session,
                updater=updater,
                command=StartWritingCommand(
                    project_id=project.id,
                    actor_type="manual_ui",
                    runtime_config=runtime_config,
                ),
            )
        except ValueError as exc:
            failure_summary = str(exc) or "Genesis map_atlas 无法生成 BookMap。"
            if "map" in failure_summary.lower() or "地图" in failure_summary or "BookMap" in failure_summary:
                session.commit()
                raise HTTPException(409, f"地图生成失败，不能启动写作：{failure_summary}") from exc
            session.rollback()
            raise HTTPException(409, failure_summary) from exc
        try:
            requested_chapters = int(handoff_result.active_chapter_plan_count or 0)
            task_max_chapters = max_chapters
            task_run_until_chapter = run_until_chapter
            if max_chapters is not None or run_until_chapter is not None:
                try:
                    target = resolve_generation_run_target(
                        project,
                        next_chapter=1,
                        run_until_chapter=run_until_chapter,
                        max_chapters=max_chapters,
                    )
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
                requested_chapters = min(requested_chapters, target.effective_max_chapters)
                task_max_chapters = target.effective_max_chapters
                task_run_until_chapter = target.run_until_chapter
            task_id = call_task_factory_with_supported_kwargs(
                create_continue_generation_task,
                {
                    "project_id": project.id,
                    "runtime_config": runtime_config,
                    "requested_chapters": requested_chapters,
                    "max_chapters": task_max_chapters,
                    "auto_continue": auto_continue,
                    "run_until_chapter": task_run_until_chapter,
                    "title": project.title,
                    "subtitle": f"启动写作 · {project.genre}",
                    "message": "Genesis 完成，准备进入写作主链。",
                },
            )
        except Exception:
            session.rollback()
            raise
        session.commit()
        return StartWritingResponse(
            ok=True,
            project_id=project.id,
            creation_status=handoff_result.project_status,
            task_id=task_id,
            message="Genesis 已完成交接，开始写作。",
        )
    finally:
        close_genesis_service(genesis_service)
        session.close()


__all__ = ['get_project_genesis', 'patch_project_genesis', 'generate_project_genesis_stage', 'lock_project_genesis_stage', 'rerun_project_genesis_stage', 'refine_project_genesis_stage', 'generate_project_genesis_name', 'start_project_writing']
