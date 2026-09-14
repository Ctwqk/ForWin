from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from forwin.models.audit import DecisionEvent
from forwin.models.phase import BandExperiencePlan
from forwin.models.planning_control import BandCheckpoint
from forwin.models.project import ChapterPlan, Project
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.planning.checkpoints import (
    band_is_first_chapter,
    chapter_blocking_message,
)
from forwin.planning.future_plan_audit import FuturePlanAuditor
from forwin.planning.health import PlanHealthService
from forwin.review.decision.types import Decision, DecisionInput
from forwin.review.plan_checks import (
    BandCheckpointEvaluator,
)
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater


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

    def _post_acceptance_audit_owner(self):
        from forwin.maintenance.order_controls import PostCanonOrderControls

        return PostCanonOrderControls(
            llm_client=getattr(self, "llm_client", None),
            record_event=self._record_decision_event,
        )

    def _audit_future_plans_after_acceptance(self, **kwargs):
        return self._post_acceptance_audit_owner()._audit_future_plans_after_acceptance(**kwargs)

    def _record_future_plan_audit_events(self, **kwargs):
        return self._post_acceptance_audit_owner()._record_future_plan_audit_events(**kwargs)

    def _record_generation_audit_report_if_due(self, **kwargs):
        return self._post_acceptance_audit_owner()._record_generation_audit_report_if_due(**kwargs)

    def _generation_audit_report_payload(self, **kwargs):
        return self._post_acceptance_audit_owner()._generation_audit_report_payload(**kwargs)

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
