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


def list_chapters(project_id: str, *, get_session) -> list[ChapterInfo]:
    session = get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")

        plans = session.execute(
            select(ChapterPlan).where(ChapterPlan.project_id == project_id).order_by(ChapterPlan.chapter_number)
        ).scalars().all()
        return _chapter_infos_for_plans(session, project_id, plans)
    finally:
        session.close()

def list_chapter_page(
    project_id: str,
    *,
    offset: int = 0,
    limit: int = _DEFAULT_CHAPTER_PAGE_LIMIT,
    get_session,
) -> ChapterListResponse:
    offset, limit = _normalize_chapter_page(offset, limit)
    session = get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")

        total = int(
            session.execute(
                select(func.count(ChapterPlan.id)).where(ChapterPlan.project_id == project_id)
            ).scalar_one()
            or 0
        )
        plans = session.execute(
            select(ChapterPlan)
            .where(ChapterPlan.project_id == project_id)
            .order_by(ChapterPlan.chapter_number)
            .offset(offset)
            .limit(limit)
        ).scalars().all()
        chapters = _chapter_infos_for_plans(session, project_id, plans)
        return ChapterListResponse(
            project_id=project_id,
            total=total,
            offset=offset,
            limit=limit,
            has_more=(offset + len(chapters)) < total,
            chapters=chapters,
        )
    finally:
        session.close()

def get_chapter(
    project_id: str,
    chapter_number: int,
    *,
    get_session,
) -> ChapterDetail:
    session = get_session()
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
        has_review = session.execute(
            select(ChapterReview.id).where(ChapterReview.draft_id == draft.id).limit(1)
        ).scalar_one_or_none() is not None

        return ChapterDetail(
            chapter_number=chapter_number,
            title=plan.title,
            body=draft.body_text,
            char_count=draft.char_count,
            summary=draft.summary,
            status=plan.status,
            has_draft=True,
            has_review=has_review,
            version=draft.version,
            acceptance_mode=str(getattr(plan, "acceptance_mode", "") or ""),
            repair_attempt_count=int(getattr(plan, "repair_attempt_count", 0) or 0),
            canon_risk_level=str(getattr(plan, "canon_risk_level", "") or ""),
            residual_review_issues=_load_json_object(getattr(plan, "residual_review_issues_json", "[]"), []),
        )
    finally:
        session.close()

def create_project_chapter_upload_job(
    project_id: str,
    req: ProjectChapterPublishRequest,
    *,
    get_session,
    publisher_manager,
) -> PublisherUploadJobResponse:
    session = get_session()
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
        payload = publisher_manager.create_upload_job(
            project_id=project_id,
            platform=req.platform,
            book_name=req.book_name,
            chapter_title=plan.title,
            body=draft.body_text,
            upload_url=req.upload_url,
            publish=req.publish,
            create_if_missing=req.create_if_missing,
            cover_generation_enabled=req.cover_generation_enabled,
            cover_confirmation_required=req.cover_confirmation_required,
            cover_candidate_count=req.cover_candidate_count,
            cover_style_hint=req.cover_style_hint,
            auto_cover_upload_enabled=req.auto_cover_upload_enabled,
            publisher_compliance_required=req.publisher_compliance_required,
            book_meta=req.book_meta.model_dump() if req.book_meta else None,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return PublisherUploadJobResponse(**payload)


__all__ = ['list_chapters', 'list_chapter_page', 'get_chapter', 'create_project_chapter_upload_job']
