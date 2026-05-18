from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.canon_quality.prompt_json.normalization import prompt_issue_evidence_refs
from forwin.canon_quality.prompt_json.schemas import PromptJsonMode, normalize_prompt_json_mode
from forwin.canon_quality.prompt_json.validation import issue_can_block
from forwin.models.base import new_id
from forwin.models.narrative_obligation import FuturePlanAuditRunRow
from forwin.models.phase import BandExperiencePlan
from forwin.models.project import ChapterPlan
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.narrative_obligations.types import NarrativeObligation, NarrativePlanPatch
from forwin.planning.band_plan_patcher import BandPlanPatcher
from forwin.planning.obligation_pre_audit import select_urgent_obligation_targets
from forwin.planning.plan_patch_validator import PlanPatchValidator
from forwin.planning.prompt_json.future_plan_auditor_prompt import FuturePlanPromptAuditor
from forwin.planning.signal_pre_audit import select_stale_signal_targets
from forwin.protocol.experience import BandDelightSchedule

from .helpers import *
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
        plans_by_id = {str(plan.id or ""): plan for plan in plans}
        bands_by_id = {str(row.band_id or ""): row for row in band_rows or []}
        obligation_repo = NarrativeObligationRepository(session)
        applied_patch_ids: list[str] = []
        persisted_patches: list[NarrativePlanPatch] = []
        blocking_reasons: list[str] = []
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


__all__ = ["FuturePlanApplyMixin"]
