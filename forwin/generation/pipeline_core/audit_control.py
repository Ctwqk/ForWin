from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from forwin.audit.events import (
    DecisionEventType,
)
from forwin.audit.gate_outcome import GateOutcome, attach_gate_outcome
from forwin.context.assembler_core import _build_canon_quality_context
from forwin.generation.pipeline_core.common import (
    _future_plan_audit_checkpoint_payload,
    logger,
)
from forwin.models.audit import DecisionEvent
from forwin.models.phase import BandExperiencePlan
from forwin.models.planning_control import BandCheckpoint
from forwin.models.project import ChapterPlan, Project
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.planning.checkpoints import (
    band_is_first_chapter,
    chapter_blocking_message,
)
from forwin.planning.future_plan_audit import FuturePlanAuditor, FuturePlanAuditRun
from forwin.planning.health import PlanHealthService
from forwin.planning.query import PlanningQuery
from forwin.review.decision.types import Decision, DecisionInput
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
            str(issue.issue_type)
            for issue in result.issues
            if str(issue.issue_type)
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




class AuditControlStage:
    """Owns the audit control stage behavior."""

    def _project_policy(self, session: Session, project: Project):
        _ = session, project
        return self.policy

    def _record_decision_event(
        self,
        *,
        updater: StateUpdater,
        project_id: str,
        event_family: str,
        event_type: str,
        summary: str,
        reason: str = "",
        scope: str = "project",
        actor_type: str = "system",
        actor_id: str = "",
        band_id: str = "",
        chapter_number: int = 0,
        task_id: str = "",
        related_object_type: str = "",
        related_object_id: str = "",
        payload: dict[str, Any] | None = None,
        parent_event_id: str = "",
        causal_root_id: str = "",
    ):
        return self.trace_recorder.record_event(
            updater=updater,
            project_id=project_id,
            event_family=event_family,
            event_type=event_type,
            summary=summary,
            reason=reason,
            scope=scope,
            actor_type=actor_type,
            actor_id=actor_id,
            band_id=band_id,
            chapter_number=chapter_number,
            task_id=task_id,
            related_object_type=related_object_type,
            related_object_id=related_object_id,
            payload=payload,
            parent_event_id=parent_event_id,
            causal_root_id=causal_root_id,
        )

    def _record_rule_decision_event(
        self,
        *,
        updater: StateUpdater,
        decision: Decision,
        decision_input: DecisionInput,
        related_object_type: str = "",
        related_object_id: str = "",
        parent_event_id: str = "",
    ) -> DecisionEvent | None:
        return self.trace_recorder.record_rule_decision(
            updater=updater, decision=decision, decision_input=decision_input,
            related_object_type=related_object_type, related_object_id=related_object_id,
            parent_event_id=parent_event_id,
        )

    def _audit_current_plan_before_write(
        self,
        *,
        session: Session,
        repo: StateRepository,
        updater: StateUpdater,
        project_id: str,
        chapter_plan: ChapterPlan,
        context,
        trigger_stage: str,
    ):
        project = session.get(Project, project_id)
        target_total_chapters = int(getattr(project, "target_total_chapters", 0) or 0)
        chapter_number = int(chapter_plan.chapter_number or 0)
        canon_quality_context = dict(
            getattr(context, "canon_quality_context", {}) or {}
        )
        obligation_repo = NarrativeObligationRepository(session)
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
            plans=[chapter_plan],
            canon_quality_context=canon_quality_context,
            obligations=obligation_repo.list_active_for_context(
                project_id, chapter_number=chapter_number
            ),
            target_total_chapters=target_total_chapters,
            include_current=True,
        )
        self._record_future_plan_audit_events(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            result=result,
        )
        plan_health = PlanHealthService.from_future_audit(result, scope="chapter")
        if plan_health.blocking:
            raise RuntimeError(
                "future_plan_audit_blocked:" + ";".join(plan_health.reasons)
            )
        if result.applied_plan_patch_ids:
            session.flush()
            return self.retrieval_broker.build_chapter_context(
                repo, project_id, chapter_plan
            )
        return context

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
                    summary=(
                        f"已记录第 {accepted_count} 个已接受章节的生成审计摘要。"
                    ),
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
        except Exception:
            logger.exception("generation audit report failed for project %s", project_id)

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

    def _previous_band_row(
        self,
        session: Session,
        *,
        project_id: str,
        current_start: int,
    ) -> BandExperiencePlan | None:
        active_arc = StateRepository(session).get_active_arc_plan(project_id)
        if active_arc is None:
            return None
        return (
            session.query(BandExperiencePlan)
            .filter(
                BandExperiencePlan.project_id == project_id,
                BandExperiencePlan.arc_id == active_arc.id,
                BandExperiencePlan.chapter_end < current_start,
            )
            .order_by(
                BandExperiencePlan.chapter_end.desc(),
                BandExperiencePlan.created_at.desc(),
            )
            .populate_existing()
            .first()
        )

    def _manual_boundary_checkpoint(
        self,
        session: Session,
        *,
        project_id: str,
        chapter_number: int,
        boundary_kind: str,
    ) -> BandCheckpoint | None:
        return (
            session.query(BandCheckpoint)
            .filter(
                BandCheckpoint.project_id == project_id,
                BandCheckpoint.trigger_source == "manual_boundary",
                BandCheckpoint.status == "pending",
                BandCheckpoint.boundary_kind == boundary_kind,
                BandCheckpoint.boundary_chapter == chapter_number,
            )
            .order_by(BandCheckpoint.created_at.desc(), BandCheckpoint.id.desc())
            .first()
        )

    def _strict_progression_block(
        self,
        *,
        session: Session,
        repo: StateRepository,
        updater: StateUpdater | None = None,
        project: Project,
        chapter_number: int,
    ) -> tuple[str, str, str]:
        from forwin.canon.projection_lock import lock_projection_project

        lock_projection_project(session, project.id)
        policy = self._project_policy(session, project)
        if chapter_number > 1:
            previous_plan = repo.get_chapter_plan(project.id, chapter_number - 1)
            if previous_plan is not None and previous_plan.status != "accepted":
                return (
                    "chapter_not_canon",
                    "",
                    chapter_blocking_message(
                        "chapter_not_canon", chapter_number=chapter_number - 1
                    ),
                )
        band_row = repo.get_band_row_for_chapter(project.id, chapter_number)
        if band_row is None or not band_is_first_chapter(
            band_row.chapter_start, chapter_number
        ):
            return "", "", ""
        previous_band = self._previous_band_row(
            session,
            project_id=project.id,
            current_start=int(band_row.chapter_start or 0),
        )
        if previous_band is None:
            return "", "", ""
        latest_checkpoint = repo.get_latest_band_checkpoint(
            project.id, band_id=previous_band.band_id
        )
        if (
            latest_checkpoint is None
            and policy.pause.band_checkpoint_action == "continue"
        ):
            return "", "", ""
        evaluator = BandCheckpointEvaluator(session)
        evidence = evaluator.inspect(latest_checkpoint)
        if (
            not evidence.current
            and (
                latest_checkpoint is not None
                or policy.pause.band_checkpoint_action != "continue"
            )
            and updater is not None
        ):
            latest_checkpoint = self._create_auto_band_checkpoint(
                session=session,
                repo=repo,
                updater=updater,
                project_id=project.id,
                chapter_number=int(previous_band.chapter_end or 0),
            )
        if latest_checkpoint is None:
            return (
                "band_checkpoint_pending",
                previous_band.band_id,
                chapter_blocking_message(
                    "band_checkpoint_pending", band_id=previous_band.band_id
                ),
            )
        checkpoint_status = evaluator.inspect(latest_checkpoint).effective_status
        if checkpoint_status in {"pass", "overridden"}:
            return "", "", ""
        if (
            checkpoint_status == "warn"
            and policy.pause.band_checkpoint_action == "continue"
        ):
            return "", "", ""
        code = {
            "pending": "band_checkpoint_pending",
            "warn": "band_checkpoint_warn",
            "fail": "band_checkpoint_fail",
            "error": "band_checkpoint_fail",
        }.get(checkpoint_status, "band_checkpoint_pending")
        return (
            code,
            previous_band.band_id,
            chapter_blocking_message(code, band_id=previous_band.band_id),
        )

    def _create_auto_band_checkpoint(
        self,
        *,
        session,
        repo,
        updater,
        project_id,
        chapter_number,
    ):
        return BandCheckpointEvaluator(session).refresh(
            project_id=project_id,
            chapter_number=chapter_number,
            audit=self.trace_recorder.audit,
        )


__all__ = ["AuditControlStage"]
