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
from forwin.planning.countdown_drift_pre_audit import select_countdown_drift_targets
from forwin.planning.ledger_state_drift_pre_audit import select_ledger_state_drift_targets
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
        open_signals = _open_signal_constraints(canon_quality_context)
        consumed_signal_ids: set[str] = set()
        for candidate in select_ledger_state_drift_targets(open_signals):
            plan = _first_eligible_plan(plans, current_chapter=current_chapter, include_current=include_current)
            if plan is None:
                continue
            issue, patch = self._ledger_state_drift_patch(
                project_id=project_id,
                current_chapter=current_chapter,
                plan=plan,
                candidate=candidate,
            )
            result.append((issue, patch))
            if candidate.source_signal_id:
                consumed_signal_ids.add(candidate.source_signal_id)
        legacy_countdown_signals = [
            signal
            for signal in open_signals
            if str(signal.get("signal_id") or "").strip() not in consumed_signal_ids
        ]
        for candidate in select_countdown_drift_targets(legacy_countdown_signals):
            plan = _first_eligible_plan(plans, current_chapter=current_chapter, include_current=include_current)
            if plan is None:
                continue
            issue, patch = self._countdown_drift_patch(
                project_id=project_id,
                current_chapter=current_chapter,
                plan=plan,
                candidate=candidate,
            )
            result.append((issue, patch))
        for candidate in select_urgent_obligation_targets(
            obligations=obligations,
            plans=plans,
            current_chapter=current_chapter,
            include_current=include_current,
            form_signals=_open_signal_constraints(canon_quality_context),
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

    def _ledger_state_drift_patch(
        self,
        *,
        project_id: str,
        current_chapter: int,
        plan: ChapterPlan,
        candidate: Any,
    ) -> tuple[FuturePlanAuditIssue, NarrativePlanPatch]:
        chapter_number = int(plan.chapter_number or 0)
        task = str(candidate.task or "").strip()
        suppression_key = str(candidate.suppression_key or "").strip()
        source_signal_id = str(candidate.source_signal_id or "").strip()
        metadata = {
            "patch_kind": "ledger_state_drift",
            "invariant_key": candidate.invariant_key,
            "invariant_kind": candidate.kind,
            "subject_key": candidate.subject_key,
            "current_chapter": current_chapter,
            "suppression_key": suppression_key,
            "source_signal_id": source_signal_id,
            "source_mode": str(candidate.source_mode or "chapter_review_form"),
            "plan_patchable": True,
            "expected": candidate.expected,
            "observed": candidate.observed,
            "allowed_bridges": candidate.allowed_bridges,
        }
        issue = FuturePlanAuditIssue(
            issue_type="ledger_state_drift_pre_write_required",
            severity="error",
            target_chapter=chapter_number,
            target_plan_id=str(plan.id or ""),
            description=task,
            evidence_refs=[f"signal:{source_signal_id}", f"chapter_plan:{chapter_number}"] if source_signal_id else [
                f"chapter_plan:{chapter_number}"
            ],
            patch_type="ledger_state_drift_pre_write",
            metadata=metadata,
        )
        patch = NarrativePlanPatch(
            id=new_id(),
            project_id=project_id,
            patch_type="ledger_state_drift_pre_write",
            target_scope="chapter",
            target_plan_id=str(plan.id or ""),
            target_arc_id=str(plan.arc_plan_id or ""),
            affected_chapters=[chapter_number],
            source_signal_ids=[source_signal_id] if source_signal_id else [],
            old_contract=_chapter_plan_contract(plan),
            new_contract={
                "ledger_state_drift_task": task,
                "suppression_key": suppression_key,
                "invariant_key": candidate.invariant_key,
                "invariant_kind": candidate.kind,
                "expected": candidate.expected,
                "observed": candidate.observed,
            },
            diff_summary=task,
            must_preserve=[str(plan.title or ""), str(plan.one_line or "")],
            must_not_change=["drop ledger state drift handling without explicit evidence"],
            writer_context_injections=[
                {
                    "type": "ledger_state_drift_resolution",
                    "instruction": task,
                    "suppression_key": suppression_key,
                    "invariant_key": candidate.invariant_key,
                    "invariant_kind": candidate.kind,
                    "expected": candidate.expected,
                    "observed": candidate.observed,
                    "source_signal_id": source_signal_id,
                }
            ],
            reviewer_context_injections=[
                {
                    "type": "ledger_state_drift_resolution",
                    "payoff_test": task,
                    "source_signal_id": source_signal_id,
                    "invariant_key": candidate.invariant_key,
                }
            ],
            expected_resolution_tests=[task],
            validation_status="pending",
            metadata=metadata,
        )
        return issue, patch

    def _countdown_drift_patch(
        self,
        *,
        project_id: str,
        current_chapter: int,
        plan: ChapterPlan,
        candidate: dict[str, Any],
    ) -> tuple[FuturePlanAuditIssue, NarrativePlanPatch]:
        chapter_number = int(plan.chapter_number or 0)
        task = str(candidate.get("task") or "").strip()
        suppression_key = str(candidate.get("suppression_key") or "").strip()
        source_signal_id = str(candidate.get("source_signal_id") or "").strip()
        metadata = {
            "patch_kind": "countdown_drift",
            "current_chapter": current_chapter,
            "suppression_key": suppression_key,
            "source_signal_id": source_signal_id,
            "source_mode": str(candidate.get("source_mode") or "chapter_review_form"),
            "plan_patchable": True,
        }
        issue = FuturePlanAuditIssue(
            issue_type="countdown_drift_pre_write_required",
            severity="error",
            target_chapter=chapter_number,
            target_plan_id=str(plan.id or ""),
            description=task,
            evidence_refs=[f"signal:{source_signal_id}", f"chapter_plan:{chapter_number}"] if source_signal_id else [
                f"chapter_plan:{chapter_number}"
            ],
            patch_type="countdown_drift_pre_write",
            metadata=metadata,
        )
        patch = NarrativePlanPatch(
            id=new_id(),
            project_id=project_id,
            patch_type="countdown_drift_pre_write",
            target_scope="chapter",
            target_plan_id=str(plan.id or ""),
            target_arc_id=str(plan.arc_plan_id or ""),
            affected_chapters=[chapter_number],
            source_signal_ids=[source_signal_id] if source_signal_id else [],
            old_contract=_chapter_plan_contract(plan),
            new_contract={
                "countdown_drift_task": task,
                "suppression_key": suppression_key,
            },
            diff_summary=task,
            must_preserve=[str(plan.title or ""), str(plan.one_line or "")],
            must_not_change=["drop countdown drift handling without explicit evidence"],
            writer_context_injections=[
                {
                    "type": "countdown_drift_resolution",
                    "instruction": task,
                    "suppression_key": suppression_key,
                    "source_signal_id": source_signal_id,
                }
            ],
            reviewer_context_injections=[
                {
                    "type": "countdown_drift_resolution",
                    "payoff_test": task,
                    "source_signal_id": source_signal_id,
                }
            ],
            expected_resolution_tests=[task],
            validation_status="pending",
            metadata=metadata,
        )
        return issue, patch

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
        metadata.update(_source_metadata(obligation.metadata))
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
        metadata.update(_signal_source_metadata(signal))
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


def _first_eligible_plan(
    plans: list[ChapterPlan],
    *,
    current_chapter: int,
    include_current: bool,
) -> ChapterPlan | None:
    eligible = [
        plan
        for plan in plans
        if (include_current or int(plan.chapter_number or 0) > int(current_chapter or 0))
        and str(plan.status or "") != "accepted"
    ]
    return min(eligible, key=lambda plan: int(plan.chapter_number or 0), default=None)


def _source_metadata(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    allowed = {"source_mode", "source_signal_id", "plan_patchable", "patch_kind"}
    return {key: raw[key] for key in allowed if key in raw}


def _signal_source_metadata(signal: dict[str, Any]) -> dict[str, Any]:
    payload = signal.get("payload", {}) if isinstance(signal, dict) else {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    if not isinstance(payload, dict):
        return {}
    source_signal_id = str(signal.get("signal_id") or "").strip()
    metadata = _source_metadata(payload)
    if source_signal_id and "source_signal_id" not in metadata:
        metadata["source_signal_id"] = source_signal_id
    return metadata
