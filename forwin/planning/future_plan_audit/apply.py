from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.book_state.query_interface import SqlBookStateQueryInterface
from forwin.models.base import new_id
from forwin.models.narrative_obligation import FuturePlanAuditRunRow
from forwin.models.phase import BandExperiencePlan
from forwin.models.project import ArcPlanVersion, ChapterPlan
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.narrative_obligations.types import NarrativeObligation, NarrativePlanPatch
from forwin.planning.band_plan_patcher import BandPlanPatcher
from forwin.planning.obligation_pre_audit import select_urgent_obligation_targets
from forwin.planning.plan_patch_validator import PlanPatchValidator
from forwin.planning.signal_pre_audit import select_stale_signal_targets
from forwin.protocol.experience import BandDelightSchedule

from .helpers import *
from .macro_progression import audit_arc_macro_boundary
from .models import AuditStatus, FuturePlanAuditIssue, FuturePlanAuditRun


from .repository import FuturePlanAuditRepository
class FuturePlanApplyMixin:
    def audit_and_apply(
        self,
        *,
        session: Session,
        project_id: str,
        current_chapter: int,
        trigger_stage: str,
        plans: list[ChapterPlan],
        canon_quality_context: dict[str, Any],
        obligations: list[NarrativeObligation],
        target_total_chapters: int,
        include_current: bool,
        band_rows: list[BandExperiencePlan] | None = None,
    ) -> FuturePlanAuditRun:
        result = self.audit_plans(
            project_id=project_id,
            current_chapter=current_chapter,
            trigger_stage=trigger_stage,
            plans=plans,
            canon_quality_context=canon_quality_context,
            obligations=obligations,
            target_total_chapters=target_total_chapters,
            include_current=include_current,
            band_rows=band_rows,
        )
        result = _with_macro_progression_boundary_issues(
            session=session,
            project_id=project_id,
            current_chapter=current_chapter,
            result=result,
        )
        plans_by_id = {str(plan.id or ""): plan for plan in plans}
        bands_by_id = {str(row.band_id or ""): row for row in band_rows or []}
        obligation_repo = NarrativeObligationRepository(session)
        applied_patch_ids: list[str] = []
        persisted_patches: list[NarrativePlanPatch] = []
        blocking_reasons: list[str] = list(result.blocking_reasons)
        accepted_chapters = [
            int(plan.chapter_number or 0)
            for plan in plans
            if str(plan.status or "") == "accepted"
        ]
        unresolved_obligation_ids = [
            obligation.id
            for obligation in obligations
            if obligation.status not in {"resolved", "waived"}
        ]
        validator = PlanPatchValidator(
            mode=self.plan_patch_validation_mode,
            llm_client=self.llm_client,
            min_blocking_confidence=self.min_blocking_confidence,
        )
        for patch in result.plan_patches:
            plan = None
            band_row = None
            band_plan_bounds: dict[str, tuple[int, int]] = {}
            if patch.target_scope == "band":
                band_row = bands_by_id.get(str(patch.target_band_id or ""))
                if band_row is None:
                    blocking_reasons.append(f"missing_band_for_patch:{patch.id or patch.target_band_id}")
                    continue
                band_plan_bounds[str(band_row.band_id or "")] = (
                    int(band_row.chapter_start or 0),
                    int(band_row.chapter_end or 0),
                )
            else:
                plan = plans_by_id.get(str(patch.target_plan_id or ""))
                if plan is None:
                    blocking_reasons.append(f"missing_plan_for_patch:{patch.id or patch.target_plan_id}")
                    continue
            patch_obligations = [
                obligation
                for obligation in obligations
                if obligation.id in set(patch.source_obligation_ids)
            ]
            validation = validator.validate(
                patch=patch,
                obligations=patch_obligations,
                current_chapter=current_chapter,
                target_total_chapters=target_total_chapters,
                accepted_chapters=accepted_chapters,
                unresolved_obligation_ids=unresolved_obligation_ids,
                band_plan_bounds=band_plan_bounds,
                minimum_scope_by_obligation={
                    obligation.id: _minimum_scope_for_obligation(obligation)
                    for obligation in patch_obligations
                    if obligation.id
                },
            )
            if not validation.passed:
                blocking_reasons.extend(f"plan_patch_validation_failed:{error}" for error in validation.errors)
                continue
            applied_patch = patch.model_copy(
                update={
                    "id": patch.id or new_id(),
                    "validation_status": "passed",
                    "validation_errors": [],
                    "applied": True,
                }
            )
            if band_row is not None:
                BandPlanPatcher().apply(band_row, applied_patch, obligations=patch_obligations)
                session.add(band_row)
            elif plan is not None:
                self.apply_plan_patch(plan, applied_patch)
                session.add(plan)
            stored_patch = obligation_repo.create_plan_patch(applied_patch)
            applied_patch_ids.append(stored_patch.id)
            persisted_patches.append(stored_patch)
        result = result.model_copy(
            update={
                "plan_patches": persisted_patches or result.plan_patches,
                "applied_plan_patch_ids": applied_patch_ids,
                "blocking_reasons": blocking_reasons,
            }
        )
        stored_run = FuturePlanAuditRepository(session).save_run(result)
        return stored_run.model_copy(update={"plan_patches": result.plan_patches})


def _with_macro_progression_boundary_issues(
    *,
    session: Session,
    project_id: str,
    current_chapter: int,
    result: FuturePlanAuditRun,
) -> FuturePlanAuditRun:
    macro_status = SqlBookStateQueryInterface(session).get_protagonist_macro_status(
        project_id=project_id,
        as_of_chapter=int(current_chapter or 0),
    )
    successful_audited_arc_ids = _successful_macro_boundary_audited_arc_ids(
        session,
        project_id=project_id,
    )
    boundary_arcs = session.execute(
        select(ArcPlanVersion).where(
            ArcPlanVersion.project_id == project_id,
            ArcPlanVersion.chapter_end > 0,
            ArcPlanVersion.chapter_end <= int(current_chapter or 0),
        )
        .order_by(ArcPlanVersion.chapter_end.asc(), ArcPlanVersion.arc_number.asc())
    ).scalars().all()
    boundary_arcs = [
        arc
        for arc in boundary_arcs
        if str(arc.id or "") not in successful_audited_arc_ids
    ]
    audited_arc_ids = sorted(
        {
            *successful_audited_arc_ids,
            *[str(arc.id or "") for arc in boundary_arcs if str(arc.id or "")],
        }
    )
    result = result.model_copy(
        update={
            "metadata": {
                **dict(result.metadata or {}),
                "macro_boundary_audited_arc_ids": audited_arc_ids,
            }
        }
    )
    macro_issues = [
        issue
        for arc in boundary_arcs
        for issue in audit_arc_macro_boundary(
            arc=arc,
            current_chapter=int(current_chapter or 0),
            status=macro_status,
        )
    ]
    if not macro_issues:
        return result
    blocking_reasons = [
        *result.blocking_reasons,
        *[
            f"{issue.issue_type}:{issue.metadata.get('arc_id', '')}"
            for issue in macro_issues
            if issue.blocking
        ],
    ]
    return result.model_copy(
        update={
            "issues": [*result.issues, *macro_issues],
            "status": "fail",
            "blocking_reasons": blocking_reasons,
        }
    )


def _successful_macro_boundary_audited_arc_ids(
    session: Session,
    *,
    project_id: str,
) -> set[str]:
    rows = session.execute(
        select(FuturePlanAuditRunRow)
        .where(
            FuturePlanAuditRunRow.project_id == project_id,
            FuturePlanAuditRunRow.status != "fail",
        )
        .order_by(FuturePlanAuditRunRow.created_at.asc())
    ).scalars().all()
    audited: set[str] = set()
    for row in rows:
        metadata = _loads_json(row.metadata_json, {})
        if isinstance(metadata, dict):
            ids = metadata.get("macro_boundary_audited_arc_ids")
            if isinstance(ids, list):
                audited.update(str(item) for item in ids if str(item).strip())
        issues = _loads_json(row.issues_json, [])
        if isinstance(issues, list):
            for issue in issues:
                if not isinstance(issue, dict):
                    continue
                if issue.get("patch_type") != "macro_progression_boundary":
                    continue
                metadata = issue.get("metadata")
                if isinstance(metadata, dict) and metadata.get("arc_id"):
                    audited.add(str(metadata["arc_id"]))
    return audited


def _loads_json(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


__all__ = ["FuturePlanApplyMixin"]
