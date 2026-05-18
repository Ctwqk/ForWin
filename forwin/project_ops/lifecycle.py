from __future__ import annotations

import inspect
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select

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
    DecisionEventInfo,
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
from forwin.models.governance import DecisionEvent
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


def list_projects(
    *,
    get_session,
    config,
    display_datetime,
) -> list[ProjectSummary]:
    session = get_session()
    try:
        projects = session.execute(
            select(Project).order_by(Project.created_at.desc())
        ).scalars().all()
        return build_project_summaries(
            session=session,
            projects=projects,
            display_datetime=display_datetime,
            review_interval_chapters=max(0, int(config.review_interval_chapters if config else 0)),
        )
    finally:
        session.close()

def create_project(
    req: ProjectCreateRequest,
    *,
    get_session,
    config,
    build_genesis_service,
    close_genesis_service,
    log_decision_event,
) -> ProjectCreateResponse:
    session = get_session()
    genesis_service = None
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
            default_operation_mode="blackbox",
            review_interval_chapters=config.review_interval_chapters if config is not None else 0,
        )
        updater = StateUpdater(session)
        project = updater.create_project(
            title=title,
            premise=premise,
            genre=str(req.genre or "").strip() or "玄幻",
            setting_summary=str(req.setting_summary or "").strip(),
            target_total_chapters=max(1, int(req.target_total_chapters or 1)),
            governance=governance,
            creation_status="creating",
            automation_json=json.dumps(
                automation.model_dump(mode="json"),
                ensure_ascii=False,
            ),
        )
        genesis_service = build_genesis_service()
        genesis_revision = genesis_service.create_initial_revision(
            session=session,
            updater=updater,
            project=project,
            brief_seed={
                "audience_hint": req.audience_hint,
                "core_emotion": req.core_emotion,
                "core_delight": req.core_delight,
                "inspiration_notes": req.inspiration_notes,
                "content_guardrails": req.content_guardrails,
            },
        )
        log_decision_event(
            session,
            project_id=project.id,
            event_family="business_event",
            event_type=DecisionEventType.PROJECT_CREATED,
            summary="项目已创建并启用默认治理策略。",
            payload={
                "governance": governance.model_dump(mode="json"),
                "creation_status": "creating",
            },
        )
        session.commit()
        session.refresh(project)
        return ProjectCreateResponse(
            ok=True,
            project_id=project.id,
            title=project.title,
            target_total_chapters=int(project.target_total_chapters or 3),
            creation_status=str(project.creation_status or "creating"),
            active_genesis_revision_id=str(genesis_revision.id or ""),
            workspace_url=f"/?workspace=genesis&project_id={project.id}",
            message=f"书本《{project.title}》已创建，已进入 Genesis 工作台。",
        )
    finally:
        close_genesis_service(genesis_service)
        session.close()

def delete_project(
    project_id: str,
    *,
    get_session,
    config,
    delete_project_impl,
    project_delete_blockers,
    project_delete_conflict_message,
    operation_id: str = "",
    test_run_id: str = "",
) -> ProjectDeleteResponse:
    normalized_operation_id = _new_operation_id(operation_id)
    session = get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        blockers = project_delete_blockers(project_id, session=session)
        if blockers:
            raise HTTPException(409, project_delete_conflict_message(blockers))
        _export_project_audit_bundle(
            session=session,
            config=config,
            project=project,
            operation_id=normalized_operation_id,
            test_run_id=test_run_id,
        )
        delete_project_impl(session, project_id)
        session.commit()
        return ProjectDeleteResponse(
            ok=True,
            project_id=project_id,
            message=f"项目《{project.title}》已删除。",
            operation_id=normalized_operation_id,
        )
    finally:
        session.close()

def bulk_delete_projects(
    req: ProjectBulkDeleteRequest,
    *,
    get_session,
    config,
    delete_project_impl,
    project_delete_blockers,
    operation_id: str = "",
    test_run_id: str = "",
) -> BulkDeleteResponse:
    normalized_operation_id = _new_operation_id(operation_id)
    session = get_session()
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
            if project_delete_blockers(normalized, session=session):
                skipped_ids.append(normalized)
                continue
            _export_project_audit_bundle(
                session=session,
                config=config,
                project=project,
                operation_id=normalized_operation_id,
                test_run_id=test_run_id,
            )
            delete_project_impl(session, normalized)
            deleted_ids.append(normalized)
        session.commit()
        return BulkDeleteResponse(
            ok=True,
            deleted_count=len(deleted_ids),
            skipped_count=len(skipped_ids),
            deleted_ids=deleted_ids,
            skipped_ids=skipped_ids,
            message=f"已删除 {len(deleted_ids)} 本书，跳过 {len(skipped_ids)} 本。",
            operation_id=normalized_operation_id,
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

def get_project(
    project_id: str,
    *,
    get_session,
    config,
    display_datetime,
) -> ProjectDetail:
    session = get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        detail = build_project_detail(
            session=session,
            project=project,
            display_datetime=display_datetime,
            review_interval_chapters=max(0, int(config.review_interval_chapters if config else 0)),
        )
        return _overlay_active_generation_task(
            detail,
            _latest_active_generation_task(session, project_id),
        )
    finally:
        session.close()


__all__ = ['list_projects', 'create_project', 'delete_project', 'bulk_delete_projects', 'get_project']
