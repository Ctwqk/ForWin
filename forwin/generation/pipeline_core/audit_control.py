from __future__ import annotations

from forwin.context.assembler_core import _build_canon_quality_context
from forwin.generation.pipeline_core.common import (
    _future_plan_audit_checkpoint_payload,
    logger,
)
from typing import Any
from forwin.planning.checkpoints import BandCheckpointIssueInfo
from forwin.review.issue_groups import issue_group_for_issue
from forwin.models.project import ChapterPlan
from forwin.models.audit import DecisionEvent
from forwin.planning.future_plan_audit import FuturePlanAuditRun
import json
from forwin.review.plan_checks import (
    band_combined_text,
    evaluate_band_obligation_contract,
    evaluate_constraint_issues,
    evaluate_director_imbalance,
    evaluate_intra_band_consistency,
    evaluate_next_band_task_compatibility,
    evaluate_resource_closure_risk,
    evaluate_task_contract,
)
from forwin.planning.checkpoints import (
    band_is_first_chapter,
    BandCheckpointDetail,
    chapter_blocking_message,
)
from forwin.audit.events import (
    DecisionEventInfo,
    DecisionEventType,
    ensure_decision_event_type,
)
from forwin.audit.gate_outcome import GateOutcome, attach_gate_outcome
from forwin.models.planning_control import BandCheckpoint
from forwin.protocol.experience import BandDelightSchedule
from forwin.models.phase import BandExperiencePlan
from forwin.models.draft import (
    ChapterDraft,
    ChapterReview,
)
from forwin.planning.future_plan_audit import FuturePlanAuditor
from forwin.models.project import Project
from sqlalchemy.orm import Session
from forwin.state.updater import StateUpdater
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.planning.health import PlanHealthService
from forwin.planning.query import PlanningQuery
from forwin.review.decision.audit import (
    build_decision_event_payload,
    digest_decision_input,
)
from forwin.review.decision.types import Decision, DecisionInput
from forwin.state.repo import StateRepository


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


def _band_checkpoint_gate_outcome(
    *,
    project_id: str,
    checkpoint_id: str,
    band_id: str,
    chapter_number: int,
    status: str,
    issues: list[BandCheckpointIssueInfo],
    policy_version: int = 0,
) -> GateOutcome:
    normalized_status = str(status or "error")
    decision = {
        "pass": "pass",
        "warn": "warn",
        "pending": "warn",
        "fail": "block",
        "error": "error",
        "overridden": "approve",
    }.get(normalized_status, "error")
    issue_keys = list(
        dict.fromkeys(str(issue.code) for issue in issues if str(issue.code))
    )
    issue_groups = list(
        dict.fromkeys(
            str(issue.issue_group or "")
            or issue_group_for_issue(code=str(issue.code or ""))
            for issue in issues
            if str(issue.code or "")
        )
    )
    return GateOutcome(
        gate_id="band_checkpoint",
        responsibility_domain="band_integrity",
        scope="band",
        candidate_id=checkpoint_id,
        chapter_number=chapter_number,
        band_id=band_id,
        policy_version=policy_version,
        fired=normalized_status != "pass",
        decision=decision,
        blocked=normalized_status in {"fail", "error"},
        issue_keys=issue_keys,
        issue_groups=[group for group in issue_groups if group],
        evidence_refs=list(
            dict.fromkeys(str(issue.detail) for issue in issues if str(issue.detail))
        ),
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
        row = updater.save_decision_event(
            DecisionEventInfo(
                project_id=project_id,
                task_id=task_id or self._audit_task_id,
                band_id=band_id,
                chapter_number=chapter_number,
                scope=scope,
                event_family=event_family,
                event_type=ensure_decision_event_type(event_type),
                actor_type=actor_type,
                actor_id=actor_id,
                summary=summary,
                reason=reason,
                payload=payload or {},
                related_object_type=related_object_type,
                related_object_id=related_object_id,
                parent_event_id=parent_event_id,
                causal_root_id=causal_root_id or self._audit_root_event_id,
            )
        )
        if not self._audit_root_event_id:
            self._audit_root_event_id = str(row.causal_root_id or row.id or "")
        return row

    def _record_rule_decision_event(
        self,
        *,
        updater: StateUpdater,
        decision: Decision,
        decision_input: DecisionInput,
        related_object_type: str = "",
        related_object_id: str = "",
        parent_event_id: str = "",
    ) -> None:
        try:
            payload = build_decision_event_payload(
                decision=decision,
                input_digest=digest_decision_input(decision_input),
            )
            self._record_decision_event(
                updater=updater,
                project_id=decision_input.project_id,
                chapter_number=decision_input.chapter_number,
                event_family="evaluation_verdict",
                event_type=DecisionEventType.RULE_DECISION_EVALUATED,
                scope="chapter",
                summary=f"engine decided {decision.outcome} via {decision.rule_id}",
                reason=decision.reason,
                related_object_type=related_object_type,
                related_object_id=related_object_id,
                payload=payload,
                parent_event_id=parent_event_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to record rule decision event project=%s chapter=%s rule=%s: %s",
                decision_input.project_id,
                decision_input.chapter_number,
                decision.rule_id,
                exc,
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
            and policy.pause.band_checkpoint_action != "continue"
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
        checkpoint_status = str(latest_checkpoint.status or "")
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
        session: Session,
        repo: StateRepository,
        updater: StateUpdater,
        project_id: str,
        chapter_number: int,
    ) -> BandCheckpoint | None:
        active_arc = repo.get_active_arc_plan(project_id)
        band_row = None
        if active_arc is not None:
            band_row = (
                session.query(BandExperiencePlan)
                .filter(
                    BandExperiencePlan.project_id == project_id,
                    BandExperiencePlan.arc_id == active_arc.id,
                    BandExperiencePlan.chapter_start <= chapter_number,
                    BandExperiencePlan.chapter_end == chapter_number,
                )
                .order_by(
                    BandExperiencePlan.created_at.desc(), BandExperiencePlan.id.desc()
                )
                .first()
            )
        if band_row is None:
            band_row = repo.get_band_row_for_chapter(project_id, chapter_number)
        if band_row is None or int(band_row.chapter_end or 0) != chapter_number:
            return None
        existing_boundary_checkpoint = (
            session.query(BandCheckpoint)
            .filter(
                BandCheckpoint.project_id == project_id,
                BandCheckpoint.band_id == band_row.band_id,
                BandCheckpoint.trigger_source == "auto_band_end",
                BandCheckpoint.boundary_kind == "band_end",
                BandCheckpoint.boundary_chapter == chapter_number,
            )
            .order_by(BandCheckpoint.created_at.desc(), BandCheckpoint.id.desc())
            .first()
        )
        if existing_boundary_checkpoint is not None and str(
            existing_boundary_checkpoint.status or ""
        ) in {"pending", "warn", "fail", "error"}:
            return existing_boundary_checkpoint
        band_plans = (
            session.query(ChapterPlan)
            .filter(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number >= int(band_row.chapter_start or 0),
                ChapterPlan.chapter_number <= int(band_row.chapter_end or 0),
            )
            .order_by(ChapterPlan.chapter_number.asc())
            .all()
        )
        unresolved = [
            row
            for row in repo.list_band_checkpoints(project_id, band_id=band_row.band_id)
            if row.status == "pending"
        ]
        constraints_enabled = (
            bool(repo.future_constraints_enabled(project_id))
            if hasattr(repo, "future_constraints_enabled")
            else True
        )
        issues: list[BandCheckpointIssueInfo] = []
        status = "pass"
        chapter_bodies: list[str] = []
        chapter_summaries: list[str] = []
        unresolved_review_chapters: list[int] = []
        review_fail_chapters: list[int] = []
        review_metas: list[dict[str, Any]] = []
        for plan in band_plans:
            if str(plan.status or "") == "needs_review":
                unresolved_review_chapters.append(int(plan.chapter_number or 0))
            latest_draft = (
                session.query(ChapterDraft)
                .filter(ChapterDraft.chapter_plan_id == plan.id)
                .order_by(ChapterDraft.version.desc())
                .first()
            )
            if latest_draft is None:
                continue
            chapter_bodies.append(str(latest_draft.body_text or ""))
            chapter_summaries.append(str(latest_draft.summary or ""))
            latest_review = (
                session.query(ChapterReview)
                .filter(ChapterReview.draft_id == latest_draft.id)
                .order_by(ChapterReview.created_at.desc(), ChapterReview.id.desc())
                .first()
            )
            if latest_review is not None and str(latest_review.verdict or "") == "fail":
                review_fail_chapters.append(int(plan.chapter_number or 0))
            if latest_review is not None:
                try:
                    review_meta = (
                        json.loads(latest_review.review_meta_json or "{}") or {}
                    )
                except (json.JSONDecodeError, TypeError):
                    review_meta = {}
                try:
                    review_issues = json.loads(latest_review.issues_json or "[]") or []
                except (json.JSONDecodeError, TypeError):
                    review_issues = []
                if isinstance(review_meta, dict):
                    review_meta["chapter_number"] = int(plan.chapter_number or 0)
                    review_meta["issue_types"] = [
                        str(item.get("issue_type") or item.get("rule_name") or "")
                        for item in review_issues
                        if isinstance(item, dict)
                    ]
                    review_metas.append(review_meta)
        if any(plan.status != "accepted" for plan in band_plans):
            status = "fail"
            issues.append(
                BandCheckpointIssueInfo(
                    code="band_not_fully_accepted",
                    severity="error",
                    issue_group=issue_group_for_issue(code="intra_band_consistency"),
                    description="band 内仍有章节未 accepted。",
                )
            )
        if unresolved:
            status = "warn" if status == "pass" else status
            issues.append(
                BandCheckpointIssueInfo(
                    code="pending_checkpoint_exists",
                    severity="warning",
                    issue_group=issue_group_for_issue(code="intra_band_consistency"),
                    description="同 band 仍存在未处理 checkpoint。",
                )
            )
        intra_band_issues = evaluate_intra_band_consistency(
            unresolved_review_chapters=unresolved_review_chapters,
            review_fail_chapters=review_fail_chapters,
            pending_checkpoint_count=len(unresolved),
            reviewer="plan_control",
            target_scope="band",
        )
        for issue in intra_band_issues:
            issues.append(
                BandCheckpointIssueInfo(
                    code="intra_band_consistency",
                    severity=issue.severity,
                    issue_group=issue.issue_group,
                    description=issue.description,
                    detail="; ".join(issue.evidence_refs),
                )
            )
        combined_text = band_combined_text(
            chapter_bodies=chapter_bodies,
            chapter_summaries=chapter_summaries,
        )
        band_task_issues = evaluate_task_contract(
            repo.get_band_task_contract_for_chapter(project_id, chapter_number),
            combined_text=combined_text,
            reviewer="plan_control",
            issue_type="band_task_completion",
            target_scope="band",
        )
        for issue in band_task_issues:
            issues.append(
                BandCheckpointIssueInfo(
                    code="band_task_completion",
                    severity=issue.severity,
                    issue_group=issue.issue_group,
                    description=issue.description,
                    detail="; ".join(issue.evidence_refs),
                )
            )
        try:
            schedule_payload = json.loads(band_row.schedule_json or "{}")
        except (TypeError, json.JSONDecodeError):
            schedule_payload = {}
        band_schedule = (
            BandDelightSchedule.model_validate(schedule_payload)
            if isinstance(schedule_payload, dict)
            else None
        )
        obligation_repo = NarrativeObligationRepository(session)
        band_obligations = [
            *obligation_repo.list_active_for_context(
                project_id, chapter_number=chapter_number + 1
            ),
            *obligation_repo.list_planned_for_chapter(
                project_id, origin_chapter_number=chapter_number
            ),
        ]
        band_obligation_issues = evaluate_band_obligation_contract(
            band_schedule,
            obligations=band_obligations,
            band_end_chapter=chapter_number,
            reviewer="plan_control",
            target_scope="band",
        )
        for issue in band_obligation_issues:
            issues.append(
                BandCheckpointIssueInfo(
                    code="band_obligation_completion",
                    severity=issue.severity,
                    issue_group=issue.issue_group,
                    description=issue.description,
                    detail="; ".join(issue.evidence_refs),
                )
            )
        director_issues = evaluate_director_imbalance(
            review_metas=review_metas,
            band_stall_guard=int(getattr(band_row, "stall_guard_max_gap", 0) or 0),
            reviewer="plan_control",
            target_scope="band",
        )
        for issue in director_issues:
            issues.append(
                BandCheckpointIssueInfo(
                    code="director_imbalance",
                    severity=issue.severity,
                    issue_group=issue.issue_group,
                    description=issue.description,
                    detail="; ".join(issue.evidence_refs),
                )
            )
        next_band_summary = repo.get_next_band_summary(project_id, chapter_number)
        constraint_chapter = (
            int(next_band_summary.chapter_start or 0)
            if next_band_summary is not None
            else chapter_number + 1
        )
        future_constraints = (
            repo.list_active_narrative_constraints(
                project_id,
                chapter_number=max(chapter_number, constraint_chapter),
            )
            if constraints_enabled
            else []
        )
        if constraints_enabled:
            compatibility_issues = evaluate_constraint_issues(
                future_constraints,
                combined_text=combined_text,
                state_changes=[],
                events=[],
                thread_beats=[],
                reviewer="plan_control",
                issue_type="next_band_compatibility",
                target_scope="band",
            )
            compatibility_issues.extend(
                evaluate_next_band_task_compatibility(
                    next_band_summary=next_band_summary,
                    combined_text=combined_text,
                    reviewer="plan_control",
                    target_scope="band",
                )
            )
            for issue in compatibility_issues:
                issues.append(
                    BandCheckpointIssueInfo(
                        code="next_band_compatibility"
                        if issue.severity == "error"
                        else "future_constraint",
                        severity=issue.severity,
                        issue_group=issue.issue_group,
                        description=issue.description,
                        detail="; ".join(issue.evidence_refs),
                    )
                )
            next_band_targets = [
                *[
                    task.target_name
                    for task in (
                        next_band_summary.band_task_contract
                        if next_band_summary is not None
                        else []
                    )
                    if str(task.target_name or "").strip()
                ],
                *[
                    constraint.subject_name
                    for constraint in future_constraints
                    if str(constraint.subject_name or "").strip()
                ],
            ]
            future_risk_issues = evaluate_resource_closure_risk(
                combined_text=combined_text,
                next_band_targets=list(dict.fromkeys(next_band_targets)),
                reviewer="plan_control",
                target_scope="band",
            )
            for issue in future_risk_issues:
                category = ""
                for ref in issue.evidence_refs:
                    text = str(ref or "")
                    if text.startswith("category="):
                        category = text.split("=", 1)[1].strip()
                        break
                issues.append(
                    BandCheckpointIssueInfo(
                        code="future_resource_preservation",
                        severity="warning",
                        category=category,
                        issue_group=issue.issue_group,
                        description=issue.description,
                        detail="; ".join(issue.evidence_refs),
                    )
                )
        if status != "fail" and any(issue.severity == "error" for issue in issues):
            status = "fail"
        elif status == "pass" and any(issue.severity == "warning" for issue in issues):
            status = "warn"
        summary = (
            "band checkpoint 通过。"
            if status == "pass"
            else "band checkpoint 需要人工处理。"
        )
        row = updater.save_band_checkpoint(
            BandCheckpointDetail(
                project_id=project_id,
                arc_id=band_row.arc_id,
                band_id=band_row.band_id,
                chapter_start=int(band_row.chapter_start or 0),
                chapter_end=int(band_row.chapter_end or 0),
                trigger_source="auto_band_end",
                boundary_kind="band_end",
                boundary_chapter=chapter_number,
                status=status,
                summary=summary,
                issues=issues,
            )
        )
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            band_id=band_row.band_id,
            chapter_number=chapter_number,
            event_family="evaluation_verdict",
            event_type=DecisionEventType.BAND_CHECKPOINT_CREATED,
            scope="band",
            summary=summary,
            related_object_type="band_checkpoint",
            related_object_id=row.id,
            payload=attach_gate_outcome(
                {
                    "status": status,
                    "chapter_review_form_result": {},
                    "band_checkpoint_mode": "chapter_review_form",
                },
                _band_checkpoint_gate_outcome(
                    project_id=project_id,
                    checkpoint_id=str(row.id or ""),
                    band_id=str(band_row.band_id or ""),
                    chapter_number=chapter_number,
                    status=status,
                    issues=issues,
                ),
            ),
        )
        return row


__all__ = ["AuditControlStage"]
