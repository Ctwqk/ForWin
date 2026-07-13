from __future__ import annotations

import inspect
import json
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

from forwin.application.read_models import build_project_detail
from forwin.candidate_drafts import CandidateDraftRepository
from forwin.api_schema import (
    CandidateDraftDetail,
    ChapterDecisionLayerInfo,
    ChapterReviewApproveRequest,
    ChapterReviewApproveResponse,
    ChapterReviewDetail,
    ChapterReviewRetryRequest,
    ChapterRewriteAttemptInfo,
    ChapterReviewIssueInfo,
    FinalResidualDecisionInfo,
    LintSignalInfo,
    RepairVerificationInfo,
)
from forwin.generation.continue_workset import (
    build_continue_generation_workset,
)
from forwin.generation.review_auto_retry import (
    eligible_for_auto_review_retry,
    prior_auto_review_retry_count,
    reset_chapter_for_auto_review_retry,
)
from forwin.audit.events import DecisionEventType
from forwin.planning.contracts import (
    derive_chapter_task_contract,
    plan_task_contract_to_json,
)
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.phase import ChapterRewriteAttempt
from forwin.models.project import ChapterPlan, Project
from forwin.protocol.review import normalize_repair_scope
from forwin.runtime.policy_store import ProjectPolicyStore
from .common import _load_json_object


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

def _decision_refs_for_types(
    decision_refs: list[Any],
    event_types: set[str],
) -> list[Any]:
    matching = [
        event
        for event in decision_refs
        if str(getattr(event, "event_type", "") or "") in event_types
    ]
    return sorted(
        matching,
        key=lambda event: (
            str(getattr(event, "created_at", "") or ""),
            str(getattr(event, "id", "") or ""),
        ),
    )


def _unique_refs(*groups: Any) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for group in groups:
        values = group if isinstance(group, (list, tuple, set)) else [group]
        for value in values:
            normalized = str(value or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            refs.append(normalized)
    return refs


def _event_evidence_refs(events: list[Any]) -> list[str]:
    refs: list[str] = []
    for event in events:
        payload = getattr(event, "payload", {}) or {}
        if not isinstance(payload, dict):
            payload = {}
        refs.extend(
            _unique_refs(
                f"decision_event:{getattr(event, 'id', '')}",
                f"prompt_trace:{payload.get('trace_id', '')}"
                if payload.get("trace_id")
                else "",
                payload.get("evidence", []),
                payload.get("missing_evidence", []),
            )
        )
    return _unique_refs(refs)


def _build_decision_layers(
    *,
    review: Any,
    review_meta: dict[str, Any],
    issues: list[dict[str, Any]],
    rewrite_attempts: list[Any],
    candidate: Any | None,
    decision_refs: list[Any],
) -> list[ChapterDecisionLayerInfo]:
    draft_refs = _decision_refs_for_types(
        decision_refs,
        {DecisionEventType.REVIEW_VERDICT_RECORDED},
    )
    issue_evidence = [
        ref
        for issue in issues
        if isinstance(issue, dict)
        for ref in issue.get("evidence_refs", []) or []
    ]
    verdict = str(getattr(review, "verdict", "") or "").strip() or "unknown"
    layers = [
        ChapterDecisionLayerInfo(
            layer="draft_review",
            status="complete",
            outcome=verdict,
            summary=str(
                review_meta.get("review_summary")
                or review_meta.get("recommended_action")
                or "草稿评审已记录。"
            ),
            blocking=verdict == "fail",
            evidence_refs=_unique_refs(
                review_meta.get("evidence_refs", []),
                issue_evidence,
                _event_evidence_refs(draft_refs),
            ),
            decision_refs=draft_refs,
        )
    ]

    repair_refs = _decision_refs_for_types(
        decision_refs,
        {
            DecisionEventType.REPAIR_STARTED,
            DecisionEventType.REPAIR_FAILED,
            DecisionEventType.REPAIR_SUCCEEDED,
        },
    )
    if rewrite_attempts:
        latest_attempt = rewrite_attempts[0]
        failure_reason = str(getattr(latest_attempt, "failure_reason", "") or "")
        result_verdict = str(getattr(latest_attempt, "result_verdict", "") or "")
        verification = _load_json_object(
            getattr(latest_attempt, "verification_json", "{}"), {}
        )
        verification_blocked = bool(verification) and (
            not bool(verification.get("fixed_all_must_fix"))
            or not bool(verification.get("preserved_all_must_preserve"))
        )
        repair_failed = bool(failure_reason) or result_verdict == "fail"
        repair_pending = not repair_failed and not result_verdict
        repair_status = (
            "failed" if repair_failed else "pending" if repair_pending else "complete"
        )
        repair_outcome = (
            "failed"
            if repair_failed
            else result_verdict or "verification_pending"
        )
        repair_scope = normalize_repair_scope(
            getattr(latest_attempt, "repair_scope", ""), default=""
        )
        layers.append(
            ChapterDecisionLayerInfo(
                layer="repair",
                status=repair_status,
                outcome=repair_outcome,
                summary=(
                    failure_reason
                    or f"已执行 {len(rewrite_attempts)} 次修复；最新范围：{repair_scope or '未标注'}。"
                ),
                blocking=repair_failed or repair_pending or verification_blocked,
                evidence_refs=_unique_refs(
                    f"rewrite_attempt:{getattr(latest_attempt, 'id', '')}",
                    f"source_draft:{getattr(latest_attempt, 'source_draft_id', '')}",
                    f"result_draft:{getattr(latest_attempt, 'result_draft_id', '')}",
                    f"result_review:{getattr(latest_attempt, 'result_review_id', '')}",
                    _event_evidence_refs(repair_refs),
                ),
                decision_refs=repair_refs,
            )
        )
    else:
        repair_required = verdict == "fail"
        layers.append(
            ChapterDecisionLayerInfo(
                layer="repair",
                status="pending" if repair_required else "not_required",
                outcome="not_started" if repair_required else "not_required",
                summary=(
                    "草稿评审要求修复，但尚无修复尝试。"
                    if repair_required
                    else "当前草稿评审不需要修复。"
                ),
                blocking=repair_required,
                evidence_refs=_event_evidence_refs(repair_refs),
                decision_refs=repair_refs,
            )
        )

    residual_refs = _decision_refs_for_types(
        decision_refs,
        {
            DecisionEventType.FORCED_ACCEPT_APPLIED,
            DecisionEventType.HARD_GATE_HIT,
        },
    )
    final_residual = review_meta.get("final_residual_decision")
    if isinstance(final_residual, dict):
        residual_outcome = str(final_residual.get("decision") or "unknown")
        residual_blocking = residual_outcome != "force_accept"
        residual_status = "blocked" if residual_blocking else "complete"
        residual_summary = str(
            final_residual.get("reason") or "最终残余资格判定已完成。"
        )
        residual_evidence = _unique_refs(
            [
                f"residual_issue:{item}"
                for item in final_residual.get("residual_issues", []) or []
            ],
            _event_evidence_refs(residual_refs),
        )
    else:
        eligibility = (
            _load_json_object(candidate.eligibility_decision_json, {})
            if candidate is not None
            else {}
        )
        if eligibility:
            eligible = bool(eligibility.get("eligible"))
            residual_outcome = "eligible" if eligible else "ineligible"
            residual_status = "complete" if eligible else "blocked"
            residual_blocking = not eligible
            residual_summary = str(
                eligibility.get("reason")
                or (
                    "候选稿已通过 Canon 资格检查。"
                    if eligible
                    else "候选稿未通过 Canon 资格检查。"
                )
            )
            residual_evidence = _unique_refs(
                f"candidate:{getattr(candidate, 'id', '')}",
                f"body_hash:{eligibility.get('body_hash', '')}",
                f"plan_revision:{eligibility.get('plan_revision', '')}",
                _event_evidence_refs(residual_refs),
            )
        elif candidate is not None and str(candidate.status or "") in {
            "needs_review",
            "failed",
        }:
            residual_outcome = "ineligible"
            residual_status = "blocked"
            residual_blocking = True
            residual_summary = str(candidate.failure_reason or "候选稿资格被阻断。")
            residual_evidence = _unique_refs(
                f"candidate:{candidate.id}",
                _event_evidence_refs(residual_refs),
            )
        else:
            residual_outcome = "not_evaluated"
            residual_status = "pending"
            residual_blocking = True
            residual_summary = "尚未形成最终残余资格判定。"
            residual_evidence = _event_evidence_refs(residual_refs)
    layers.append(
        ChapterDecisionLayerInfo(
            layer="residual_eligibility",
            status=residual_status,
            outcome=residual_outcome,
            summary=residual_summary,
            blocking=residual_blocking,
            evidence_refs=residual_evidence,
            decision_refs=residual_refs,
        )
    )

    gate_types = {
        DecisionEventType.GATE_DELEGATION_REQUESTED,
        DecisionEventType.GATE_DELEGATION_DECIDED,
        DecisionEventType.GATE_DELEGATION_FAILED,
        DecisionEventType.GATE_DELEGATION_APPROVED,
    }
    gate_refs = _decision_refs_for_types(decision_refs, gate_types)
    if not gate_refs:
        gate_status = "not_required"
        gate_outcome = "not_delegated"
        gate_summary = "本章未调用委托门禁。"
        gate_blocking = False
    else:
        latest_gate = gate_refs[-1]
        latest_type = str(getattr(latest_gate, "event_type", "") or "")
        payload = getattr(latest_gate, "payload", {}) or {}
        if not isinstance(payload, dict):
            payload = {}
        if latest_type == DecisionEventType.GATE_DELEGATION_APPROVED:
            gate_status, gate_outcome, gate_blocking = "complete", "approved", False
        elif latest_type == DecisionEventType.GATE_DELEGATION_FAILED:
            gate_status, gate_outcome, gate_blocking = "failed", "failed", True
        elif latest_type == DecisionEventType.GATE_DELEGATION_DECIDED:
            gate_outcome = str(payload.get("decision") or "unknown")
            gate_status = "complete"
            gate_blocking = gate_outcome != "approve"
        else:
            gate_status, gate_outcome, gate_blocking = "pending", "pending", True
        gate_summary = str(
            getattr(latest_gate, "summary", "")
            or getattr(latest_gate, "reason", "")
            or "委托门禁已记录。"
        )
    layers.append(
        ChapterDecisionLayerInfo(
            layer="gate_delegation",
            status=gate_status,
            outcome=gate_outcome,
            summary=gate_summary,
            blocking=gate_blocking,
            evidence_refs=_event_evidence_refs(gate_refs),
            decision_refs=gate_refs,
        )
    )

    canon_refs = _decision_refs_for_types(
        decision_refs,
        {DecisionEventType.CANON_COMMIT, DecisionEventType.CANON_COMMIT_FAILED},
    )
    if candidate is None:
        canon_status, canon_outcome, canon_blocking = "missing", "no_candidate", True
        canon_summary = "缺少候选稿记录，无法确认 Canon 状态。"
        canon_evidence: list[str] = _event_evidence_refs(canon_refs)
    else:
        candidate_status = str(candidate.status or "")
        candidate_canon_status = str(candidate.canon_status or "candidate")
        commit_id = str(candidate.canon_commit_id or "")
        if (
            candidate_status == "accepted"
            and candidate_canon_status == "canon"
            and commit_id
        ):
            canon_status, canon_outcome, canon_blocking = (
                "complete",
                "committed",
                False,
            )
            canon_summary = f"Canon 已提交：{commit_id}"
        elif candidate_status in {"needs_review", "failed"}:
            canon_status, canon_outcome, canon_blocking = (
                "blocked",
                candidate_status,
                True,
            )
            canon_summary = str(candidate.failure_reason or "Canon 提交被阻断。")
        elif candidate_status == "accepted":
            canon_status, canon_outcome, canon_blocking = (
                "blocked",
                "inconsistent_commit",
                True,
            )
            canon_summary = "候选稿标记 accepted，但缺少完整 Canon commit 身份。"
        else:
            canon_status, canon_outcome, canon_blocking = (
                "pending",
                candidate_status or candidate_canon_status,
                True,
            )
            canon_summary = "候选稿尚未完成 Canon 原子提交。"
        canon_evidence = _unique_refs(
            f"candidate:{candidate.id}",
            f"canon_commit:{commit_id}" if commit_id else "",
            f"canon_artifact:{candidate.canon_artifact_path}"
            if candidate.canon_artifact_path
            else "",
            _event_evidence_refs(canon_refs),
        )
    layers.append(
        ChapterDecisionLayerInfo(
            layer="canon",
            status=canon_status,
            outcome=canon_outcome,
            summary=canon_summary,
            blocking=canon_blocking,
            evidence_refs=canon_evidence,
            decision_refs=canon_refs,
        )
    )
    return layers


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
        rewrite_attempts = (
            session.execute(
                select(ChapterRewriteAttempt)
                .where(
                    ChapterRewriteAttempt.project_id == project_id,
                    ChapterRewriteAttempt.chapter_number == chapter_number,
                )
                .order_by(
                    ChapterRewriteAttempt.attempt_no.desc(),
                    ChapterRewriteAttempt.created_at.desc(),
                )
            )
            .scalars()
            .all()
        )
        decision_refs = decision_refs_for_chapter_review(
            session,
            project_id=project_id,
            chapter_number=chapter_number,
            review_id=review.id,
        )
        candidate = CandidateDraftRepository(session).latest_for_chapter(
            project_id=project_id,
            chapter_number=chapter_number,
        )
        latest_attempt = rewrite_attempts[0] if rewrite_attempts else None
        residual_review_issues = (
            review_meta.get("residual_review_issues")
            if isinstance(review_meta.get("residual_review_issues"), list)
            else _load_json_object(
                getattr(plan, "residual_review_issues_json", "[]"), []
            )
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
                dict(
                    (review_meta.get("repair_instruction") or {}).get("design_patch")
                    or {}
                )
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
                RepairVerificationInfo.model_validate(
                    review_meta.get("repair_verification")
                )
                if isinstance(review_meta.get("repair_verification"), dict)
                else None
            ),
            final_residual_decision=(
                FinalResidualDecisionInfo.model_validate(
                    review_meta.get("final_residual_decision")
                )
                if isinstance(review_meta.get("final_residual_decision"), dict)
                else None
            ),
            repair_exhausted=bool(review_meta.get("repair_exhausted")),
            rewrite_attempts=[
                ChapterRewriteAttemptInfo(
                    attempt_no=int(item.attempt_no or 0),
                    repair_phase=str(
                        getattr(item, "repair_phase", "") or "review_repair"
                    ),
                    phase_attempt_no=int(getattr(item, "phase_attempt_no", 0) or 0),
                    repair_scope=normalize_repair_scope(
                        item.repair_scope or "", default=""
                    ),
                    result_verdict=str(item.result_verdict or ""),
                    result_review_id=str(getattr(item, "result_review_id", "") or ""),
                    failure_reason=str(getattr(item, "failure_reason", "") or ""),
                    forced_accept_applied=bool(item.forced_accept_applied),
                    design_patch=_load_json_object(item.design_patch_json, {}),
                    verification=(
                        RepairVerificationInfo.model_validate(
                            _load_json_object(
                                getattr(item, "verification_json", "{}"), {}
                            )
                        )
                        if _load_json_object(
                            getattr(item, "verification_json", "{}"), {}
                        )
                        else None
                    ),
                    source_chapter_plan=_load_json_object(
                        getattr(item, "source_chapter_plan_json", "{}"), {}
                    ),
                    result_chapter_plan=_load_json_object(
                        getattr(item, "result_chapter_plan_json", "{}"), {}
                    ),
                    source_band_plan=_load_json_object(
                        getattr(item, "source_band_plan_json", "{}"), {}
                    ),
                    result_band_plan=_load_json_object(
                        getattr(item, "result_band_plan_json", "{}"), {}
                    ),
                )
                for item in reversed(rewrite_attempts)
            ],
            decision_refs=decision_refs,
            decision_layers=_build_decision_layers(
                review=review,
                review_meta=review_meta,
                issues=[item for item in issues if isinstance(item, dict)],
                rewrite_attempts=rewrite_attempts,
                candidate=candidate,
                decision_refs=decision_refs,
            ),
            rule_decision=_latest_rule_decision(decision_refs),
        )
    finally:
        session.close()


def _latest_rule_decision(decision_refs: list[Any]) -> dict[str, Any]:
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
            raise HTTPException(
                404, f"第{chapter_number}章尚未生成 candidate draft"
            ) from exc
        raise
    session = get_session()
    try:
        record = CandidateDraftRepository(session).latest_for_chapter(
            project_id=project_id,
            chapter_number=chapter_number,
        )
        if record is None:
            raise HTTPException(404, f"第{chapter_number}章缺少 v5 candidate record")
        record_status = str(record.status or "")
        canon_status = str(record.canon_status or "candidate")
        canon_ready = canon_status == "canon"
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
            scene_outputs=_load_json_object(record.scene_outputs_json, []),
            state_change_candidates=_load_json_object(
                record.state_change_candidates_json, []
            ),
            event_candidates=_load_json_object(record.event_candidates_json, []),
            thread_beat_candidates=_load_json_object(
                record.thread_beat_candidates_json, []
            ),
            review_verdict=review.verdict,
            review_summary=review.review_summary,
            repair_attempts=review.rewrite_attempts,
            repair_attempt_count=int(record.repair_attempt_count or 0),
            canon_ready=canon_ready,
            canon_status=canon_status,
            canon_artifact_path=str(record.canon_artifact_path or ""),
            failure_reason=str(record.failure_reason or ""),
        )
    finally:
        session.close()


def approve_chapter_review(
    project_id: str,
    chapter_number: int,
    req: ChapterReviewApproveRequest,
    *,
    config,
    pipeline,
    get_session,
    display_datetime,
    active_generation_task_error_cls,
    require_reason,
    project_has_active_generation_task,
    generation_task_conflict_message,
    log_decision_event,
    create_continue_generation_task,
    update_task,
) -> ChapterReviewApproveResponse:
    if config is None or pipeline is None:
        raise HTTPException(500, "服务尚未完成初始化")

    reason = require_reason(req.reason, action="接受 review")
    task_id = ""
    if req.continue_generation:
        session = get_session()
        try:
            project = session.get(Project, project_id)
            if project is None:
                raise HTTPException(404, "项目不存在")
            ProjectPolicyStore(session).load(project)
            if project_has_active_generation_task(project_id, session=session):
                raise HTTPException(409, generation_task_conflict_message(project_id))
            project_detail = build_project_detail(
                session=session,
                project=project,
                display_datetime=display_datetime,
            )
            if project_detail.blocking_reason.code:
                log_decision_event(
                    session,
                    project_id=project_id,
                    event_family="evaluation_verdict",
                    event_type=DecisionEventType.HARD_GATE_HIT,
                    actor_type="api",
                    scope="project",
                    summary=project_detail.blocking_reason.message
                    or project_detail.blocking_reason.code,
                    payload={"blocking_reason": project_detail.blocking_reason.code},
                    band_id=project_detail.blocking_reason.band_id,
                    chapter_number=int(
                        project_detail.blocking_reason.chapter_number or 0
                    ),
                    related_object_type="project",
                    related_object_id=project_id,
                )
                session.commit()
                raise HTTPException(409, project_detail.blocking_reason.message)
        finally:
            session.close()

    try:
        accept_review_parameters = inspect.signature(pipeline.accept_review).parameters
        if "reason" in accept_review_parameters:
            result = pipeline.accept_review(project_id, chapter_number, reason=reason)
        else:
            result = pipeline.accept_review(project_id, chapter_number)
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
                requested_chapters=workset.requested_chapters,
                message=f"已接受第{chapter_number}章，准备继续后续章节。",
            )
        except active_generation_task_error_cls as exc:
            raise HTTPException(409, str(exc)) from exc
        update_task(
            task_id,
            frozen_artifacts=[result["frozen_artifact"]]
            if result["frozen_artifact"]
            else [],
        )
        message = f"{message} 已启动后续章节继续执行。"
    elif req.continue_generation and accepted_status == "needs_review":
        auto_retry_payload = _auto_retry_review_approve_blocker(
            project_id=project_id,
            chapter_number=chapter_number,
            result=result,
            message=message,
            reason=reason,
            get_session=get_session,
            active_generation_task_error_cls=active_generation_task_error_cls,
            create_continue_generation_task=create_continue_generation_task,
            update_task=update_task,
        )
        if auto_retry_payload is not None:
            return auto_retry_payload
        message = f"{message} 未启动后续章节。"
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


def _auto_retry_review_approve_blocker(
    *,
    project_id: str,
    chapter_number: int,
    result: dict[str, Any],
    message: str,
    reason: str,
    get_session,
    active_generation_task_error_cls,
    create_continue_generation_task,
    update_task,
) -> ChapterReviewApproveResponse | None:
    frozen_artifact = str(result.get("frozen_artifact") or "")
    task_id = ""
    requested_chapters = 0
    session = get_session()
    try:
        plan = session.execute(
            select(ChapterPlan).where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number == chapter_number,
            )
        ).scalar_one_or_none()
        if plan is None or str(plan.status or "") != "needs_review":
            return None
        if not eligible_for_auto_review_retry(plan):
            return None
        if prior_auto_review_retry_count(session, project_id, chapter_number) > 0:
            return None
        reset_chapter_for_auto_review_retry(
            session,
            project_id=project_id,
            chapter_number=chapter_number,
            plan=plan,
            source="review_approve_auto_retry",
            reason=reason or "review_approve_auto_retry",
            summary=f"第{chapter_number}章 canon gate needs_review 自动重置为 planned。",
            terminal_block_reason="review_approve_canon_gate",
            system_block=True,
            frozen_artifact=frozen_artifact,
        )
        workset = build_continue_generation_workset(
            session,
            project_id,
            source="review_approve_auto_retry",
        )
        requested_chapters = int(workset.requested_chapters or 0)
        session.commit()
    finally:
        session.close()

    if requested_chapters > 0:
        try:
            task_id = create_continue_generation_task(
                project_id=project_id,
                requested_chapters=requested_chapters,
                message=f"第{chapter_number}章 canon gate 阻断后已自动重置，准备重新生成。",
            )
        except active_generation_task_error_cls as exc:
            raise HTTPException(409, str(exc)) from exc
        update_task(
            task_id,
            frozen_artifacts=[frozen_artifact] if frozen_artifact else [],
        )
        message = f"{message} 已自动重置并启动重试。"
    else:
        message = f"{message} 已自动重置为 planned，但没有剩余章节需要继续执行。"

    return ChapterReviewApproveResponse(
        ok=True,
        project_id=project_id,
        chapter_number=chapter_number,
        status="planned",
        message=message,
        task_id=task_id,
        frozen_artifact=frozen_artifact,
    )


def retry_chapter_review(
    project_id: str,
    chapter_number: int,
    req: ChapterReviewRetryRequest,
    *,
    config,
    get_session,
    active_generation_task_error_cls,
    require_reason,
    project_has_active_generation_task,
    generation_task_conflict_message,
    log_decision_event,
    create_continue_generation_task,
) -> ChapterReviewApproveResponse:
    if config is None:
        raise HTTPException(500, "服务尚未完成初始化")

    reason = require_reason(req.reason, action="重试 review 章节")
    task_id = ""
    continue_requested_chapters = 0
    session = get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        if project_has_active_generation_task(project_id, session=session):
            raise HTTPException(409, generation_task_conflict_message(project_id))
        ProjectPolicyStore(session).load(project)
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
            payload={
                "chapter_number": chapter_number,
                "previous_status": previous_status,
            },
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


__all__ = [
    "get_chapter_review",
    "get_candidate_draft",
    "approve_chapter_review",
    "retry_chapter_review",
]
