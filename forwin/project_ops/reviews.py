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


def latest_rewrite_attempts_by_chapter(
    session,
    project_id: str,
    chapter_numbers: list[int] | None = None,
) -> dict[int, ChapterRewriteAttempt]:
    return load_latest_rewrite_attempts_by_chapter(session, project_id, chapter_numbers)

def get_chapter_review(
    project_id: str,
    chapter_number: int,
    *,
    get_session,
    decision_refs_for_chapter_review,
) -> ChapterReviewDetail:
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
            raise HTTPException(404, f"第{chapter_number}章尚未生成 draft")

        review = session.execute(
            select(ChapterReview)
            .where(ChapterReview.draft_id == draft.id)
            .order_by(ChapterReview.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if review is None:
            raise HTTPException(404, f"第{chapter_number}章尚未生成 review")

        issues = _load_json_object(review.issues_json, [])
        review_meta = _load_json_object(review.review_meta_json, {})
        rewrite_attempts = session.execute(
            select(ChapterRewriteAttempt)
            .where(
                ChapterRewriteAttempt.project_id == project_id,
                ChapterRewriteAttempt.chapter_number == chapter_number,
            )
            .order_by(ChapterRewriteAttempt.attempt_no.desc(), ChapterRewriteAttempt.created_at.desc())
        ).scalars().all()
        decision_refs = decision_refs_for_chapter_review(
            session,
            project_id=project_id,
            chapter_number=chapter_number,
            review_id=review.id,
        )
        latest_attempt = rewrite_attempts[0] if rewrite_attempts else None
        residual_review_issues = (
            review_meta.get("residual_review_issues")
            if isinstance(review_meta.get("residual_review_issues"), list)
            else _load_json_object(getattr(plan, "residual_review_issues_json", "[]"), [])
        )
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
            issues=[
                ChapterReviewIssueInfo.model_validate(issue)
                for issue in issues
                if isinstance(issue, dict)
            ],
            artifact_meta_path=draft.llm_raw_response,
            recommended_action=str(review_meta.get("recommended_action") or ""),
            review_summary=str(review_meta.get("review_summary") or ""),
            planned_reward_tags=[
                str(item)
                for item in (review_meta.get("planned_reward_tags") or [])
                if str(item).strip()
            ],
            delivered_reward_tags=[
                str(item)
                for item in (review_meta.get("delivered_reward_tags") or [])
                if str(item).strip()
            ],
            experience_scores={
                str(key): float(value)
                for key, value in (review_meta.get("experience_scores") or {}).items()
            },
            review_notes=[
                str(item)
                for item in (review_meta.get("review_notes") or [])
                if str(item).strip()
            ],
            lint_signals=[
                LintSignalInfo.model_validate(item)
                for item in (review_meta.get("lint_signals") or [])
                if isinstance(item, dict)
            ],
            evidence_refs=[
                str(item)
                for item in (review_meta.get("evidence_refs") or [])
                if str(item).strip()
            ],
            confirmed_signal_refs=[
                str(item)
                for item in (review_meta.get("confirmed_signal_refs") or [])
                if str(item).strip()
            ],
            reviewer_mode=str(review_meta.get("reviewer_mode") or ""),
            proposed_design_patch=(
                dict((review_meta.get("repair_instruction") or {}).get("design_patch") or {})
                if isinstance(review_meta.get("repair_instruction"), dict)
                else {}
            ),
            rewrite_attempt_count=len(rewrite_attempts),
            latest_repair_scope=(
                normalize_repair_scope(latest_attempt.repair_scope or "", default="")
                if latest_attempt
                else ""
            ),
            latest_repair_scope_reason=str(review_meta.get("scope_reason") or ""),
            forced_accept_applied=bool(review_meta.get("forced_accept_applied")),
            acceptance_mode=str(getattr(plan, "acceptance_mode", "") or ""),
            repair_attempt_count=int(getattr(plan, "repair_attempt_count", 0) or 0),
            canon_risk_level=str(getattr(plan, "canon_risk_level", "") or ""),
            residual_review_issues=[
                ChapterReviewIssueInfo.model_validate(item)
                for item in residual_review_issues
                if isinstance(item, dict)
            ],
            repair_verification=(
                RepairVerificationInfo.model_validate(review_meta.get("repair_verification"))
                if isinstance(review_meta.get("repair_verification"), dict)
                else None
            ),
            final_gate_decision=(
                FinalGateDecisionInfo.model_validate(review_meta.get("final_gate_decision"))
                if isinstance(review_meta.get("final_gate_decision"), dict)
                else None
            ),
            repair_exhausted=bool(review_meta.get("repair_exhausted")),
            rewrite_attempts=[
                ChapterRewriteAttemptInfo(
                    attempt_no=int(item.attempt_no or 0),
                    repair_scope=normalize_repair_scope(item.repair_scope or "", default=""),
                    result_verdict=str(item.result_verdict or ""),
                    result_review_id=str(getattr(item, "result_review_id", "") or ""),
                    failure_reason=str(getattr(item, "failure_reason", "") or ""),
                    forced_accept_applied=bool(item.forced_accept_applied),
                    design_patch=_load_json_object(item.design_patch_json, {}),
                    verification=(
                        RepairVerificationInfo.model_validate(_load_json_object(getattr(item, "verification_json", "{}"), {}))
                        if _load_json_object(getattr(item, "verification_json", "{}"), {})
                        else None
                    ),
                    source_chapter_plan=_load_json_object(getattr(item, "source_chapter_plan_json", "{}"), {}),
                    result_chapter_plan=_load_json_object(getattr(item, "result_chapter_plan_json", "{}"), {}),
                    source_band_plan=_load_json_object(getattr(item, "source_band_plan_json", "{}"), {}),
                    result_band_plan=_load_json_object(getattr(item, "result_band_plan_json", "{}"), {}),
                )
                for item in reversed(rewrite_attempts)
            ],
            decision_refs=decision_refs,
            review_engine_decision=_latest_review_engine_decision(decision_refs),
        )
    finally:
        session.close()


def _latest_review_engine_decision(decision_refs: list[Any]) -> dict[str, Any]:
    for event in reversed(decision_refs):
        payload = getattr(event, "payload", {}) or {}
        if not isinstance(payload, dict):
            continue
        rule_id = str(payload.get("rule_id") or "").strip()
        if not rule_id:
            continue
        return {
            "rule_id": rule_id,
            "outcome": str(payload.get("outcome") or ""),
            "reason": str(payload.get("reason") or getattr(event, "reason", "") or ""),
            "missing_evidence": [
                str(item)
                for item in payload.get("missing_evidence", []) or []
                if str(item).strip()
            ],
            "routed_from": str(payload.get("routed_from") or ""),
        }
    return {}

def get_candidate_draft(
    project_id: str,
    chapter_number: int,
    *,
    get_session,
    decision_refs_for_chapter_review,
) -> CandidateDraftDetail:
    try:
        review = get_chapter_review(
            project_id,
            chapter_number,
            get_session=get_session,
            decision_refs_for_chapter_review=decision_refs_for_chapter_review,
        )
    except HTTPException as exc:
        if exc.status_code == 404 and "draft" in str(exc.detail).lower():
            raise HTTPException(404, f"第{chapter_number}章尚未生成 candidate draft") from exc
        raise
    session = get_session()
    try:
        record = CandidateDraftRepository(session).latest_for_chapter(
            project_id=project_id,
            chapter_number=chapter_number,
        )
        record_status = str(getattr(record, "status", "") or review.status or "")
        canon_status = str(getattr(record, "canon_status", "") or "")
        if not canon_status:
            canon_status = "canon" if str(review.status or "") == "accepted" else "candidate"
        canon_ready = canon_status == "canon" or str(review.status or "") == "accepted"
        return CandidateDraftDetail(
            project_id=review.project_id,
            chapter_number=review.chapter_number,
            title=review.title,
            status=record_status,
            candidate_draft_id=review.draft_id,
            version=review.version,
            body=review.body,
            summary=review.summary,
            char_count=len(review.body or ""),
            scene_outputs=_load_json_object(getattr(record, "scene_outputs_json", "[]"), []) if record else [],
            state_change_candidates=_load_json_object(getattr(record, "state_change_candidates_json", "[]"), []) if record else [],
            event_candidates=_load_json_object(getattr(record, "event_candidates_json", "[]"), []) if record else [],
            thread_beat_candidates=_load_json_object(getattr(record, "thread_beat_candidates_json", "[]"), []) if record else [],
            review_verdict=review.verdict,
            review_summary=review.review_summary,
            repair_attempts=review.rewrite_attempts,
            repair_attempt_count=(
                int(getattr(record, "repair_attempt_count", 0) or 0)
                if record is not None
                else int(getattr(review, "repair_attempt_count", 0) or len(review.rewrite_attempts))
            ),
            canon_ready=canon_ready,
            canon_status=canon_status,
            canon_artifact_path=str(getattr(record, "canon_artifact_path", "") or ""),
            failure_reason=str(getattr(record, "failure_reason", "") or ""),
        )
    finally:
        session.close()

def approve_chapter_review(
    project_id: str,
    chapter_number: int,
    req: ChapterReviewApproveRequest,
    *,
    config,
    orchestrator,
    runtime_settings,
    get_session,
    display_datetime,
    active_generation_task_error_cls,
    require_reason,
    resolve_project_governance,
    project_has_active_generation_task,
    generation_task_conflict_message,
    log_decision_event,
    create_continue_generation_task,
    update_task,
) -> ChapterReviewApproveResponse:
    if config is None or orchestrator is None:
        raise HTTPException(500, "服务尚未完成初始化")

    runtime_config = build_saved_runtime_config(
        base_config=config,
        runtime_settings=runtime_settings,
    )
    reason = require_reason(req.reason, action="接受 review")
    task_id = ""
    if req.continue_generation:
        session = get_session()
        try:
            project = session.get(Project, project_id)
            if project is None:
                raise HTTPException(404, "项目不存在")
            governance = resolve_project_governance(project, base_config=config)
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
        finally:
            session.close()

    try:
        accept_review_parameters = inspect.signature(orchestrator.accept_review).parameters
        if "reason" in accept_review_parameters:
            result = orchestrator.accept_review(project_id, chapter_number, reason=reason)
        else:
            result = orchestrator.accept_review(project_id, chapter_number)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc

    accepted_status = str(result.get("status") or "accepted")
    message = result["message"]
    if req.continue_generation and accepted_status == "accepted":
        session = get_session()
        try:
            workset = build_continue_generation_workset(
                session,
                project_id,
                source="review_approve_continue",
            )
        finally:
            session.close()
        if workset.requested_chapters <= 0:
            message = f"{message} 未启动后续章节。"
            return ChapterReviewApproveResponse(
                ok=True,
                project_id=project_id,
                chapter_number=chapter_number,
                status=accepted_status,
                message=message,
                task_id=task_id,
                frozen_artifact=result.get("frozen_artifact") or "",
            )
        try:
            task_id = create_continue_generation_task(
                project_id=project_id,
                runtime_config=runtime_config,
                requested_chapters=workset.requested_chapters,
                message=f"已接受第{chapter_number}章，准备继续后续章节。",
            )
        except active_generation_task_error_cls as exc:
            raise HTTPException(409, str(exc)) from exc
        update_task(
            task_id,
            frozen_artifacts=[result["frozen_artifact"]] if result["frozen_artifact"] else [],
        )
        message = f"{message} 已启动后续章节继续执行。"
    elif req.continue_generation:
        message = f"{message} 未启动后续章节。"

    return ChapterReviewApproveResponse(
        ok=True,
        project_id=project_id,
        chapter_number=chapter_number,
        status=accepted_status,
        message=message,
        task_id=task_id,
        frozen_artifact=result.get("frozen_artifact") or "",
    )

def retry_chapter_review(
    project_id: str,
    chapter_number: int,
    req: ChapterReviewRetryRequest,
    *,
    config,
    runtime_settings,
    get_session,
    active_generation_task_error_cls,
    require_reason,
    resolve_project_governance,
    project_has_active_generation_task,
    generation_task_conflict_message,
    log_decision_event,
    create_continue_generation_task,
) -> ChapterReviewApproveResponse:
    if config is None:
        raise HTTPException(500, "服务尚未完成初始化")

    reason = require_reason(req.reason, action="重试 review 章节")
    runtime_config = build_saved_runtime_config(
        base_config=config,
        runtime_settings=runtime_settings,
    )
    task_id = ""
    continue_requested_chapters = 0
    session = get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        if project_has_active_generation_task(project_id, session=session):
            raise HTTPException(409, generation_task_conflict_message(project_id))
        governance = resolve_project_governance(project, base_config=config)
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
        plan = session.execute(
            select(ChapterPlan).where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number == chapter_number,
            )
        ).scalar_one_or_none()
        if plan is None:
            raise HTTPException(404, f"第{chapter_number}章不存在")
        previous_status = str(plan.status or "")
        allowed_statuses = {"needs_review", "drafted"}
        if bool(getattr(req, "allow_accepted", False)):
            allowed_statuses.add("accepted")
        if previous_status not in allowed_statuses:
            raise HTTPException(
                400,
                f"第{chapter_number}章不是可 retry 状态（当前 {previous_status or 'unknown'}）",
            )
        plan.status = "planned"
        plan.acceptance_mode = ""
        plan.repair_attempt_count = 0
        plan.residual_review_issues_json = "[]"
        plan.canon_risk_level = ""
        goals_payload = _load_json_object(plan.goals_json, [])
        if isinstance(goals_payload, list):
            cleaned_goals = [
                str(item).strip()
                for item in goals_payload
                if len(str(item).strip()) >= 2
            ]
            if cleaned_goals != goals_payload:
                plan.goals_json = json.dumps(cleaned_goals, ensure_ascii=False)
                plan.task_contract_json = plan_task_contract_to_json(
                    derive_chapter_task_contract(cleaned_goals)
                )
        session.add(plan)
        log_decision_event(
            session,
            project_id=project_id,
            event_family="audit_action",
            event_type=DecisionEventType.RETRY_ATTEMPT,
            actor_type="api",
            scope="chapter",
            summary=f"第{chapter_number}章 review 候选已重置为 planned，等待重写。",
            reason=reason,
            payload={"chapter_number": chapter_number, "previous_status": previous_status},
            chapter_number=chapter_number,
            related_object_type="chapter",
            related_object_id=str(plan.id),
        )
        if req.continue_generation:
            workset = build_continue_generation_workset(
                session,
                project_id,
                source="review_retry_continue",
            )
            continue_requested_chapters = int(workset.requested_chapters or 0)
        session.commit()
    finally:
        session.close()

    message = f"第{chapter_number}章已重置为 planned。"
    if req.continue_generation and continue_requested_chapters > 0:
        try:
            task_id = create_continue_generation_task(
                project_id=project_id,
                runtime_config=runtime_config,
                requested_chapters=continue_requested_chapters,
                message=f"已重置第{chapter_number}章，准备重新生成。",
            )
        except active_generation_task_error_cls as exc:
            raise HTTPException(409, str(exc)) from exc
        message = f"{message} 已启动后续章节继续执行。"
    elif req.continue_generation:
        message = f"{message} 未启动后续章节。"

    return ChapterReviewApproveResponse(
        ok=True,
        project_id=project_id,
        chapter_number=chapter_number,
        status="planned",
        message=message,
        task_id=task_id,
        frozen_artifact="",
    )


__all__ = ['latest_rewrite_attempts_by_chapter', 'get_chapter_review', 'get_candidate_draft', 'approve_chapter_review', 'retry_chapter_review']
