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
from forwin.generation.run_target import resolve_generation_run_target


def continue_project_generation(
    project_id: str,
    req: ProjectContinueGenerationRequest | None = None,
    *,
    get_session,
    config,
    runtime_settings,
    display_datetime,
    active_generation_task_error_cls,
    resolve_project_governance,
    governance_request_payload,
    project_has_active_generation_task,
    generation_task_conflict_message,
    log_decision_event,
    create_continue_generation_task,
    serialize_task,
    get_generation_task_or_404,
) -> TaskResponse:
    if not config:
        raise HTTPException(503, "服务尚未初始化")
    runtime_config = build_saved_runtime_config(
        base_config=config,
        runtime_settings=runtime_settings,
    )
    session = get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        if str(project.creation_status or "") in {"creating", "genesis_ready"}:
            raise HTTPException(409, "该项目仍在 Genesis 阶段，请先完成创世并点击“启动写作”。")
        governance = resolve_project_governance(
            project,
            overrides=governance_request_payload(req),
            base_config=config,
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
            generation_audit_interval_chapters=governance.generation_audit_interval_chapters,
            generation_audit_pause_enabled=governance.generation_audit_pause_enabled,
        )
        if project_has_active_generation_task(project_id, session=session):
            raise HTTPException(409, generation_task_conflict_message(project_id))
        plans = session.execute(
            select(ChapterPlan)
            .where(ChapterPlan.project_id == project_id)
            .order_by(ChapterPlan.chapter_number.asc())
        ).scalars().all()
        waiting_review = [plan.chapter_number for plan in plans if plan.status == "needs_review"]
        if waiting_review:
            raise HTTPException(409, f"仍有章节等待 review：{', '.join(str(item) for item in waiting_review)}")
        waiting_acceptance = [plan.chapter_number for plan in plans if plan.status == "drafted"]
        if waiting_acceptance:
            raise HTTPException(409, f"仍有章节等待接受：{', '.join(str(item) for item in waiting_acceptance)}")
        project_detail = build_project_detail(
            session=session,
            project=project,
            display_datetime=display_datetime,
            review_interval_chapters=governance.review_interval_chapters,
        )
        if project_detail.blocking_reason.code:
            log_decision_event(
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
        auto_continue = True if req is None or req.auto_continue is None else bool(req.auto_continue)
        run_until_chapter = req.run_until_chapter if req is not None else None
        workset = build_continue_generation_workset(
            session,
            project_id,
            max_chapters=max_chapters,
            source="direct_continue",
        )
        if workset.requested_chapters <= 0:
            raise _continue_workset_http_error(workset)
        task_max_chapters = max_chapters
        task_run_until_chapter = run_until_chapter
        if run_until_chapter is not None:
            first_chapter = int(workset.chapter_numbers[0])
            try:
                target = resolve_generation_run_target(
                    project,
                    next_chapter=first_chapter,
                    run_until_chapter=run_until_chapter,
                    max_chapters=max_chapters,
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            task_max_chapters = target.effective_max_chapters
            task_run_until_chapter = target.run_until_chapter
            workset = build_continue_generation_workset(
                session,
                project_id,
                max_chapters=task_max_chapters,
                source="direct_continue",
            )
            if workset.requested_chapters <= 0:
                raise _continue_workset_http_error(workset)
        elif max_chapters is not None:
            first_chapter = int(workset.chapter_numbers[0])
            target_total = int(getattr(project, "target_total_chapters", 0) or 0)
            batch_end_chapter = first_chapter + int(max_chapters) - 1
            if target_total >= first_chapter:
                batch_end_chapter = min(batch_end_chapter, target_total)
            task_run_until_chapter = batch_end_chapter
        task_id = call_task_factory_with_supported_kwargs(
            create_continue_generation_task,
            {
                "project_id": project_id,
                "runtime_config": runtime_config,
                "requested_chapters": workset.requested_chapters,
                "max_chapters": task_max_chapters,
                "auto_continue": auto_continue,
                "run_until_chapter": task_run_until_chapter,
                "title": project.title,
                "subtitle": f"继续生成 · {project.genre}",
                "message": "准备继续生成剩余章节。",
            },
        )
    except active_generation_task_error_cls as exc:
        raise HTTPException(409, str(exc)) from exc
    finally:
        session.close()
    return serialize_task(task_id, get_generation_task_or_404(task_id))

def extend_project_generation(
    project_id: str,
    req: ProjectExtendGenerationRequest,
    *,
    get_session,
    display_datetime,
    project_has_active_generation_task,
    generation_task_conflict_message,
) -> ProjectDetail:
    session = get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        if str(project.creation_status or "") in {"creating", "genesis_ready"}:
            raise HTTPException(409, "该项目仍在 Genesis 阶段，请先完成创世并点击“启动写作”。")
        if project_has_active_generation_task(project_id, session=session):
            raise HTTPException(409, generation_task_conflict_message(project_id))

        plans = session.execute(
            select(ChapterPlan)
            .where(ChapterPlan.project_id == project_id)
            .order_by(ChapterPlan.chapter_number.asc(), ChapterPlan.id.asc())
        ).scalars().all()
        waiting_review = [plan.chapter_number for plan in plans if plan.status == "needs_review"]
        if waiting_review:
            raise HTTPException(409, f"仍有章节等待 review：{', '.join(str(item) for item in waiting_review)}")
        waiting_acceptance = [plan.chapter_number for plan in plans if plan.status == "drafted"]
        if waiting_acceptance:
            raise HTTPException(409, f"仍有章节等待接受：{', '.join(str(item) for item in waiting_acceptance)}")
        pending_generation = [
            plan.chapter_number
            for plan in plans
            if str(plan.status or "") in {"planned", "failed"}
        ]
        if pending_generation:
            raise HTTPException(
                409,
                "已有待生成章节计划，请先使用 continue-generation："
                + ", ".join(str(item) for item in pending_generation[:12]),
            )

        additional_chapters = int(req.additional_chapters or 0)
        last_chapter = max([int(plan.chapter_number or 0) for plan in plans] or [0])
        start_chapter = last_chapter + 1
        end_chapter = start_chapter + additional_chapters - 1
        if end_chapter <= last_chapter:
            raise HTTPException(400, "追加章节数必须大于 0")

        max_arc_number = session.execute(
            select(func.max(ArcPlanVersion.arc_number)).where(ArcPlanVersion.project_id == project_id)
        ).scalar_one()
        next_arc_number = int(max_arc_number or 0) + 1
        guard = _extension_continuity_guard(req)
        updater = StateUpdater(session)
        project.target_total_chapters = max(int(project.target_total_chapters or 0), end_chapter)
        session.add(project)
        arc = updater.create_arc_plan(
            project_id=project_id,
            arc_synopsis=_extension_arc_synopsis(
                req=req,
                start_chapter=start_chapter,
                end_chapter=end_chapter,
            ),
            version=1,
            status="planned",
            arc_number=next_arc_number,
            chapter_start=start_chapter,
            chapter_end=end_chapter,
            planned_target_size=additional_chapters,
            planned_soft_min=max(1, int(round(additional_chapters * 0.85))),
            planned_soft_max=max(additional_chapters, int(round(additional_chapters * 1.20))),
        )
        for offset, chapter_number in enumerate(range(start_chapter, end_chapter + 1)):
            title, one_line, goals, experience_plan = _extension_chapter_blueprint(
                chapter_number=chapter_number,
                offset=offset,
                guard=guard,
                end_chapter=end_chapter,
            )
            updater.create_chapter_plan(
                project_id=project_id,
                arc_plan_id=arc.id,
                chapter_number=chapter_number,
                title=title,
                one_line=one_line,
                goals=goals,
                experience_plan=experience_plan,
            )
        session.commit()
        return build_project_detail(
            session=session,
            project=project,
            display_datetime=display_datetime,
        )
    finally:
        session.close()

def update_project_automation(
    project_id: str,
    req: ProjectAutomationUpdateRequest,
    *,
    get_session,
    persist_project_automation,
) -> ProjectAutomationUpdateResponse:
    session = get_session()
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
        stored = persist_project_automation(session, project, updated)
        session.commit()
        return ProjectAutomationUpdateResponse(
            ok=True,
            project_id=project_id,
            automation=stored,
            message="书本自动化设置已保存。",
        )
    finally:
        session.close()


__all__ = ['continue_project_generation', 'extend_project_generation', 'update_project_automation']
