from __future__ import annotations

import logging

from forwin.candidate_drafts import CandidateDraftRepository
from forwin.canon.types import CanonAdmissionOutcome
from forwin.checker.hard_floor import run_hard_floor
from forwin.checker.pulp_policy import evaluate_pulp_beat_policy
from forwin.experience.trope_cooldown import save_accepted_trope_usage_for_chapter
from forwin.maintenance import deferred as deferred_maintenance
from forwin.models.audit import DecisionEvent
from forwin.generation.pipeline_core.chapter_review_gate import (
    handle_chapter_review_gate,
)
from forwin.generation.pipeline_core import chapter_execution_support
from forwin.generation.pipeline_core.obligation_resolution import (
    _verify_obligations_after_acceptance,
)
from forwin.generation.pipeline_core.result import RunResult
from forwin.planning.checkpoints import BandCheckpointDetail, BandCheckpointIssueInfo
from forwin.audit.events import DecisionEventType
from forwin.audit.gate_outcome import attach_gate_outcome
from forwin.review.issue_groups import issue_group_for_issue
import json
from forwin.generation.pipeline_core.common import TransientLLMChapterFailure
from forwin.checker.rules import ContinuityChecker
from sqlalchemy.orm import Session
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.review.repair.service import (
    _canon_repair_scope,
    _canon_repair_scope_can_run,
)

logger = logging.getLogger(__name__)


class ChapterExecutionStage:
    """Owns the project chapters stage behavior."""

    def _run_project_chapters(
        self,
        *,
        session: Session,
        repo: StateRepository,
        updater: StateUpdater,
        checker: ContinuityChecker,
        project_id: str,
        chapter_numbers: list[int],
        requested_chapters: int,
    ) -> RunResult:
        completed_chapters: list[int] = []
        failed_chapters: list[int] = []
        paused_chapters: list[int] = []
        frozen_artifacts: list[str] = []
        system_block_chapters: list[int] = []
        last_requested_chapter = max(chapter_numbers, default=0)
        project = repo.get_project(project_id)
        if project is None:
            raise ValueError(f"项目不存在: {project_id}")
        policy = self._project_policy(session, project)

        for chapter_num in chapter_numbers:
            if self._abort_requested():
                return self._cancelled_result(
                    project_id,
                    requested_chapters,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                    frozen_artifacts=frozen_artifacts,
                    current_chapter=chapter_num,
                )
            if self._pause_requested():
                return self._paused_result(
                    project_id,
                    requested_chapters,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                    frozen_artifacts=frozen_artifacts,
                    current_chapter=max(0, chapter_num - 1),
                )
            print(f"\n{'─' * 60}")
            print(f"正在生成第 {chapter_num} 章...")
            print(f"{'─' * 60}")

            chapter_plan = repo.get_chapter_plan(project_id, chapter_num)
            if chapter_plan is None:
                logger.error("Chapter plan %d not found, skipping.", chapter_num)
                failed_chapters.append(chapter_num)
                continue
            block_code, block_band_id, block_message = self._strict_progression_block(
                session=session,
                repo=repo,
                updater=updater,
                project=project,
                chapter_number=chapter_num,
            )
            if block_code:
                self._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    band_id=block_band_id,
                    chapter_number=chapter_num,
                    event_family="evaluation_verdict",
                    event_type=DecisionEventType.HARD_GATE_HIT,
                    scope="chapter",
                    summary=block_message,
                    related_object_type="chapter_plan",
                    related_object_id=chapter_plan.id,
                    payload={"blocking_reason": block_code},
                )
                session.commit()
                paused_chapters.append(chapter_num)
                return self._paused_result(
                    project_id,
                    requested_chapters,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                    frozen_artifacts=frozen_artifacts,
                    current_chapter=max(0, chapter_num - 1),
                )
            manual_start_checkpoint = self._manual_boundary_checkpoint(
                session,
                project_id=project_id,
                chapter_number=chapter_num,
                boundary_kind="chapter_start",
            )
            manual_start_approved = bool(
                manual_start_checkpoint is not None
                and self._resolve_checkpoint_gate(
                    updater=updater,
                    checkpoint=manual_start_checkpoint,
                    gate_kind="manual_checkpoint_chapter_start",
                    chapter_number=chapter_num,
                )
            )
            if manual_start_approved:
                session.commit()
            elif manual_start_checkpoint is not None:
                self._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    band_id=manual_start_checkpoint.band_id,
                    chapter_number=chapter_num,
                    event_family="evaluation_verdict",
                    event_type=DecisionEventType.MANUAL_CHECKPOINT_HIT,
                    scope="chapter",
                    summary="命中 chapter_start manual checkpoint，运行已暂停。",
                    related_object_type="band_checkpoint",
                    related_object_id=manual_start_checkpoint.id,
                    payload=attach_gate_outcome(
                        {},
                        chapter_execution_support.checkpoint_event_gate_outcome(
                            manual_start_checkpoint,
                            chapter_number=chapter_num,
                            policy_version=int(
                                getattr(project, "runtime_policy_version", 0) or 0
                            ),
                            decision="pause",
                            blocked=False,
                        ),
                    ),
                )
                session.commit()
                paused_chapters.append(chapter_num)
                return self._paused_result(
                    project_id,
                    requested_chapters,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                    frozen_artifacts=frozen_artifacts,
                    current_chapter=max(0, chapter_num - 1),
                )

            try:
                self._emit_progress(
                    "stage_changed",
                    stage="assembling_context",
                    project_id=project_id,
                    requested_chapters=requested_chapters,
                    current_chapter=chapter_num,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                )
                context = self.retrieval_broker.build_chapter_context(
                    repo, project_id, chapter_plan
                )
                context = self._audit_current_plan_before_write(
                    session=session,
                    repo=repo,
                    updater=updater,
                    project_id=project_id,
                    chapter_plan=chapter_plan,
                    context=context,
                    trigger_stage="pre_write",
                )
                context_summary = dict(
                    getattr(self.retrieval_broker, "last_observability_summary", {})
                    or {}
                )
                if context_summary:
                    self._record_decision_event(
                        updater=updater,
                        project_id=project_id,
                        chapter_number=chapter_num,
                        event_family="runtime_observation",
                        event_type=DecisionEventType.CONTEXT_ASSEMBLED,
                        scope="chapter",
                        summary=f"第{chapter_num}章 context 已组装。",
                        payload=context_summary,
                    )
                    if any(
                        int(context_summary.get(key) or 0) > 0
                        for key in (
                            "pruned_entities",
                            "pruned_threads",
                            "pruned_relations",
                            "pruned_memories",
                        )
                    ):
                        self._record_decision_event(
                            updater=updater,
                            project_id=project_id,
                            chapter_number=chapter_num,
                            event_family="runtime_observation",
                            event_type=DecisionEventType.CONTEXT_PRUNED,
                            scope="chapter",
                            summary=f"第{chapter_num}章 context 已按 budget 裁剪。",
                            payload=context_summary,
                        )

                if self._abort_requested():
                    return self._cancelled_result(
                        project_id,
                        requested_chapters,
                        completed_chapters=completed_chapters,
                        failed_chapters=failed_chapters,
                        paused_chapters=paused_chapters,
                        frozen_artifacts=frozen_artifacts,
                        current_chapter=chapter_num,
                    )
                if self._pause_requested():
                    return self._paused_result(
                        project_id,
                        requested_chapters,
                        completed_chapters=completed_chapters,
                        failed_chapters=failed_chapters,
                        paused_chapters=paused_chapters,
                        frozen_artifacts=frozen_artifacts,
                        current_chapter=chapter_num,
                    )
                self._emit_progress(
                    "stage_changed",
                    stage="writing_chapter",
                    project_id=project_id,
                    requested_chapters=requested_chapters,
                    current_chapter=chapter_num,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                )
                writer_output = self._write_chapter_with_attention_fallback(
                    context=context,
                    project_id=project_id,
                    chapter_number=chapter_num,
                    updater=updater,
                    paused_chapters=paused_chapters,
                    frozen_artifacts=frozen_artifacts,
                )
                if writer_output is None:
                    if self._abort_requested():
                        return self._cancelled_result(
                            project_id,
                            requested_chapters,
                            completed_chapters=completed_chapters,
                            failed_chapters=failed_chapters,
                            paused_chapters=paused_chapters,
                            frozen_artifacts=frozen_artifacts,
                            current_chapter=chapter_num,
                        )
                    session.commit()
                    break
                if self._pause_requested():
                    session.commit()
                    return self._paused_result(
                        project_id,
                        requested_chapters,
                        completed_chapters=completed_chapters,
                        failed_chapters=failed_chapters,
                        paused_chapters=paused_chapters,
                        frozen_artifacts=frozen_artifacts,
                        current_chapter=chapter_num,
                    )
                self._emit_progress(
                    "stage_changed",
                    stage="continuity_review",
                    project_id=project_id,
                    requested_chapters=requested_chapters,
                    current_chapter=chapter_num,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                )
                writer_output, verdict, force_accept_applied = (
                    self.repair.review_candidate(
                        execution=self.repair_execution,
                        session=session,
                        repo=repo,
                        updater=updater,
                        checker=checker,
                        project_id=project_id,
                        chapter_plan=chapter_plan,
                        context=context,
                        writer_output=writer_output,
                    )
                )
                repair_attempt_count = len(
                    repo.list_chapter_rewrite_attempts(project_id, chapter_num)
                )
                residual_review_issues = self._review_issue_payloads(verdict)
                canon_risk_level = self._review_canon_risk(verdict)
                session.commit()
                if self._pause_requested():
                    return self._paused_result(
                        project_id,
                        requested_chapters,
                        completed_chapters=completed_chapters,
                        failed_chapters=failed_chapters,
                        paused_chapters=paused_chapters,
                        frozen_artifacts=frozen_artifacts,
                        current_chapter=chapter_num,
                    )

                if self.policy.canon.hard_floor:
                    hard_floor = run_hard_floor(
                        writer_output=writer_output,
                        context_pack=context,
                        repo=repo,
                        project_id=project_id,
                        chapter_number=chapter_num,
                        policy=self.policy,
                    )
                    pulp_policy = evaluate_pulp_beat_policy(
                        session=session,
                        project_id=project_id,
                        chapter_number=chapter_num,
                        hard_floor_result=hard_floor,
                        policy=self.policy,
                    )
                    if pulp_policy.fatal:
                        fail_reasons = [*hard_floor.fail_reasons, pulp_policy.reason]
                        hard_floor = hard_floor.model_copy(
                            update={
                                "passed": False,
                                "fail_reasons": fail_reasons,
                                "checks": {
                                    **hard_floor.checks,
                                    pulp_policy.reason: False,
                                },
                                "metadata": {
                                    **hard_floor.metadata,
                                    "pulp_beat_policy": pulp_policy.model_dump(
                                        mode="json"
                                    ),
                                },
                            }
                        )
                    evaluated_candidate = CandidateDraftRepository(
                        session
                    ).latest_for_chapter(
                        project_id=project_id,
                        chapter_number=chapter_num,
                    )
                    hard_floor_gate_outcome = (
                        chapter_execution_support.hard_floor_gate_outcome(
                            hard_floor,
                            candidate_id=str(
                                getattr(evaluated_candidate, "id", "") or ""
                            ),
                            chapter_number=chapter_num,
                            policy_version=int(
                                getattr(evaluated_candidate, "policy_version", 0)
                                or 0
                            ),
                        )
                    )
                    chapter_execution_support.record_pulp_beat_evaluation(
                        self,
                        updater=updater,
                        project_id=project_id,
                        chapter_number=chapter_num,
                        hard_floor=hard_floor,
                        gate_outcome=hard_floor_gate_outcome,
                    )
                    if not hard_floor.passed:
                        hard_floor_issues = [
                            {
                                "reviewer": "hard_floor",
                                "rule_name": reason,
                                "severity": "error",
                                "message": f"hard floor failed: {reason}",
                            }
                            for reason in hard_floor.fail_reasons
                        ]
                        hard_floor_reason = "; ".join(hard_floor.fail_reasons)
                        summary = f"第{chapter_num}章 hard floor failed"
                        if hard_floor_reason:
                            summary = f"{summary}: {hard_floor_reason}"
                        updater.mark_chapter_status(
                            project_id,
                            chapter_num,
                            "failed",
                            repair_attempt_count=repair_attempt_count,
                            residual_review_issues=[
                                *residual_review_issues,
                                *hard_floor_issues,
                            ],
                            canon_risk_level="high",
                        )
                        self._record_decision_event(
                            updater=updater,
                            project_id=project_id,
                            chapter_number=chapter_num,
                            event_family="evaluation_verdict",
                            event_type=DecisionEventType.HARD_GATE_HIT,
                            scope="chapter",
                            summary=summary,
                            reason=hard_floor_reason,
                            payload=attach_gate_outcome(
                                hard_floor.model_dump(mode="json"),
                                hard_floor_gate_outcome,
                            ),
                        )
                        session.commit()
                        failed_chapters.append(chapter_num)
                        break

                review_gate = handle_chapter_review_gate(
                    self,
                    session=session,
                    updater=updater,
                    project_id=project_id,
                    chapter_plan=chapter_plan,
                    writer_output=writer_output,
                    verdict=verdict,
                    residual_review_issues=residual_review_issues,
                    canon_risk_level=canon_risk_level,
                    repair_attempt_count=repair_attempt_count,
                    force_accept_applied=force_accept_applied,
                    chapter_number=chapter_num,
                    last_requested_chapter=last_requested_chapter,
                    requested_chapters=requested_chapters,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                )
                if review_gate.pause_required:
                    break
                gate_approved = review_gate.gate_approved
                acceptance_mode = (
                    "gate_approved"
                    if gate_approved
                    else (
                        "force_accept_after_repair"
                        if force_accept_applied
                        else "normal"
                    )
                )
                accepted_residual_issues = (
                    residual_review_issues
                    if force_accept_applied or gate_approved
                    else []
                )

                while True:
                    self._emit_progress(
                        "stage_changed",
                        stage="applying_canon",
                        project_id=project_id,
                        requested_chapters=requested_chapters,
                        current_chapter=chapter_num,
                        completed_chapters=completed_chapters,
                        failed_chapters=failed_chapters,
                        paused_chapters=paused_chapters,
                    )
                    candidate = CandidateDraftRepository(session).latest_for_chapter(
                        project_id=project_id,
                        chapter_number=chapter_num,
                    )
                    if candidate is None:
                        canon_outcome = CanonAdmissionOutcome(
                            blocked_path="v5 candidate record not found",
                            block_kind="candidate_missing",
                        )
                    else:
                        preparation = self.canon_preparation.prepare(
                            context=self.canon_preparation_context,
                            session=session,
                            repo=repo,
                            updater=updater,
                            candidate_id=candidate.id,
                            project_id=project_id,
                            chapter_number=chapter_num,
                            writer_output=writer_output,
                            verdict=verdict,
                            acceptance_mode=acceptance_mode,
                            repair_attempt_count=repair_attempt_count,
                            residual_review_issues=accepted_residual_issues,
                            canon_risk_level=canon_risk_level,
                        )
                        if preparation.blocked or preparation.plan is None:
                            canon_outcome = CanonAdmissionOutcome(
                                blocked_path=preparation.blocked_path,
                                block_kind=preparation.block_kind,
                                canon_gate_result=preparation.canon_gate_result,
                            )
                        else:
                            session.commit()
                            if self._abort_requested():
                                return self._cancelled_result(
                                    project_id,
                                    requested_chapters,
                                    completed_chapters=completed_chapters,
                                    failed_chapters=failed_chapters,
                                    paused_chapters=paused_chapters,
                                    frozen_artifacts=frozen_artifacts,
                                    current_chapter=chapter_num,
                                )
                            canon_outcome = self.canon_admission.commit_plan(
                                preparation.plan
                            )
                            session.expire_all()
                            repo, updater, checker = self._make_state_helpers(session)
                    if not canon_outcome.blocked:
                        break
                    frozen_path = canon_outcome.blocked_path
                    gate_result = canon_outcome.canon_gate_result
                    repair_scope = (
                        _canon_repair_scope(
                            getattr(gate_result, "required_repair_scope", "")
                        )
                        if canon_outcome.block_kind == "canon_quality"
                        and gate_result is not None
                        else ""
                    )
                    can_run_canon_repair = _canon_repair_scope_can_run(repair_scope)
                    if can_run_canon_repair:
                        (
                            writer_output,
                            verdict,
                            canon_force_accept_applied,
                        ) = self.repair.repair_canon_block(
                            execution=self.repair_execution,
                            session=session,
                            repo=repo,
                            updater=updater,
                            checker=checker,
                            project_id=project_id,
                            chapter_plan=chapter_plan,
                            context=context,
                            writer_output=writer_output,
                            gate_result=gate_result,
                        )
                        force_accept_applied = (
                            force_accept_applied or canon_force_accept_applied
                        )
                        repair_attempt_count = len(
                            repo.list_chapter_rewrite_attempts(project_id, chapter_num)
                        )
                        residual_review_issues = self._review_issue_payloads(verdict)
                        canon_risk_level = self._review_canon_risk(verdict)
                        session.commit()
                        if verdict.verdict != "fail":
                            force_accept_applied = (
                                force_accept_applied or canon_force_accept_applied
                            )
                            continue
                    if (
                        canon_outcome.block_kind == "canon_quality"
                        and not can_run_canon_repair
                    ):
                        system_block_chapters.append(chapter_num)
                        gate_summary = str(
                            getattr(gate_result, "gate_summary", "")
                            or "canon quality gate blocked commit"
                        )
                        self._record_decision_event(
                            updater=updater,
                            project_id=project_id,
                            chapter_number=chapter_num,
                            event_family="evaluation_verdict",
                            event_type=DecisionEventType.CANON_COMMIT_BLOCKED,
                            scope="chapter",
                            summary=(
                                f"第{chapter_num}章 canon quality gate system_block: "
                                f"{gate_summary}"
                            ),
                            reason=gate_summary,
                            payload={
                                "outcome": "system_block",
                                "reason": gate_summary,
                                "required_repair_scope": canon_outcome.repairable_scope,
                            },
                        )
                    if frozen_path:
                        frozen_artifacts.append(frozen_path)
                    updater.mark_chapter_status(
                        project_id,
                        chapter_num,
                        "needs_review",
                        repair_attempt_count=repair_attempt_count,
                        residual_review_issues=residual_review_issues,
                        canon_risk_level="high",
                    )
                    session.commit()
                    paused_chapters.append(chapter_num)
                    self._emit_progress(
                        "stage_changed",
                        stage="paused_for_review",
                        project_id=project_id,
                        requested_chapters=requested_chapters,
                        current_chapter=chapter_num,
                        completed_chapters=completed_chapters,
                        failed_chapters=failed_chapters,
                        paused_chapters=paused_chapters,
                        frozen_artifacts=frozen_artifacts,
                    )
                    repo, updater, checker = self._make_state_helpers(session)
                    break
                if canon_outcome.blocked:
                    break
                if self._pause_requested():
                    session.commit()
                    return self._paused_result(
                        project_id,
                        requested_chapters,
                        completed_chapters=completed_chapters,
                        failed_chapters=failed_chapters,
                        paused_chapters=paused_chapters,
                        frozen_artifacts=frozen_artifacts,
                        current_chapter=chapter_num,
                    )

                save_accepted_trope_usage_for_chapter(
                    session,
                    project_id=project_id,
                    arc_id=str(getattr(chapter_plan, "arc_plan_id", "") or ""),
                    band_id="",
                    chapter_number=chapter_num,
                    experience_plan_json=str(
                        getattr(chapter_plan, "experience_plan_json", "") or "{}"
                    ),
                )
                chapter_execution_support.defer_structured_extraction_if_needed(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_num,
                    writer_output=writer_output,
                )
                self._emit_progress(
                    "stage_changed",
                    stage="running_post_acceptance",
                    project_id=project_id,
                    requested_chapters=requested_chapters,
                    current_chapter=chapter_num,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                )
                self._run_phase3_pass(
                    session=session,
                    project_id=project_id,
                    chapter_number=chapter_num,
                )
                _verify_obligations_after_acceptance(
                    self,
                    session=session,
                    project_id=project_id,
                    chapter_number=chapter_num,
                    accepted_text=writer_output.body,
                )
                future_plan_audit_result = self._audit_future_plans_after_acceptance(
                    session=session,
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_num,
                    trigger_stage="post_acceptance",
                )
                future_plan_audit_blocked = bool(
                    future_plan_audit_result is not None
                    and future_plan_audit_result.blocking_reasons
                )
                generation_audit_pause = (
                    self._record_generation_audit_checkpoint_if_due(
                        session=session,
                        updater=updater,
                        project_id=project_id,
                        chapter_number=chapter_num,
                        requested_chapters=requested_chapters,
                        last_requested_chapter=last_requested_chapter,
                        completed_chapters=completed_chapters,
                        failed_chapters=failed_chapters,
                        paused_chapters=paused_chapters,
                        future_plan_audit_result=future_plan_audit_result,
                        policy=policy,
                    )
                )
                if generation_audit_pause:
                    audit_event = (
                        session.query(DecisionEvent)
                        .filter(
                            DecisionEvent.project_id == project_id,
                            DecisionEvent.chapter_number == chapter_num,
                            DecisionEvent.event_type
                            == DecisionEventType.GENERATION_AUDIT_CHECKPOINT_REACHED,
                        )
                        .order_by(
                            DecisionEvent.created_at.desc(), DecisionEvent.id.desc()
                        )
                        .first()
                    )
                    try:
                        audit_payload = json.loads(
                            str(getattr(audit_event, "payload_json", "{}") or "{}")
                        )
                    except (json.JSONDecodeError, TypeError):
                        audit_payload = {}
                    audit_outcome = self._resolve_gate_delegation(
                        updater=updater,
                        project_id=project_id,
                        gate_kind="generation_audit_pause",
                        scope="project",
                        chapter_number=chapter_num,
                        related_object_type="decision_event",
                        related_object_id=str(getattr(audit_event, "id", "") or ""),
                        parent_event_id=str(getattr(audit_event, "id", "") or ""),
                        input_snapshot={
                            "generation_audit": audit_payload,
                            "chapter_number": chapter_num,
                            "completed_chapters": [*completed_chapters, chapter_num],
                            "failed_chapters": failed_chapters,
                            "paused_chapters": paused_chapters,
                            "runtime_policy": policy.model_dump(mode="json"),
                        },
                    )
                    if audit_outcome.approved:
                        generation_audit_pause = False
                checkpoint_row = None
                checkpoint_pause = False
                checkpoint_warn_pause = False
                if policy.pause.band_checkpoint_action != "continue":
                    try:
                        checkpoint_row = self._create_auto_band_checkpoint(
                            session=session,
                            repo=repo,
                            updater=updater,
                            project_id=project_id,
                            chapter_number=chapter_num,
                        )
                    except Exception as exc:
                        band_row = repo.get_band_row_for_chapter(
                            project_id, chapter_num
                        )
                        if band_row is None:
                            raise
                        checkpoint_row = updater.save_band_checkpoint(
                            BandCheckpointDetail(
                                project_id=project_id,
                                arc_id=band_row.arc_id,
                                band_id=band_row.band_id,
                                chapter_start=int(band_row.chapter_start or 0),
                                chapter_end=int(band_row.chapter_end or 0),
                                trigger_source="auto_band_end",
                                boundary_kind="band_end",
                                boundary_chapter=chapter_num,
                                status="error",
                                summary="band checkpoint evaluator 异常，运行已暂停。",
                                issues=[
                                    BandCheckpointIssueInfo(
                                        code="checkpoint_evaluator_error",
                                        severity="error",
                                        issue_group=issue_group_for_issue(
                                            code="runtime"
                                        ),
                                        description="checkpoint evaluator 执行失败。",
                                        detail=f"{exc.__class__.__name__}: {exc}",
                                    )
                                ],
                            )
                        )
                        self._record_decision_event(
                            updater=updater,
                            project_id=project_id,
                            band_id=band_row.band_id,
                            chapter_number=chapter_num,
                            event_family="runtime_observation",
                            event_type=DecisionEventType.CHECKPOINT_EVALUATOR_ERROR,
                            scope="band",
                            summary="band checkpoint evaluator 异常。",
                            reason=str(exc),
                            related_object_type="band_checkpoint",
                            related_object_id=checkpoint_row.id,
                            payload=attach_gate_outcome(
                                {
                                    "status": "error",
                                    "error_class": exc.__class__.__name__,
                                    "error_summary": str(exc),
                                },
                                chapter_execution_support.checkpoint_event_gate_outcome(
                                    checkpoint_row,
                                    chapter_number=chapter_num,
                                    policy_version=int(
                                        getattr(project, "runtime_policy_version", 0)
                                        or 0
                                    ),
                                    decision="error",
                                    blocked=True,
                                ),
                            ),
                        )
                    if checkpoint_row is not None and checkpoint_row.status in {
                        "fail",
                        "error",
                    }:
                        checkpoint_pause = True
                    if (
                        checkpoint_row is not None
                        and checkpoint_row.status == "warn"
                        and policy.pause.band_checkpoint_action
                        in {"pause_on_warn", "pause_always"}
                    ):
                        checkpoint_warn_pause = True
                should_pause_for_checkpoint = checkpoint_pause or (
                    checkpoint_warn_pause and chapter_num != last_requested_chapter
                )
                if (
                    (checkpoint_pause or checkpoint_warn_pause)
                    and checkpoint_row is not None
                    and self._resolve_checkpoint_gate(
                        updater=updater,
                        checkpoint=checkpoint_row,
                        gate_kind="band_checkpoint_pause",
                        chapter_number=chapter_num,
                    )
                ):
                    should_pause_for_checkpoint = False
                manual_after_accept = self._manual_boundary_checkpoint(
                    session,
                    project_id=project_id,
                    chapter_number=chapter_num,
                    boundary_kind="chapter_accepted",
                )
                manual_band_end = self._manual_boundary_checkpoint(
                    session,
                    project_id=project_id,
                    chapter_number=chapter_num,
                    boundary_kind="band_end",
                )
                if manual_after_accept is not None and self._resolve_checkpoint_gate(
                    updater=updater,
                    checkpoint=manual_after_accept,
                    gate_kind="manual_checkpoint_chapter_accepted",
                    chapter_number=chapter_num,
                ):
                    manual_after_accept = None
                if manual_band_end is not None and self._resolve_checkpoint_gate(
                    updater=updater,
                    checkpoint=manual_band_end,
                    gate_kind="manual_checkpoint_band_end",
                    chapter_number=chapter_num,
                ):
                    manual_band_end = None
                session.commit()
                if (
                    should_pause_for_checkpoint
                    or manual_after_accept is not None
                    or manual_band_end is not None
                    or future_plan_audit_blocked
                    or generation_audit_pause
                ):
                    if should_pause_for_checkpoint and checkpoint_row is not None:
                        self._record_decision_event(
                            updater=updater,
                            project_id=project_id,
                            band_id=checkpoint_row.band_id,
                            chapter_number=chapter_num,
                            event_family="evaluation_verdict",
                            event_type=DecisionEventType.BAND_CHECKPOINT_HIT,
                            scope="band",
                            summary="band checkpoint 命中阻断，运行已暂停。",
                            related_object_type="band_checkpoint",
                            related_object_id=checkpoint_row.id,
                            payload=attach_gate_outcome(
                                {"status": checkpoint_row.status},
                                chapter_execution_support.checkpoint_event_gate_outcome(
                                    checkpoint_row,
                                    chapter_number=chapter_num,
                                    policy_version=int(
                                        getattr(project, "runtime_policy_version", 0)
                                        or 0
                                    ),
                                    decision="pause",
                                    blocked=False,
                                ),
                            ),
                        )
                    if manual_after_accept is not None:
                        self._record_decision_event(
                            updater=updater,
                            project_id=project_id,
                            band_id=manual_after_accept.band_id,
                            chapter_number=chapter_num,
                            event_family="evaluation_verdict",
                            event_type=DecisionEventType.MANUAL_CHECKPOINT_HIT,
                            scope="chapter",
                            summary="命中 chapter_accepted manual checkpoint，运行已暂停。",
                            related_object_type="band_checkpoint",
                            related_object_id=manual_after_accept.id,
                            payload=attach_gate_outcome(
                                {},
                                chapter_execution_support.checkpoint_event_gate_outcome(
                                    manual_after_accept,
                                    chapter_number=chapter_num,
                                    policy_version=int(
                                        getattr(project, "runtime_policy_version", 0)
                                        or 0
                                    ),
                                    decision="pause",
                                    blocked=False,
                                ),
                            ),
                        )
                    if manual_band_end is not None:
                        self._record_decision_event(
                            updater=updater,
                            project_id=project_id,
                            band_id=manual_band_end.band_id,
                            chapter_number=chapter_num,
                            event_family="evaluation_verdict",
                            event_type=DecisionEventType.MANUAL_CHECKPOINT_HIT,
                            scope="band",
                            summary="命中 band_end manual checkpoint，运行已暂停。",
                            related_object_type="band_checkpoint",
                            related_object_id=manual_band_end.id,
                            payload=attach_gate_outcome(
                                {},
                                chapter_execution_support.checkpoint_event_gate_outcome(
                                    manual_band_end,
                                    chapter_number=chapter_num,
                                    policy_version=int(
                                        getattr(project, "runtime_policy_version", 0)
                                        or 0
                                    ),
                                    decision="pause",
                                    blocked=False,
                                ),
                            ),
                        )
                    if (
                        future_plan_audit_blocked
                        and future_plan_audit_result is not None
                    ):
                        self._record_decision_event(
                            updater=updater,
                            project_id=project_id,
                            chapter_number=chapter_num,
                            event_family="evaluation_verdict",
                            event_type=DecisionEventType.FUTURE_PLAN_AUDIT_RUN,
                            scope="project",
                            summary="future plan audit 存在未修复阻断，运行已暂停。",
                            related_object_type="future_plan_audit_run",
                            related_object_id=future_plan_audit_result.id,
                            payload={
                                "blocking_reasons": list(
                                    future_plan_audit_result.blocking_reasons
                                ),
                                "inspected_chapters": list(
                                    future_plan_audit_result.inspected_chapters
                                ),
                            },
                        )
                    session.commit()
                    completed_with_current = [*completed_chapters, chapter_num]
                    paused_chapters.append(chapter_num)
                    return self._paused_result(
                        project_id,
                        requested_chapters,
                        completed_chapters=completed_with_current,
                        failed_chapters=failed_chapters,
                        paused_chapters=paused_chapters,
                        frozen_artifacts=frozen_artifacts,
                        current_chapter=chapter_num,
                    )
                if self._pause_requested():
                    completed_chapters.append(chapter_num)
                    return self._paused_result(
                        project_id,
                        requested_chapters,
                        completed_chapters=completed_chapters,
                        failed_chapters=failed_chapters,
                        paused_chapters=paused_chapters,
                        frozen_artifacts=frozen_artifacts,
                        current_chapter=chapter_num,
                    )

            except Exception as exc:
                logger.exception("Chapter %d failed.", chapter_num)
                session.rollback()
                repo, updater, checker = self._make_state_helpers(session)
                current_plan = repo.get_chapter_plan(project_id, chapter_num)
                if current_plan is not None and current_plan.status == "accepted":
                    deferred_maintenance.record_deferred_maintenance(
                        updater,
                        deferred_maintenance.DeferredMaintenanceRecord(
                            project_id=project_id,
                            chapter_number=chapter_num,
                            task_type="post_acceptance_pipeline",
                            reason=str(exc),
                            payload={"error_class": exc.__class__.__name__},
                        ),
                    )
                    session.commit()
                    if chapter_num not in completed_chapters:
                        completed_chapters.append(chapter_num)
                    self._emit_progress(
                        "stage_changed",
                        stage="post_acceptance_deferred",
                        project_id=project_id,
                        requested_chapters=requested_chapters,
                        current_chapter=chapter_num,
                        completed_chapters=completed_chapters,
                        failed_chapters=failed_chapters,
                        paused_chapters=paused_chapters,
                    )
                    logger.warning(
                        "Chapter %d remains accepted; post-acceptance work was deferred.",
                        chapter_num,
                    )
                    break
                updater.mark_chapter_status(project_id, chapter_num, "failed")
                session.commit()
                failed_chapters.append(chapter_num)
                self._emit_progress(
                    "stage_changed",
                    stage="chapter_failed",
                    project_id=project_id,
                    requested_chapters=requested_chapters,
                    current_chapter=chapter_num,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                )
                print(f"  ✗ 第{chapter_num}章失败: {exc}")
                if isinstance(
                    exc, TransientLLMChapterFailure
                ) or self._is_transient_llm_like(exc):
                    logger.warning(
                        "Stopping run after transient LLM failure on chapter %d to avoid cascading failures.",
                        chapter_num,
                    )
                    break
                logger.warning(
                    "Stopping run after chapter %d failure.",
                    chapter_num,
                )
                break

            completed_chapters.append(chapter_num)
            self._emit_progress(
                "chapter_completed",
                project_id=project_id,
                requested_chapters=requested_chapters,
                current_chapter=chapter_num,
                completed_chapters=completed_chapters,
                failed_chapters=failed_chapters,
                paused_chapters=paused_chapters,
            )

            issue_summary = ""
            if verdict.issues:
                issue_summary = " | 问题: " + "; ".join(
                    i.description for i in verdict.issues[:3]
                )
            print(
                f"  ✓ 第{chapter_num}章 《{writer_output.title}》 "
                f"({writer_output.char_count}字) "
                f"审查: {verdict.verdict}{issue_summary}"
            )

        return RunResult(
            project_id=project_id,
            requested_chapters=requested_chapters,
            completed_chapters=completed_chapters,
            failed_chapters=failed_chapters,
            paused_chapters=paused_chapters,
            frozen_artifacts=frozen_artifacts,
            system_block_chapters=system_block_chapters,
        )


__all__ = ["ChapterExecutionStage"]
