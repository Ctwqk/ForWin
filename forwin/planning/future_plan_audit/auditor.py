from __future__ import annotations

from typing import Any

from forwin.canon_quality.invariant_contract import (
    immutable_rule_invariants,
    render_invariant_anchor,
)
from forwin.models.base import new_id
from forwin.models.phase import BandExperiencePlan
from forwin.models.project import ChapterPlan
from forwin.narrative_obligations.types import NarrativeObligation, NarrativePlanPatch

from .apply import FuturePlanApplyMixin
from .countdown import FuturePlanCountdownMixin
from .custody import FuturePlanCustodyMixin
from .helpers import (
    _band_contract_covers_obligation,
    _band_row_for_obligation,
    _chapter_plan_contract,
    _character_state_constraints,
    _countdown_constraints,
    _loads,
    _minimum_scope_for_obligation,
)
from .models import AuditStatus, FuturePlanAuditIssue, FuturePlanAuditRun
from .obligations import FuturePlanObligationMixin
from .patches import FuturePlanPatchMixin


class FuturePlanAuditor(
    FuturePlanApplyMixin,
    FuturePlanCustodyMixin,
    FuturePlanCountdownMixin,
    FuturePlanObligationMixin,
    FuturePlanPatchMixin,
):
    def __init__(
        self,
        *,
        mode: str = "chapter_review_form",
        plan_patch_validation_mode: str = "chapter_review_form",
        llm_client: object | None = None,
        min_blocking_confidence: float = 0.8,
    ) -> None:
        self.mode = _normalize_form_mode(mode)
        self.plan_patch_validation_mode = _normalize_form_mode(plan_patch_validation_mode)
        self.llm_client = llm_client
        self.min_blocking_confidence = float(min_blocking_confidence)

    def audit_plans(
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
        band_rows: list[BandExperiencePlan] | None = None,
    ) -> FuturePlanAuditRun:
        inspected = [
            int(plan.chapter_number or 0)
            for plan in plans
            if include_current or int(plan.chapter_number or 0) > int(current_chapter or 0)
        ]
        for row in band_rows or []:
            for chapter in range(int(row.chapter_start or 0), int(row.chapter_end or 0) + 1):
                if (include_current or chapter > int(current_chapter or 0)) and chapter not in inspected:
                    inspected.append(chapter)
        inspected.sort()
        issues: list[FuturePlanAuditIssue] = []
        patches: list[NarrativePlanPatch] = []
        countdowns = _countdown_constraints(canon_quality_context)
        character_state_constraints = _character_state_constraints(canon_quality_context)
        immutable_invariants = immutable_rule_invariants(canon_quality_context)
        suppressed_prompt_constraint_keys: set[str] = set()
        form_plan_patch_signals_consumed = 0
        for plan in plans:
            chapter_number = int(plan.chapter_number or 0)
            if not include_current and chapter_number <= int(current_chapter or 0):
                continue
            if str(plan.status or "") == "accepted":
                continue
            invariant_binding = _audit_invariant_plan_binding(
                project_id=project_id,
                plan=plan,
                invariants=immutable_invariants,
            )
            if invariant_binding is not None:
                issue, patch = invariant_binding
                issues.append(issue)
                patches.append(patch)
            for issue, patch in self._audit_countdown_plan(
                project_id=project_id,
                current_chapter=int(current_chapter or 0),
                plan=plan,
                countdowns=countdowns,
            ):
                issues.append(issue)
                patches.append(patch)
            for issue, patch in self._audit_character_state_plan(
                project_id=project_id,
                current_chapter=int(current_chapter or 0),
                plan=plan,
                character_state_constraints=character_state_constraints,
            ):
                issues.append(issue)
                patches.append(patch)
        band_covered_obligation_ids: set[str] = set()
        for obligation in obligations:
            if obligation.status not in {"active", "planned"}:
                continue
            if _minimum_scope_for_obligation(obligation) != "band":
                continue
            band_row = _band_row_for_obligation(
                obligation=obligation,
                band_rows=list(band_rows or []),
                current_chapter=int(current_chapter or 0),
            )
            if band_row is None:
                continue
            if _band_contract_covers_obligation(band_row, obligation):
                band_covered_obligation_ids.add(obligation.id)
                continue
            issue, patch = self._audit_band_obligation_binding(
                project_id=project_id,
                current_chapter=int(current_chapter or 0),
                band_row=band_row,
                obligation=obligation,
            )
            issues.append(issue)
            patches.append(patch)

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
            if issue.metadata.get("source_mode") == "chapter_review_form" and issue.metadata.get("plan_patchable") is True:
                form_plan_patch_signals_consumed += 1

        plans_by_chapter = {int(plan.chapter_number or 0): plan for plan in plans}
        for obligation in obligations:
            if obligation.id in band_covered_obligation_ids or _minimum_scope_for_obligation(obligation) == "band":
                continue
            if obligation.must_resolve_now:
                continue
            plan = plans_by_chapter.get(int(obligation.deadline_chapter or 0))
            if plan is None or str(plan.status or "") == "accepted":
                continue
            issue, patch = self._audit_obligation_binding(
                project_id=project_id,
                current_chapter=int(current_chapter or 0),
                plan=plan,
                obligation=obligation,
                target_total_chapters=int(target_total_chapters or 0),
            )
            if issue is not None and patch is not None:
                issues.append(issue)
                patches.append(patch)

        status: AuditStatus = "pass"
        if any(issue.severity == "error" for issue in issues):
            status = "fail"
        elif issues:
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
                "suppressed_prompt_constraint_keys": sorted(suppressed_prompt_constraint_keys),
                "pre_write_patch_count": len(suppressed_prompt_constraint_keys),
                "form_plan_patch_signals_consumed": form_plan_patch_signals_consumed,
            },
        )


def _audit_invariant_plan_binding(
    *,
    project_id: str,
    plan: ChapterPlan,
    invariants: list[dict[str, Any]],
) -> tuple[FuturePlanAuditIssue, NarrativePlanPatch] | None:
    if not invariants:
        return None
    experience = _loads(plan.experience_plan_json, {})
    if not isinstance(experience, dict):
        experience = {}
    existing = {
        str(item).strip()
        for item in experience.get("rule_anchors", []) or []
        if str(item).strip()
    }
    rendered = [(item, render_invariant_anchor(item)) for item in invariants]
    missing = [(item, anchor) for item, anchor in rendered if anchor not in existing]
    if not missing:
        return None
    chapter_number = int(plan.chapter_number or 0)
    keys = [str(item.get("invariant_key") or "") for item, _anchor in missing]
    anchors = [anchor for _item, anchor in missing]
    description = (
        f"第{chapter_number}章计划缺少 {len(anchors)} 条 active canon invariant 绑定。"
    )
    evidence_refs = list(
        dict.fromkeys(
            [
                f"chapter_plan:{chapter_number}",
                *[
                    str(ref)
                    for item, _anchor in missing
                    for ref in item.get("evidence_refs", []) or []
                    if str(ref).strip()
                ],
            ]
        )
    )
    metadata = {"invariant_keys": keys, "anchor_count": len(anchors)}
    issue = FuturePlanAuditIssue(
        issue_type="invariant_plan_binding_missing",
        severity="warning",
        target_chapter=chapter_number,
        target_plan_id=str(plan.id or ""),
        description=description,
        evidence_refs=evidence_refs,
        patch_type="invariant_plan_binding",
        blocking=False,
        metadata=metadata,
    )
    patch = NarrativePlanPatch(
        id=new_id(),
        project_id=project_id,
        patch_type="invariant_plan_binding",
        target_scope="chapter",
        target_plan_id=str(plan.id or ""),
        target_arc_id=str(plan.arc_plan_id or ""),
        affected_chapters=[chapter_number],
        old_contract=_chapter_plan_contract(plan),
        new_contract={
            "invariant_anchors": anchors,
            "invariants": [item for item, _anchor in missing],
        },
        diff_summary=description,
        must_preserve=[str(plan.title or ""), str(plan.one_line or "")],
        must_not_change=anchors,
        writer_context_injections=[
            {"type": "canon_invariant", "invariant": item, "instruction": anchor}
            for item, anchor in missing
        ],
        reviewer_context_injections=[
            {
                "type": "canon_invariant",
                "invariant": item,
                "payoff_test": f"{item.get('invariant_key')} definition remains unchanged or has an explicit canon bridge.",
            }
            for item, _anchor in missing
        ],
        expected_resolution_tests=[
            f"{item.get('invariant_key')} definition remains unchanged or has an explicit canon bridge."
            for item, _anchor in missing
        ],
        validation_status="pending",
        metadata=metadata,
    )
    return issue, patch


def _normalize_form_mode(value: str | None) -> str:
    normalized = str(value or "chapter_review_form").strip().lower()
    if normalized in {"off", "primary", "chapter_review_form"}:
        return "chapter_review_form" if normalized == "primary" else normalized
    return "chapter_review_form"


__all__ = ["FuturePlanAuditor"]
