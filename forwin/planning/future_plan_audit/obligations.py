from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.base import new_id
from forwin.models.narrative_obligation import FuturePlanAuditRunRow
from forwin.models.phase import BandExperiencePlan
from forwin.models.project import ChapterPlan
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.narrative_obligations.types import NarrativeObligation, NarrativePlanPatch
from forwin.planning.band_plan_patcher import BandPlanPatcher
from forwin.planning.obligation_pre_audit import select_urgent_obligation_targets
from forwin.planning.plan_patch_validator import PlanPatchValidator
from forwin.planning.signal_pre_audit import select_stale_signal_targets
from forwin.protocol.experience import BandDelightSchedule

from .helpers import *
from .models import AuditStatus, FuturePlanAuditIssue, FuturePlanAuditRun


class FuturePlanObligationMixin:
    def _audit_pre_write_obligations_and_signals(
        self,
        *,
        project_id: str,
        current_chapter: int,
        plans: list[ChapterPlan],
        canon_quality_context: dict[str, Any],
        obligations: list[NarrativeObligation],
        include_current: bool,
    ) -> list[tuple[FuturePlanAuditIssue, NarrativePlanPatch]]:
        result: list[tuple[FuturePlanAuditIssue, NarrativePlanPatch]] = []
        for candidate in select_urgent_obligation_targets(
            obligations=obligations,
            plans=plans,
            current_chapter=current_chapter,
            include_current=include_current,
        ):
            obligation = candidate["obligation"]
            plan = candidate["plan"]
            suppression_key = str(candidate.get("suppression_key") or "").strip()
            issue, patch = self._must_resolve_obligation_patch(
                project_id=project_id,
                current_chapter=current_chapter,
                plan=plan,
                obligation=obligation,
                suppression_key=suppression_key,
            )
            result.append((issue, patch))
        for candidate in select_stale_signal_targets(
            open_signals=_open_signal_constraints(canon_quality_context),
            plans=plans,
            current_chapter=current_chapter,
            include_current=include_current,
        ):
            signal = candidate["signal"]
            plan = candidate["plan"]
            suppression_key = str(candidate.get("suppression_key") or "").strip()
            issue, patch = self._stale_signal_patch(
                project_id=project_id,
                current_chapter=current_chapter,
                plan=plan,
                signal=signal,
                suppression_key=suppression_key,
            )
            result.append((issue, patch))
        return result

    def _must_resolve_obligation_patch(
        self,
        *,
        project_id: str,
        current_chapter: int,
        plan: ChapterPlan,
        obligation: NarrativeObligation,
        suppression_key: str,
    ) -> tuple[FuturePlanAuditIssue, NarrativePlanPatch]:
        chapter_number = int(plan.chapter_number or 0)
        description = (
            f"第{chapter_number}章写作前必须偿还叙事义务 {obligation.id}: "
            f"{obligation.payoff_test}"
        )
        metadata = {
            "obligation_id": obligation.id,
            "priority": obligation.priority,
            "payoff_test": obligation.payoff_test,
            "must_resolve_now": True,
            "suppression_key": suppression_key,
        }
        issue = FuturePlanAuditIssue(
            issue_type="obligation_pre_write_required",
            severity="error",
            target_chapter=chapter_number,
            target_plan_id=str(plan.id or ""),
            description=description,
            evidence_refs=[f"obligation:{obligation.id}", f"chapter_plan:{chapter_number}"],
            patch_type="obligation_pre_write",
            metadata=metadata,
        )
        patch = NarrativePlanPatch(
            id=new_id(),
            project_id=project_id,
            patch_type="obligation_pre_write",
            target_scope="chapter",
            target_plan_id=str(plan.id or ""),
            target_arc_id=str(plan.arc_plan_id or ""),
            affected_chapters=[chapter_number],
            source_obligation_ids=[obligation.id],
            old_contract=_chapter_plan_contract(plan),
            new_contract={
                "obligations_to_resolve": [obligation.id],
                "payoff_test": obligation.payoff_test,
                "summary": obligation.summary,
                "must_resolve_now": True,
            },
            diff_summary=description,
            must_preserve=[str(plan.title or ""), str(plan.one_line or "")],
            must_not_change=[f"remove must_resolve_now obligation {obligation.id}"],
            writer_context_injections=[
                {
                    "type": "narrative_obligation",
                    "obligation_id": obligation.id,
                    "priority": obligation.priority,
                    "summary": obligation.summary,
                    "payoff_test": obligation.payoff_test,
                    "deadline_chapter": obligation.deadline_chapter,
                    "must_resolve_now": True,
                }
            ],
            reviewer_context_injections=[
                {
                    "type": "narrative_obligation",
                    "obligation_id": obligation.id,
                    "payoff_test": obligation.payoff_test,
                    "must_resolve_now": True,
                }
            ],
            expected_resolution_tests=[obligation.payoff_test],
            validation_status="pending",
            metadata=metadata,
        )
        return issue, patch

    def _stale_signal_patch(
        self,
        *,
        project_id: str,
        current_chapter: int,
        plan: ChapterPlan,
        signal: dict[str, Any],
        suppression_key: str,
    ) -> tuple[FuturePlanAuditIssue, NarrativePlanPatch]:
        chapter_number = int(plan.chapter_number or 0)
        signal_id = str(signal.get("signal_id") or signal.get("subject_key") or "").strip()
        signal_chapter = int(signal.get("chapter_number") or 0)
        signal_type = str(signal.get("signal_type") or "quality_signal").strip()
        description_text = str(signal.get("description") or "").strip()
        instruction = (
            f"修复第{signal_chapter}章遗留质量信号 {signal_type}"
            f"{f'({signal_id})' if signal_id else ''}：{description_text}"
        ).strip()
        metadata = {
            "signal_id": signal_id,
            "signal_type": signal_type,
            "origin_chapter": signal_chapter,
            "current_chapter": current_chapter,
            "suppression_key": suppression_key,
        }
        issue = FuturePlanAuditIssue(
            issue_type="stale_open_signal_pre_write_required",
            severity="error",
            target_chapter=chapter_number,
            target_plan_id=str(plan.id or ""),
            description=instruction,
            evidence_refs=[f"signal:{signal_id}", f"chapter_plan:{chapter_number}"],
            patch_type="signal_pre_write",
            metadata=metadata,
        )
        patch = NarrativePlanPatch(
            id=new_id(),
            project_id=project_id,
            patch_type="signal_pre_write",
            target_scope="chapter",
            target_plan_id=str(plan.id or ""),
            target_arc_id=str(plan.arc_plan_id or ""),
            affected_chapters=[chapter_number],
            source_signal_ids=[signal_id] if signal_id else [],
            old_contract=_chapter_plan_contract(plan),
            new_contract={
                "form_instruction": instruction,
                "signal_id": signal_id,
                "signal_type": signal_type,
                "origin_chapter": signal_chapter,
            },
            diff_summary=instruction,
            must_preserve=[str(plan.title or ""), str(plan.one_line or "")],
            must_not_change=["drop stale open quality signal without resolution evidence"],
            writer_context_injections=[
                {
                    "type": "open_quality_signal_resolution",
                    "signal_id": signal_id,
                    "signal_type": signal_type,
                    "description": description_text,
                    "origin_chapter": signal_chapter,
                    "instruction": instruction,
                }
            ],
            reviewer_context_injections=[
                {
                    "type": "open_quality_signal_resolution",
                    "signal_id": signal_id,
                    "payoff_test": instruction,
                }
            ],
            expected_resolution_tests=[instruction],
            validation_status="pending",
            metadata=metadata,
        )
        return issue, patch

    def _audit_obligation_binding(
        self,
        *,
        project_id: str,
        current_chapter: int,
        plan: ChapterPlan,
        obligation: NarrativeObligation,
        target_total_chapters: int,
    ) -> tuple[FuturePlanAuditIssue | None, NarrativePlanPatch | None]:
        if obligation.status not in {"active", "planned"}:
            return None, None
        text = _plan_text(plan)
        if obligation.id and obligation.id in text and obligation.payoff_test and obligation.payoff_test in text:
            return None, None
        chapter_number = int(plan.chapter_number or 0)
        issue_type = "obligation_missing_from_future_plan"
        if (
            target_total_chapters
            and chapter_number >= target_total_chapters
            and obligation.priority in {"P0", "P1"}
        ):
            issue_type = "final_plan_carries_mainline_debt"
        description = f"第{chapter_number}章计划没有承接叙事义务 {obligation.id}: {obligation.payoff_test}"
        issue = FuturePlanAuditIssue(
            issue_type=issue_type,
            severity="error",
            target_chapter=chapter_number,
            target_plan_id=str(plan.id or ""),
            description=description,
            evidence_refs=[f"obligation:{obligation.id}", f"chapter_plan:{chapter_number}"],
            patch_type="obligation_plan_binding",
            metadata={
                "obligation_id": obligation.id,
                "priority": obligation.priority,
                "payoff_test": obligation.payoff_test,
            },
        )
        patch = NarrativePlanPatch(
            id=new_id(),
            project_id=project_id,
            patch_type="obligation_plan_binding",
            target_scope="chapter",
            target_plan_id=str(plan.id or ""),
            target_arc_id=str(plan.arc_plan_id or ""),
            affected_chapters=[chapter_number],
            source_obligation_ids=[obligation.id],
            old_contract=_chapter_plan_contract(plan),
            new_contract={
                "obligations_to_resolve": [obligation.id],
                "payoff_test": obligation.payoff_test,
                "summary": obligation.summary,
            },
            diff_summary=description,
            must_preserve=[str(plan.title or ""), str(plan.one_line or "")],
            must_not_change=[f"remove unresolved obligation {obligation.id}"],
            writer_context_injections=[
                {
                    "type": "narrative_obligation",
                    "obligation_id": obligation.id,
                    "priority": obligation.priority,
                    "summary": obligation.summary,
                    "payoff_test": obligation.payoff_test,
                    "deadline_chapter": obligation.deadline_chapter,
                }
            ],
            reviewer_context_injections=[
                {
                    "type": "narrative_obligation",
                    "obligation_id": obligation.id,
                    "payoff_test": obligation.payoff_test,
                    "must_resolve_now": True,
                }
            ],
            expected_resolution_tests=[obligation.payoff_test],
            validation_status="pending",
            metadata=issue.metadata,
        )
        return issue, patch

    def _audit_band_obligation_binding(
        self,
        *,
        project_id: str,
        current_chapter: int,
        band_row: BandExperiencePlan,
        obligation: NarrativeObligation,
    ) -> tuple[FuturePlanAuditIssue, NarrativePlanPatch]:
        description = f"band plan {band_row.band_id} 没有承接叙事义务 {obligation.id}: {obligation.payoff_test}"
        issue = FuturePlanAuditIssue(
            issue_type="obligation_missing_from_band_plan",
            severity="error",
            target_chapter=int(band_row.chapter_end or obligation.deadline_chapter or 0),
            target_plan_id=str(band_row.id or band_row.band_id or ""),
            description=description,
            evidence_refs=[f"obligation:{obligation.id}", f"band_plan:{band_row.band_id}"],
            patch_type="obligation_band_plan_binding",
            metadata={
                "obligation_id": obligation.id,
                "priority": obligation.priority,
                "payoff_test": obligation.payoff_test,
                "band_id": band_row.band_id,
            },
        )
        patch = BandPlanPatcher().build_obligation_patch(
            project_id=project_id,
            band_row=band_row,
            obligations=[obligation],
            current_chapter=current_chapter,
            patch_type="obligation_band_plan_binding",
        )
        return issue, patch


__all__ = ["FuturePlanObligationMixin"]
