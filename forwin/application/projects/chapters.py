from __future__ import annotations


from fastapi import HTTPException
from sqlalchemy import func, select

from forwin.api_schema import (
    ChapterDetail,
    ChapterListResponse,
    ChapterInfo,
    ProjectChapterPublishRequest,
    PublisherUploadJobResponse,
)
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.project import ChapterPlan, Project
from .common import (
    _chapter_infos_for_plans,
    _load_json_object,
    _normalize_chapter_page,
)


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


def list_chapters(project_id: str, *, get_session) -> list[ChapterInfo]:
    session = get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")

        plans = (
            session.execute(
                select(ChapterPlan)
                .where(ChapterPlan.project_id == project_id)
                .order_by(ChapterPlan.chapter_number)
            )
            .scalars()
            .all()
        )
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
                select(func.count(ChapterPlan.id)).where(
                    ChapterPlan.project_id == project_id
                )
            ).scalar_one()
            or 0
        )
        plans = (
            session.execute(
                select(ChapterPlan)
                .where(ChapterPlan.project_id == project_id)
                .order_by(ChapterPlan.chapter_number)
                .offset(offset)
                .limit(limit)
            )
            .scalars()
            .all()
        )
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
            select(ChapterDraft)
            .where(ChapterDraft.chapter_plan_id == plan.id)
            .order_by(ChapterDraft.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        if draft is None:
            raise HTTPException(404, f"第{chapter_number}章尚未生成")
        has_review = (
            session.execute(
                select(ChapterReview.id)
                .where(ChapterReview.draft_id == draft.id)
                .limit(1)
            ).scalar_one_or_none()
            is not None
        )

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
            residual_review_issues=_load_json_object(
                getattr(plan, "residual_review_issues_json", "[]"), []
            ),
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
        if plan.status != "accepted":
            raise HTTPException(409, f"第{req.chapter_number}章尚未进入 Canon")
        candidate = session.execute(
            select(CandidateDraftRecord)
            .where(
                CandidateDraftRecord.project_id == project_id,
                CandidateDraftRecord.chapter_number == req.chapter_number,
                CandidateDraftRecord.status == "accepted",
            )
            .order_by(CandidateDraftRecord.updated_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if candidate is None:
            raise HTTPException(409, f"第{req.chapter_number}章尚未进入 Canon")
        commit = session.get(CanonCommitRecord, candidate.canon_commit_id)
        draft = session.get(ChapterDraft, candidate.candidate_draft_id)
        if (
            commit is None
            or commit.status != "committed"
            or commit.candidate_id != candidate.id
            or commit.project_id != project_id
            or int(commit.chapter_number or 0) != req.chapter_number
            or commit.idempotency_key != candidate.idempotency_key
            or candidate.chapter_plan_id != plan.id
            or draft is None
            or draft.chapter_plan_id != plan.id
        ):
            raise HTTPException(409, f"第{req.chapter_number}章 Canon 身份不完整")
        canon_idempotency_key = commit.idempotency_key
        candidate_id = candidate.id
    finally:
        session.close()

    try:
        payload = publisher_manager.find_canon_job(
            canon_idempotency_key=canon_idempotency_key,
            project_id=project_id,
            chapter_number=req.chapter_number,
            candidate_id=candidate_id,
            platform=req.platform,
        )
        if payload is None:
            raise HTTPException(
                409,
                "Canon 发布任务尚未物化，请等待事件处理后重试。",
            )
        publisher_manager.release_canon_jobs(
            project_id=project_id,
            job_ids=[payload["job_id"]],
            publish=req.publish,
            actor_type="manual_ui",
        )
        payload = publisher_manager.find_canon_job(
            canon_idempotency_key=canon_idempotency_key,
            project_id=project_id,
            chapter_number=req.chapter_number,
            candidate_id=candidate_id,
            platform=req.platform,
        )
        if payload is None:
            raise HTTPException(409, "Canon 发布任务在释放期间不可用。")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return PublisherUploadJobResponse(**payload)


__all__ = [
    "list_chapters",
    "list_chapter_page",
    "get_chapter",
    "create_project_chapter_upload_job",
]
