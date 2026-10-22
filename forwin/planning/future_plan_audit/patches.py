from __future__ import annotations

from forwin.models.project import ChapterPlan
from forwin.narrative_obligations.types import NarrativePlanPatch

from .helpers import _json, _loads


class FuturePlanPatchMixin:
    def apply_plan_patch(self, plan: ChapterPlan, patch: NarrativePlanPatch) -> None:
        if patch.patch_type == "invariant_plan_binding":
            self._apply_invariant_plan_binding(plan, patch)
            return
        if patch.patch_type in {"obligation_plan_binding", "obligation_pre_write", "defer_acceptance"}:
            self._apply_obligation_patch(plan, patch)
            return
        if patch.patch_type == "signal_pre_write":
            self.apply_form_plan_patch(plan, patch)

    @staticmethod
    def _apply_invariant_plan_binding(
        plan: ChapterPlan,
        patch: NarrativePlanPatch,
    ) -> None:
        anchors = [
            str(item).strip()
            for item in patch.new_contract.get("invariant_anchors", []) or []
            if str(item).strip()
        ]
        invariant_keys = [
            str(item).strip()
            for item in patch.metadata.get("invariant_keys", []) or []
            if str(item).strip()
        ]
        experience = _loads(plan.experience_plan_json, {})
        if not isinstance(experience, dict):
            experience = {}
        existing = [
            str(item).strip()
            for item in experience.get("rule_anchors", []) or []
            if str(item).strip()
        ]
        retained = [
            item
            for item in existing
            if not any(f"[{key}]" in item for key in invariant_keys)
        ]
        experience["rule_anchors"] = list(dict.fromkeys([*anchors, *retained]))
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
    def apply_form_plan_patch(plan: ChapterPlan, patch: NarrativePlanPatch) -> None:
        """Put the patch's form instruction first in the plan's goals and progress markers."""
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
