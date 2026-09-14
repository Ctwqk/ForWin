from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from forwin.audit.events import (
    DecisionEventType,
)
from forwin.audit.gate_outcome import GateOutcome, attach_gate_outcome
from forwin.context.assembler_core import _build_canon_quality_context
from forwin.generation.pipeline_core import chapter_execution_support
from forwin.generation.pipeline_core.common import (
    _future_plan_audit_checkpoint_payload,
    logger,
)
from forwin.generation.pipeline_core.obligation_resolution import (
    _verify_obligations_after_acceptance,
)
from forwin.models.audit import DecisionEvent
from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.models.planning_control import BandCheckpoint
from forwin.models.project import ChapterPlan, Project
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.planning.checkpoints import (
    BandCheckpointDetail,
    BandCheckpointIssueInfo,
)
from forwin.planning.future_plan_audit import FuturePlanAuditor, FuturePlanAuditRun
from forwin.planning.health import PlanHealthService
from forwin.planning.query import PlanningQuery
from forwin.review.issue_groups import issue_group_for_issue
from forwin.review.plan_checks import (
    BandCheckpointEvaluator,
)
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater


def _future_plan_gate_outcome(
    result: FuturePlanAuditRun,
    *,
    plan_health,
) -> GateOutcome:
    issue_keys = list(
        dict.fromkeys(
            str(issue.issue_type) for issue in result.issues if str(issue.issue_type)
        )
    )
    issue_groups = list(
        dict.fromkeys(
            str(issue.metadata.get("issue_group") or "")
            or issue_group_for_issue(issue_type=str(issue.issue_type))
            for issue in result.issues
            if str(issue.issue_type)
        )
    )
    if plan_health.blocking:
        decision = "block"
    elif plan_health.severity == "warn" or result.issues:
        decision = "warn"
    else:
        decision = "pass"
    return GateOutcome(
        gate_id="future_plan_audit",
        responsibility_domain="future_plan_integrity",
        scope="project",
        candidate_id=str(result.id or ""),
        chapter_number=int(result.current_chapter or 0),
        fired=bool(result.issues or result.blocking_reasons),
        decision=decision,
        blocked=bool(plan_health.blocking),
        issue_keys=issue_keys,
        issue_groups=[group for group in issue_groups if group],
        evidence_refs=list(plan_health.evidence),
    )


class PostCanonOrderControls:
    """Post-acceptance checks shared by pipeline and durable phase3 recovery."""

    def __init__(
        self,
        *,
        policy=None,
        llm_client=None,
        trace_recorder=None,
        record_event=None,
        checkpoint_evaluator=None,
        resolve_checkpoint=None,
    ):
        self.policy = policy
        self.llm_client = llm_client
        self.trace_recorder = trace_recorder
        self._record_decision_event = record_event or trace_recorder.record_event
        self._checkpoint_evaluator = checkpoint_evaluator
        self._resolve_checkpoint = resolve_checkpoint

    def _project_policy(self, session, project):
        return self.policy

    def _create_auto_band_checkpoint(self, **kwargs):
        if self._checkpoint_evaluator is not None:
            return self._checkpoint_evaluator(**kwargs)
        return BandCheckpointEvaluator(kwargs["session"]).refresh(
            project_id=kwargs["project_id"],
            chapter_number=kwargs["chapter_number"],
            audit=self.trace_recorder.audit,
        )

    def _audit_future_plans_after_acceptance(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project_id: str,
        chapter_number: int,
        trigger_stage: str = "post_acceptance",
    ) -> FuturePlanAuditRun | None:
        project = session.get(Project, project_id)
        target_total_chapters = int(getattr(project, "target_total_chapters", 0) or 0)
        planning_query = PlanningQuery()
        plans = planning_query.future_chapters(
            session,
            project_id=project_id,
            current_chapter=chapter_number,
            include_current=False,
        )
        band_rows = planning_query.future_bands(
            session,
            project_id=project_id,
            current_chapter=chapter_number,
        )
        if not plans and not band_rows:
            return None
        obligation_repo = NarrativeObligationRepository(session)
        obligations = [
            *obligation_repo.list_active_for_context(
                project_id, chapter_number=chapter_number + 1
            ),
            *obligation_repo.list_planned_for_chapter(
                project_id, origin_chapter_number=chapter_number
            ),
        ]
        canon_quality_context = _build_canon_quality_context(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number + 1,
            target_total_chapters=target_total_chapters,
        )
        result = FuturePlanAuditor(
            mode="chapter_review_form",
            plan_patch_validation_mode="chapter_review_form",
            llm_client=self.llm_client,
            min_blocking_confidence=0.8,
        ).audit_and_apply(
            session=session,
            project_id=project_id,
            current_chapter=chapter_number,
            trigger_stage=trigger_stage,
            plans=plans,
            canon_quality_context=canon_quality_context,
            obligations=obligations,
            target_total_chapters=target_total_chapters,
            include_current=False,
            band_rows=band_rows,
        )
        self._record_future_plan_audit_events(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            result=result,
        )
        return result

    def _record_future_plan_audit_events(
        self,
        *,
        updater: StateUpdater,
        project_id: str,
        chapter_number: int,
        result: FuturePlanAuditRun,
    ) -> None:
        if not result.inspected_chapters:
            return
        plan_health = PlanHealthService.from_future_audit(result)
        event = self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="evaluation_verdict",
            event_type=DecisionEventType.FUTURE_PLAN_AUDIT_RUN,
            scope="project",
            summary=f"future plan audit: {result.status}",
            related_object_type="future_plan_audit_run",
            related_object_id=result.id,
            payload=attach_gate_outcome(
                {
                    **result.model_dump(mode="json", exclude={"plan_patches"}),
                    "plan_health": plan_health.model_dump(mode="json"),
                },
                _future_plan_gate_outcome(result, plan_health=plan_health),
            ),
        )
        if result.applied_plan_patch_ids:
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="audit_action",
                event_type=DecisionEventType.FUTURE_PLAN_PATCH_APPLIED,
                scope="project",
                summary=f"已应用 {len(result.applied_plan_patch_ids)} 个 future plan patch。",
                related_object_type="future_plan_audit_run",
                related_object_id=result.id,
                parent_event_id=str(event.id or ""),
                payload={
                    "applied_plan_patch_ids": list(result.applied_plan_patch_ids),
                    "issue_types": [issue.issue_type for issue in result.issues],
                },
            )

    def _record_generation_audit_report_if_due(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project_id: str,
        chapter_number: int,
        future_plan_audit_result: FuturePlanAuditRun | None,
    ) -> None:
        session.flush()
        try:
            with session.begin_nested():
                cadence = 6
                accepted_count = (
                    session.query(ChapterPlan)
                    .filter(
                        ChapterPlan.project_id == project_id,
                        ChapterPlan.status == "accepted",
                    )
                    .count()
                )
                if accepted_count <= 0 or accepted_count % cadence != 0:
                    return

                report_identity = f"{project_id}:accepted:{accepted_count}"
                existing = (
                    session.query(DecisionEvent)
                    .filter(
                        DecisionEvent.project_id == project_id,
                        DecisionEvent.event_type
                        == DecisionEventType.GENERATION_AUDIT_CHECKPOINT_REACHED,
                        DecisionEvent.related_object_type
                        == "generation_audit_checkpoint",
                        DecisionEvent.related_object_id == report_identity,
                    )
                    .first()
                )
                if existing is not None:
                    return
                payload = self._generation_audit_report_payload(
                    session=session,
                    project_id=project_id,
                    accepted_count=accepted_count,
                    cadence=cadence,
                    future_plan_audit_result=future_plan_audit_result,
                )
                self._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    event_family="runtime_observation",
                    event_type=DecisionEventType.GENERATION_AUDIT_CHECKPOINT_REACHED,
                    scope="project",
                    summary=(f"已记录第 {accepted_count} 个已接受章节的生成审计摘要。"),
                    related_object_type="generation_audit_checkpoint",
                    related_object_id=report_identity,
                    payload=attach_gate_outcome(
                        payload,
                        GateOutcome(
                            gate_id="generation_audit",
                            gate_version="report-only-v1",
                            responsibility_domain="generation_operations",
                            scope="project",
                            candidate_id=f"generation-audit:{report_identity}",
                            chapter_number=chapter_number,
                            evaluated=False,
                            fired=False,
                            decision="pass",
                            blocked=False,
                        ),
                    ),
                )
        except Exception:  # noqa: BLE001 - existing report-only audit boundary
            logger.exception(
                "generation audit report failed for project %s", project_id
            )

    def _generation_audit_report_payload(
        self,
        *,
        session: Session,
        project_id: str,
        accepted_count: int,
        cadence: int,
        future_plan_audit_result: FuturePlanAuditRun | None,
    ) -> dict[str, Any]:
        accepted_window = (
            session.query(ChapterPlan)
            .filter(
                ChapterPlan.project_id == project_id,
                ChapterPlan.status == "accepted",
            )
            .order_by(ChapterPlan.chapter_number.desc())
            .limit(cadence)
            .all()
        )
        accepted_window.reverse()
        accepted_chapter_window = [
            int(plan.chapter_number or 0) for plan in accepted_window
        ]
        window_start = min(accepted_chapter_window, default=0)
        window_end = max(accepted_chapter_window, default=0)
        plans = (
            session.query(ChapterPlan)
            .filter(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number >= window_start,
                ChapterPlan.chapter_number <= window_end,
            )
            .order_by(ChapterPlan.chapter_number.asc())
            .all()
        )
        status_by_chapter = {
            str(int(plan.chapter_number or 0)): str(plan.status or "") for plan in plans
        }
        accepted_chapters = [
            int(plan.chapter_number or 0)
            for plan in plans
            if str(plan.status or "") == "accepted"
        ]
        needs_review_chapters = [
            int(plan.chapter_number or 0)
            for plan in plans
            if str(plan.status or "") == "needs_review"
        ]
        failed_window_chapters = [
            int(plan.chapter_number or 0)
            for plan in plans
            if str(plan.status or "") == "failed"
        ]
        high_risk_chapters = [
            int(plan.chapter_number or 0)
            for plan in plans
            if str(plan.canon_risk_level or "") == "high"
        ]
        repair_attempts_by_chapter = {
            str(int(plan.chapter_number or 0)): int(plan.repair_attempt_count or 0)
            for plan in plans
            if int(plan.repair_attempt_count or 0) > 0
        }
        residual_issue_count_by_chapter: dict[str, int] = {}
        for plan in plans:
            raw_issues = str(plan.residual_review_issues_json or "[]")
            try:
                parsed_issues = json.loads(raw_issues)
            except (json.JSONDecodeError, TypeError):
                parsed_issues = []
            if isinstance(parsed_issues, list) and parsed_issues:
                residual_issue_count_by_chapter[str(int(plan.chapter_number or 0))] = (
                    len(parsed_issues)
                )
        return {
            "accepted_count": accepted_count,
            "cadence": cadence,
            "accepted_chapter_window": accepted_chapter_window,
            "window_start": window_start,
            "window_end": window_end,
            "failed_chapters": sorted({int(item) for item in failed_window_chapters}),
            "accepted_chapters": accepted_chapters,
            "needs_review_chapters": needs_review_chapters,
            "status_by_chapter": status_by_chapter,
            "high_risk_chapters": high_risk_chapters,
            "repair_attempts_by_chapter": repair_attempts_by_chapter,
            "residual_issue_count_by_chapter": residual_issue_count_by_chapter,
            "future_plan_audit": _future_plan_audit_checkpoint_payload(
                future_plan_audit_result
            ),
        }

    def run(
        self, control_session: Session, commit, *, trigger_stage="post_acceptance"
    ) -> dict[str, Any]:
        repo, updater = StateRepository(control_session), StateUpdater(control_session)
        candidate = control_session.get(CandidateDraftRecord, commit.candidate_id)
        if candidate is None:
            raise ValueError("post-Canon candidate is unavailable")
        draft = control_session.get(ChapterDraft, candidate.candidate_draft_id)
        if draft is None:
            raise ValueError("accepted post-Canon draft is unavailable")
        _verify_obligations_after_acceptance(
            self,
            session=control_session,
            project_id=commit.project_id,
            chapter_number=commit.chapter_number,
            accepted_text=str(draft.body_text or ""),
        )
        future_audit = self._audit_future_plans_after_acceptance(
            session=control_session,
            updater=updater,
            project_id=commit.project_id,
            chapter_number=commit.chapter_number,
            trigger_stage=trigger_stage,
        )
        self._record_generation_audit_report_if_due(
            session=control_session,
            updater=updater,
            project_id=commit.project_id,
            chapter_number=commit.chapter_number,
            future_plan_audit_result=future_audit,
        )
        checkpoint = self._run_post_canon_band_checkpoint(
            session=control_session,
            repo=repo,
            updater=updater,
            project_id=commit.project_id,
            chapter_number=commit.chapter_number,
        )
        result = {
            "project_id": commit.project_id,
            "chapter_number": int(commit.chapter_number or 0),
            "trigger_stage": trigger_stage,
            "blocking_reasons": (
                list(future_audit.blocking_reasons) if future_audit is not None else []
            ),
            "future_plan_audit": (
                future_audit.model_dump(mode="json")
                if future_audit is not None
                else None
            ),
            "checkpoint": checkpoint,
        }

        result["checkpoint_delegation_pending"] = self.requires_delegation(result)
        return result

    def requires_delegation(self, result):
        checkpoint = result.get("checkpoint")
        return bool(
            self._resolve_checkpoint is not None
            and not result.get("checkpoint_delegation_completed")
            and self.policy.pause.gate_delegate == "spark"
            and self.policy.pause.band_checkpoint_action
            in {"pause_on_warn", "pause_always"}
            and isinstance(checkpoint, dict)
            and checkpoint.get("status") == "warn"
            and not result.get("blocking_reasons")
        )

    def finalize(self, session, commit, result):
        resume_checkpoint_delegation(
            session=session,
            project_id=commit.project_id,
            chapter_number=commit.chapter_number,
            controls=result,
            policy=self.policy,
            resolve_checkpoint=lambda **request: self._resolve_checkpoint(
                **request, retry_on_failure=True, release_audit_locks=True
            ),
        )
        result.pop("checkpoint_delegation_pending", None)
        result["checkpoint_delegation_completed"] = True

    def _run_post_canon_band_checkpoint(
        self,
        *,
        session: Session,
        repo,
        updater,
        project_id: str,
        chapter_number: int,
    ) -> dict[str, Any] | None:
        project = repo.get_project(project_id)
        if project is None:
            raise ValueError(f"Project not found: {project_id}")
        policy = self._project_policy(session, project)
        if policy.pause.band_checkpoint_action == "continue":
            return None
        try:
            checkpoint = self._create_auto_band_checkpoint(
                session=session,
                repo=repo,
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
            )
        except Exception as exc:
            band_row = repo.get_band_row_for_chapter(project_id, chapter_number)
            if band_row is None:
                raise
            checkpoint = updater.save_band_checkpoint(
                BandCheckpointDetail(
                    project_id=project_id,
                    arc_id=band_row.arc_id,
                    band_id=band_row.band_id,
                    chapter_start=int(band_row.chapter_start or 0),
                    chapter_end=int(band_row.chapter_end or 0),
                    trigger_source="auto_band_end",
                    boundary_kind="band_end",
                    boundary_chapter=chapter_number,
                    status="error",
                    summary="band checkpoint evaluator exception; run paused.",
                    issues=[
                        BandCheckpointIssueInfo(
                            code="checkpoint_evaluator_error",
                            severity="error",
                            issue_group=issue_group_for_issue(code="runtime"),
                            description="band checkpoint evaluator failed.",
                            detail=f"{exc.__class__.__name__}: {exc}",
                        )
                    ],
                )
            )
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                band_id=band_row.band_id,
                chapter_number=chapter_number,
                event_family="runtime_observation",
                event_type=DecisionEventType.CHECKPOINT_EVALUATOR_ERROR,
                scope="band",
                summary="band checkpoint evaluator failed.",
                reason=str(exc),
                related_object_type="band_checkpoint",
                related_object_id=checkpoint.id,
                payload=attach_gate_outcome(
                    {
                        "status": "error",
                        "error_class": exc.__class__.__name__,
                        "error_summary": str(exc),
                    },
                    chapter_execution_support.checkpoint_event_gate_outcome(
                        checkpoint,
                        chapter_number=chapter_number,
                        policy_version=int(
                            getattr(project, "runtime_policy_version", 0) or 0
                        ),
                        decision="error",
                        blocked=True,
                    ),
                ),
            )
        if checkpoint is None:
            return None
        return {
            "id": checkpoint.id,
            "band_id": checkpoint.band_id,
            "status": checkpoint.status,
        }


def resume_checkpoint_delegation(
    *, session, project_id, chapter_number, controls, policy, resolve_checkpoint
):
    if controls.get("checkpoint_delegation_completed"):
        return
    checkpoint_payload = controls.get("checkpoint")
    if not isinstance(checkpoint_payload, dict):
        return
    checkpoint_id = str(checkpoint_payload.get("id") or "").strip()
    if not checkpoint_id:
        return
    checkpoint = session.get(BandCheckpoint, checkpoint_id)
    if checkpoint is None:
        return
    if checkpoint.project_id != project_id:
        raise ValueError("post-Canon checkpoint project mismatch")
    status = str(checkpoint.status or "")
    should_resolve = status in {"fail", "error"} or (
        status == "warn"
        and policy.pause.band_checkpoint_action in {"pause_on_warn", "pause_always"}
    )
    if should_resolve:
        resolve_checkpoint(
            updater=StateUpdater(session),
            checkpoint=checkpoint,
            gate_kind="band_checkpoint_pause",
            chapter_number=chapter_number,
        )


def recover_order_controls(
    maintenance, *, commit_id, llm_client_factory, gate_delegation_factory=None
):
    """Recover using the accepted candidate's original task policy, never current defaults."""
    from forwin.generation.task_payload import payload_from_json
    from forwin.models.canon import CanonCommitRecord
    from forwin.models.task import GenerationTask
    from forwin.observability.pipeline_trace import (
        PipelineAuditContext,
        PipelineTraceRecorder,
    )

    with maintenance.session_factory() as session:
        commit = session.get(CanonCommitRecord, commit_id)
        candidate = (
            session.get(CandidateDraftRecord, commit.candidate_id) if commit else None
        )
        try:
            provenance = (
                json.loads(candidate.metadata_json or "{}") if candidate else {}
            )
            task_id = provenance.get("generation_task_id", "")
            task = session.get(GenerationTask, task_id) if task_id else None
            payload = payload_from_json(task.execution_payload_json) if task else None
        except (ValueError, TypeError, AttributeError):
            task, payload = None, None
        if (
            commit is None
            or candidate is None
            or task is None
            or payload is None
            or task.project_id != commit.project_id
            or candidate.project_id != commit.project_id
            or payload.policy_version != candidate.policy_version
        ):
            return maintenance.run_order_controls(
                canon_commit_id=commit_id,
                runner=lambda session, commit: {
                    "blocking_reasons": ["post_canon_policy_provenance_missing"],
                },
            )
        policy = payload.policy_snapshot
    llm_client = llm_client_factory(policy)
    try:
        recorder = PipelineTraceRecorder(
            audit=PipelineAuditContext(task_id=task_id),
            artifact_store=maintenance.artifact_store,
            observability=None,
        )
        from forwin.generation.gate_delegation import (
            GateDelegationService,
            SparkGateDelegate,
        )
        from forwin.generation.pipeline_core.gate_delegation import (
            CheckpointGateResolver,
        )

        resolver = CheckpointGateResolver(
            session_factory=maintenance.session_factory,
            policy=policy,
            gate_delegation=(
                gate_delegation_factory(llm_client)
                if gate_delegation_factory
                else GateDelegationService(
                    spark_delegate=SparkGateDelegate(llm_client=llm_client)
                )
            ),
            audit=recorder.audit,
        )
        owner = PostCanonOrderControls(
            policy=policy,
            llm_client=llm_client,
            trace_recorder=recorder,
            resolve_checkpoint=resolver._resolve_checkpoint_gate,
        )
        return maintenance.run_order_controls(
            canon_commit_id=commit_id,
            trace_client=llm_client,
            runner=lambda session, commit: owner.run(
                session, commit, trigger_stage="continuation_preflight"
            ),
            finalizer=owner.finalize,
            requires_delegation=owner.requires_delegation,
        )
    finally:
        llm_client.close()
