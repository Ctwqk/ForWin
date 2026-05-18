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


class FuturePlanPromptJsonMixin:
    def _audit_plans_prompt_json(
        self,
        *,
        project_id: str,
        current_chapter: int,
        trigger_stage: str,
        plans: list[ChapterPlan],
        canon_quality_context: dict[str, Any],
        obligations: list[NarrativeObligation],
        target_total_chapters: int,
        include_current: bool,
        band_rows: list[BandExperiencePlan] | None,
    ) -> FuturePlanAuditRun:
        inspected = _inspected_chapters(
            plans=plans,
            band_rows=band_rows,
            current_chapter=current_chapter,
            include_current=include_current,
        )
        result = FuturePlanPromptAuditor(
            llm_client=self.llm_client,
            min_blocking_confidence=self.min_blocking_confidence,
        ).analyze(
            _future_plan_prompt_payload(
                plans=plans,
                canon_quality_context=canon_quality_context,
                obligations=obligations,
                target_total_chapters=target_total_chapters,
                current_chapter=current_chapter,
                include_current=include_current,
                band_rows=band_rows,
            )
        )
        plans_by_id = {str(plan.id or ""): plan for plan in plans if str(plan.id or "").strip()}
        impacts_by_id = {
            str(item.get("plan_item_id") or ""): item
            for item in result.get("plan_impacts", [])
            if isinstance(item, dict)
        }
        fallback_plan = _first_prompt_target_plan(
            plans=plans,
            current_chapter=current_chapter,
            include_current=include_current,
        )
        issues: list[FuturePlanAuditIssue] = []
        patches: list[NarrativePlanPatch] = []
        for index, raw_issue in enumerate(result.get("issues", []) or [], start=1):
            if not isinstance(raw_issue, dict):
                continue
            plan_id = _prompt_issue_plan_id(raw_issue=raw_issue, impacts_by_id=impacts_by_id)
            plan = plans_by_id.get(plan_id) or fallback_plan
            impact = impacts_by_id.get(plan_id) if plan_id else None
            audit_issue = _prompt_issue_to_future_plan_issue(
                issue=raw_issue,
                result=result,
                current_chapter=current_chapter,
                plan=plan,
                plan_id=plan_id,
                impact=impact,
                min_blocking_confidence=self.min_blocking_confidence,
            )
            issues.append(audit_issue)
            patch = _prompt_issue_to_plan_patch(
                project_id=project_id,
                issue=raw_issue,
                result=result,
                plan=plan,
                plan_id=audit_issue.target_plan_id,
                target_chapter=audit_issue.target_chapter,
                index=index,
                impact=impact,
            )
            if patch is not None:
                patches.append(patch)

        suppressed_prompt_constraint_keys: set[str] = set()
        for issue, patch in self._audit_pre_write_obligations_and_signals(
            project_id=project_id,
            current_chapter=int(current_chapter or 0),
            plans=plans,
            canon_quality_context=canon_quality_context,
            obligations=obligations,
            include_current=include_current,
        ):
            issues.append(issue)
            patches.append(patch)
            suppression_key = str(issue.metadata.get("suppression_key") or "").strip()
            if suppression_key:
                suppressed_prompt_constraint_keys.add(suppression_key)

        status: AuditStatus = "pass"
        if any(issue.severity == "error" and issue.blocking for issue in issues):
            status = "fail"
        elif issues or patches or str(result.get("verdict") or "") in {"warn", "fail", "uncertain"}:
            status = "warn"
        return FuturePlanAuditRun(
            project_id=project_id,
            current_chapter=int(current_chapter or 0),
            trigger_stage=trigger_stage,
            inspected_chapters=inspected,
            status=status,
            issues=issues,
            plan_patches=patches,
            blocking_reasons=[
                f"{issue.issue_type}:{issue.target_chapter}"
                for issue in issues
                if issue.blocking
            ],
            metadata={
                "source_analyzer": str(result.get("analyzer") or "FuturePlanPromptAuditor"),
                "source_mode": "prompt_json",
                "original_verdict": str(result.get("verdict") or ""),
                "original_confidence": float(result.get("confidence") or 0.0),
                "prompt_json_summary": str(result.get("summary") or ""),
                "suppressed_prompt_constraint_keys": sorted(suppressed_prompt_constraint_keys),
                "pre_write_patch_count": len(suppressed_prompt_constraint_keys),
            },
        )


__all__ = ["FuturePlanPromptJsonMixin"]
