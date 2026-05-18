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


class FuturePlanPatchMixin:
    def apply_plan_patch(self, plan: ChapterPlan, patch: NarrativePlanPatch) -> None:
        if patch.patch_type in {"canon_plan_staleness", "future_plan_audit"}:
            if _is_custody_state_patch(patch):
                self._apply_custody_state_patch(plan, patch)
            else:
                self._apply_countdown_patch(plan, patch)
            return
        if patch.patch_type in {"obligation_plan_binding", "obligation_pre_write", "defer_acceptance"}:
            self._apply_obligation_patch(plan, patch)
            return
        if patch.patch_type == "signal_pre_write":
            self._apply_form_plan_patch(plan, patch)

    @staticmethod
    def _apply_countdown_patch(plan: ChapterPlan, patch: NarrativePlanPatch) -> None:
        metadata = patch.metadata or patch.new_contract
        key = str(metadata.get("countdown_key") or patch.new_contract.get("countdown_key") or "").strip()
        label = str(metadata.get("label") or patch.new_contract.get("label") or key or "倒计时").strip()
        latest = int(metadata.get("latest_remaining_minutes") or patch.new_contract.get("latest_remaining_minutes") or 0)
        rewrite_false_prior = bool(metadata.get("false_prior_conflict"))
        instruction = (
            f"{label}已关闭；不得写成仍有剩余时间。"
            if latest <= 0
            else f"{label}必须延续最新 canon ledger：剩余时间不得超过 {latest} 分钟。"
        )
        hard_instruction = str(patch.new_contract.get("hard_rule") or _hard_countdown_instruction(label=label, key=key, latest=latest))
        plan.title = _rewrite_stale_countdown_text(
            str(plan.title or ""),
            key=key,
            label=label,
            latest=latest,
            rewrite_false_prior=rewrite_false_prior,
        )
        plan.one_line = _rewrite_stale_countdown_text(
            _strip_countdown_instruction_text(str(plan.one_line or "")),
            key=key,
            label=label,
            latest=latest,
            rewrite_false_prior=rewrite_false_prior,
        )
        goals = _loads(plan.goals_json, [])
        rewritten_goals = [
            _rewrite_stale_countdown_text(
                _strip_countdown_instruction_text(str(item)),
                key=key,
                label=label,
                latest=latest,
                rewrite_false_prior=rewrite_false_prior,
            )
            for item in goals
            if str(item).strip()
        ]
        plan.goals_json = _json([item for item in rewritten_goals if str(item).strip()][:5])
        plan.task_contract_json = _json(
            _rewrite_json_strings(
                _strip_countdown_instruction_noise(_loads(plan.task_contract_json, [])),
                key=key,
                label=label,
                latest=latest,
                rewrite_false_prior=rewrite_false_prior,
            )
        )
        experience = _loads(plan.experience_plan_json, {})
        if not isinstance(experience, dict):
            experience = {}
        experience = _rewrite_json_strings(
            _strip_countdown_instruction_noise(experience),
            key=key,
            label=label,
            latest=latest,
            rewrite_false_prior=rewrite_false_prior,
        )
        rule_anchors = [str(item) for item in experience.get("rule_anchors", []) if str(item).strip()]
        if hard_instruction and hard_instruction not in rule_anchors:
            rule_anchors.insert(0, hard_instruction)
        if instruction not in rule_anchors:
            rule_anchors.insert(0, instruction)
        experience["rule_anchors"] = rule_anchors[:8]
        plan.experience_plan_json = _json(experience)

    @staticmethod
    def _apply_obligation_patch(plan: ChapterPlan, patch: NarrativePlanPatch) -> None:
        obligation_ids = list(patch.source_obligation_ids or [])
        payoff_tests = [str(test).strip() for test in patch.expected_resolution_tests if str(test).strip()]
        payoff_tests = payoff_tests or [
            str(item.get("payoff_test") or "").strip()
            for item in patch.reviewer_context_injections
            if isinstance(item, dict) and str(item.get("payoff_test") or "").strip()
        ]
        contract_payoff = str(patch.new_contract.get("payoff_test") or "").strip()
        if contract_payoff and contract_payoff not in payoff_tests:
            payoff_tests.append(contract_payoff)
        goals = [str(item).strip() for item in _loads(plan.goals_json, []) if str(item).strip()]
        for obligation_id, payoff_test in zip(obligation_ids, payoff_tests or [""]):
            goal = f"偿还叙事义务 {obligation_id}：{payoff_test}".strip("：")
            if goal not in goals:
                goals.insert(0, goal)
        plan.goals_json = _json(goals[:5])
        experience = _loads(plan.experience_plan_json, {})
        if not isinstance(experience, dict):
            experience = {}
        progress_markers = [str(item) for item in experience.get("progress_markers", []) if str(item).strip()]
        rule_anchors = [str(item) for item in experience.get("rule_anchors", []) if str(item).strip()]
        obligation_contract = list(experience.get("obligations_to_resolve", []) or [])
        for obligation_id in obligation_ids:
            if obligation_id and obligation_id not in obligation_contract:
                obligation_contract.append(obligation_id)
        for payoff_test in payoff_tests:
            if payoff_test and payoff_test not in progress_markers:
                progress_markers.insert(0, payoff_test)
            if payoff_test and payoff_test not in rule_anchors:
                rule_anchors.insert(0, payoff_test)
        experience["obligations_to_resolve"] = obligation_contract
        experience["progress_markers"] = progress_markers[:5]
        experience["rule_anchors"] = rule_anchors[:5]
        plan.experience_plan_json = _json(experience)

    @staticmethod
    def _apply_custody_state_patch(plan: ChapterPlan, patch: NarrativePlanPatch) -> None:
        metadata = patch.metadata or patch.new_contract
        character_name = str(metadata.get("character_name") or patch.new_contract.get("character_name") or "").strip()
        latest_chapter = int(metadata.get("latest_chapter") or patch.new_contract.get("latest_chapter") or 0)
        hard_instruction = str(
            patch.new_contract.get("rule")
            or _hard_custody_instruction(character_name=character_name, latest_chapter=latest_chapter)
        )
        plan.title = _rewrite_stale_custody_text(str(plan.title or ""), character_name=character_name)
        plan.one_line = _rewrite_stale_custody_text(str(plan.one_line or ""), character_name=character_name)
        goals = [
            _rewrite_stale_custody_text(str(item), character_name=character_name)
            for item in _loads(plan.goals_json, [])
            if str(item).strip()
        ]
        plan.goals_json = _json([item for item in goals if str(item).strip()][:5])
        plan.task_contract_json = _json(
            _rewrite_custody_json_strings(_loads(plan.task_contract_json, []), character_name=character_name)
        )
        experience = _loads(plan.experience_plan_json, {})
        if not isinstance(experience, dict):
            experience = {}
        experience = _rewrite_custody_json_strings(experience, character_name=character_name)
        if not isinstance(experience, dict):
            experience = {}
        progress_markers = [str(item) for item in experience.get("progress_markers", []) if str(item).strip()]
        rule_anchors = [str(item) for item in experience.get("rule_anchors", []) if str(item).strip()]
        if hard_instruction and hard_instruction not in rule_anchors:
            rule_anchors.insert(0, hard_instruction)
        progress = f"承接{character_name}已脱困但仍受追踪器或系统权限限制的状态。"
        if character_name and progress not in progress_markers:
            progress_markers.insert(0, progress)
        experience["progress_markers"] = progress_markers[:5]
        experience["rule_anchors"] = rule_anchors[:8]
        plan.experience_plan_json = _json(experience)

    @staticmethod
    def _apply_form_plan_patch(plan: ChapterPlan, patch: NarrativePlanPatch) -> None:
        instruction = str(patch.new_contract.get("form_instruction") or patch.diff_summary or "").strip()
        if not instruction:
            return
        goals = [str(item).strip() for item in _loads(plan.goals_json, []) if str(item).strip()]
        if instruction not in goals:
            goals.insert(0, instruction)
        plan.goals_json = _json(goals[:5])
        experience = _loads(plan.experience_plan_json, {})
        if not isinstance(experience, dict):
            experience = {}
        progress_markers = [str(item) for item in experience.get("progress_markers", []) if str(item).strip()]
        if instruction not in progress_markers:
            progress_markers.insert(0, instruction)
        experience["progress_markers"] = progress_markers[:5]
        plan.experience_plan_json = _json(experience)


__all__ = ["FuturePlanPatchMixin"]
