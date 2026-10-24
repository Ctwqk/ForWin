"""Post-Canon arc activation, remaining-plan feasibility and just-in-time band planning."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.canon import CanonCommitRecord
from forwin.models.planning_v6 import BandPlanRow
from forwin.models.project import ArcPlanVersion, ChapterPlan
from forwin.planning.band_planner.types import (
    REWRITABLE_PLAN_STATUSES, BandPlanningAuditError, BandPlanOutcome, FeasibilityAuditor, PlanIssue,
)
from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
from forwin.planning.beat_sheet.types import BandSpan
from forwin.planning.feasibility import open_plan_feasibility_conflict, respond_to_infeasible_band
from forwin.runtime.policy import RuntimePolicy

if TYPE_CHECKING:
    from forwin.planning.arc_progression import ArcProgressionService
    from forwin.planning.band_planner.service import BandPlanner


class _RemainingBandPlanner:
    """Keep the original infeasible chapters when a failed replan only reports provider/audit errors."""

    def __init__(self, planner: BandPlanner, issues: Sequence[PlanIssue]) -> None:
        self.planner, self.issues = planner, issues

    def replan_remaining(self, session: Session, **kwargs) -> BandPlanOutcome:
        try:
            outcome = self.planner.replan_remaining(session, **kwargs)
        except BandPlanningAuditError as exc:
            outcome = BandPlanOutcome(band_plan_id=None, issues=exc.issues, failure_reason="audit_error")
        if outcome.band_plan_id is None:
            return replace(outcome, issues=[*self.issues, *outcome.issues])
        return outcome


class PlanStepService:
    def __init__(self, band_planner: BandPlanner, feasibility: FeasibilityAuditor,
                 arc_progression: ArcProgressionService) -> None:
        self.band_planner = band_planner
        self.feasibility = feasibility
        self.arc_progression = arc_progression

    def run(self, session: Session, commit: CanonCommitRecord, *, policy: RuntimePolicy) -> dict:
        """Commit activation, audit/replan and next-band planning separately; hold no write lock over a model call."""
        project_id, number = commit.project_id, commit.chapter_number
        result = {
            "feasibility_issue_count": 0, "replanned_band_ids": [], "conflict_ids": [],
            "planned_band_id": None, "activated_arc_id": None, "band_planning_deferred": [],
        }
        arc = session.scalar(select(ArcPlanVersion).where(
            ArcPlanVersion.project_id == project_id, ArcPlanVersion.status != "superseded",
            ArcPlanVersion.chapter_start <= number, ArcPlanVersion.chapter_end >= number,
        ).order_by(ArcPlanVersion.version.desc()).limit(1))
        if arc is None:
            return result

        # Activation commits twice. Nothing from the audit or planner may precede it.
        following = None
        if number == arc.chapter_end:
            following = self.arc_progression.activate_next_arc_if_needed(
                session, project_id=project_id, after_chapter=number, policy=policy,
            )
            session.commit()
            result["activated_arc_id"] = following.id if following is not None else None

        sheets = ArcBeatSheetRepository(session)
        sheet_row = sheets.active_for_arc(arc.id)
        sheet = sheets.load(sheet_row) if sheet_row is not None else None
        band_row = session.scalar(select(BandPlanRow).where(
            BandPlanRow.project_id == project_id, BandPlanRow.arc_plan_id == arc.id,
            BandPlanRow.status == "active", BandPlanRow.chapter_start <= number,
            BandPlanRow.chapter_end >= number,
        ).order_by(BandPlanRow.version.desc()).limit(1))
        band = (BandSpan(band_index=band_row.band_index, chapter_start=band_row.chapter_start,
                         chapter_end=band_row.chapter_end) if band_row is not None else
                next((item for item in sheet.band_breakdown if item.chapter_start <= number <= item.chapter_end),
                     None) if sheet is not None else None)
        if band is not None:
            plans = list(session.scalars(select(ChapterPlan).where(
                ChapterPlan.project_id == project_id, ChapterPlan.arc_plan_id == arc.id,
                ChapterPlan.chapter_number > number, ChapterPlan.chapter_number <= band.chapter_end,
                ChapterPlan.status.in_(REWRITABLE_PLAN_STATUSES),
            ).order_by(ChapterPlan.chapter_number)))
            if plans:
                issues = self.feasibility.audit_plans(
                    session, project_id=project_id, plans=plans, beat_sheet=sheet, policy=policy,
                )
                result["feasibility_issue_count"] = len(issues)
                if any(issue.severity == "error" for issue in issues):
                    if band_row is not None:
                        response = respond_to_infeasible_band(
                            session, band_planner=_RemainingBandPlanner(self.band_planner, issues),
                            project_id=project_id, band_plan_id=band_row.id, from_chapter=number + 1,
                            issues=issues, policy=policy,
                        )
                        if response.replanned:
                            result["replanned_band_ids"].append(response.band_plan_id)
                        if response.conflict_id:
                            result["conflict_ids"].append(response.conflict_id)
                        if response.deferred_reason:
                            result["band_planning_deferred"].append({
                                "band": [band.chapter_start, band.chapter_end],
                                "reason": response.deferred_reason, "issue_codes": [],
                            })
                    else:
                        conflict = open_plan_feasibility_conflict(
                            session, project_id=project_id, band=band, issues=issues, policy=policy,
                        )
                        result["conflict_ids"].append(conflict.id)
        session.commit()

        next_arc, next_sheet, next_band = arc, sheets.active_for_arc(arc.id), None
        if following is not None:
            next_arc = following
            next_sheet = sheets.active_for_arc(following.id)
            if next_sheet is not None:
                next_band = min(sheets.load(next_sheet).band_breakdown,
                                key=lambda item: item.chapter_start, default=None)
        elif band is not None and number == band.chapter_end and next_sheet is not None:
            next_band = min((item for item in sheets.load(next_sheet).band_breakdown if item.chapter_start > number),
                            key=lambda item: item.chapter_start, default=None)
        if next_band is not None:
            # Reuse only the current sheet's complete band, with every rewritable row attached to it.
            existing = session.scalar(select(BandPlanRow).where(
                BandPlanRow.project_id == project_id, BandPlanRow.arc_plan_id == next_arc.id,
                BandPlanRow.band_index == next_band.band_index, BandPlanRow.status == "active",
            ).order_by(BandPlanRow.version.desc()).limit(1))
            plans = list(session.scalars(select(ChapterPlan).where(
                ChapterPlan.project_id == project_id, ChapterPlan.arc_plan_id == next_arc.id,
                ChapterPlan.chapter_number.between(next_band.chapter_start, next_band.chapter_end),
            )))
            complete = ({plan.chapter_number for plan in plans}
                        == set(range(next_band.chapter_start, next_band.chapter_end + 1)))
            reusable = (existing is not None and next_sheet is not None
                        and existing.beat_sheet_id == next_sheet.id and complete
                        and existing.chapter_start == next_band.chapter_start
                        and existing.chapter_end == next_band.chapter_end
                        and all(plan.status != "failed" and (plan.status not in REWRITABLE_PLAN_STATUSES
                                or plan.band_plan_id == existing.id) for plan in plans))
            if reusable:
                outcome = BandPlanOutcome(band_plan_id=existing.id)
            else:
                try:
                    if existing is not None:
                        try:
                            issues = self.feasibility.audit_plans(
                                session, project_id=project_id,
                                plans=[plan for plan in plans if plan.status in REWRITABLE_PLAN_STATUSES],
                                beat_sheet=sheets.load(next_sheet), policy=policy,
                            )
                        except Exception as exc:
                            raise BandPlanningAuditError(str(exc), [PlanIssue(
                                code="audit_error", severity="error", message=str(exc),
                            )]) from exc
                        result["feasibility_issue_count"] += len(issues)
                    outcome = self.band_planner.plan_band(
                        session, project_id=project_id, arc_plan=next_arc, beat_sheet_row=next_sheet,
                        band=next_band, policy=policy,
                        expected_band_plan_id=existing.id if existing is not None else None,
                    )
                except BandPlanningAuditError as exc:
                    outcome = BandPlanOutcome(band_plan_id=None, issues=exc.issues, failure_reason="audit_error")
                if existing is not None and outcome.band_plan_id == existing.id:
                    # All remaining rows may be protected, leaving the stale band impossible to refresh.
                    outcome = BandPlanOutcome(band_plan_id=None, failure_reason="band_not_current")
            result["planned_band_id"] = outcome.band_plan_id
            if outcome.band_plan_id is None:
                codes = list(dict.fromkeys(issue.code for issue in outcome.issues))
                result["band_planning_deferred"].append({
                    "band": [next_band.chapter_start, next_band.chapter_end],
                    "reason": outcome.failure_reason or (codes[0] if codes else "infeasible"), "issue_codes": codes,
                })
            session.commit()
        return result


__all__ = ["PlanStepService"]
