from __future__ import annotations

from forwin.models.project import ChapterPlan
from forwin.narrative_obligations.types import NarrativePlanPatch

from .helpers import _json, _loads


_OBLIGATION_PATCH_TYPES = frozenset({"obligation_plan_binding", "obligation_pre_write", "defer_acceptance"})


class FuturePlanPatchMixin:
    def apply_plan_patch(self, plan: ChapterPlan, patch: NarrativePlanPatch) -> None:
        if patch.patch_type == "invariant_plan_binding":
            self._apply_invariant_plan_binding(plan, patch)
            return
        if patch.patch_type in _OBLIGATION_PATCH_TYPES:
            self._apply_obligation_patch(plan, patch)
            return
        if patch.patch_type == "signal_pre_write":
            self.apply_form_plan_patch(plan, patch)

    @classmethod
    def revert_plan_patch(cls, plan: ChapterPlan, patch: NarrativePlanPatch) -> None:
        """Take back out of the plan what applying the patch put in.

        An obligation patch loses its goals, its obligations_to_resolve entries and its payoff
        markers; any other patch loses its form instruction. What the five-item caps pushed out
        when the patch applied does not come back.
        """
        if patch.patch_type in _OBLIGATION_PATCH_TYPES:
            obligation_ids = list(patch.source_obligation_ids or [])
            payoff_tests = _obligation_payoff_tests(patch)
            _remove_from_plan(
                plan,
                goals=set(_obligation_goals(obligation_ids, payoff_tests)),
                experience_items={
                    "obligations_to_resolve": set(obligation_ids),
                    "progress_markers": set(payoff_tests),
                    "rule_anchors": set(payoff_tests),
                },
            )
            return
        instruction = _form_instruction(patch)
        if instruction:
            _remove_from_plan(plan, goals={instruction}, experience_items={"progress_markers": {instruction}})

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
        payoff_tests = _obligation_payoff_tests(patch)
        goals = [str(item).strip() for item in _loads(plan.goals_json, []) if str(item).strip()]
        for goal in _obligation_goals(obligation_ids, payoff_tests):
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
        instruction = _form_instruction(patch)
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


def _obligation_payoff_tests(patch: NarrativePlanPatch) -> list[str]:
    payoff_tests = [str(test).strip() for test in patch.expected_resolution_tests if str(test).strip()]
    payoff_tests = payoff_tests or [
        str(item.get("payoff_test") or "").strip()
        for item in patch.reviewer_context_injections
        if isinstance(item, dict) and str(item.get("payoff_test") or "").strip()
    ]
    contract_payoff = str(patch.new_contract.get("payoff_test") or "").strip()
    if contract_payoff and contract_payoff not in payoff_tests:
        payoff_tests.append(contract_payoff)
    return payoff_tests


def _obligation_goals(obligation_ids: list[str], payoff_tests: list[str]) -> list[str]:
    return [
        f"偿还叙事义务 {obligation_id}：{payoff_test}".strip("：")
        for obligation_id, payoff_test in zip(obligation_ids, payoff_tests or [""])
    ]


def _form_instruction(patch: NarrativePlanPatch) -> str:
    return str(patch.new_contract.get("form_instruction") or patch.diff_summary or "").strip()


def _remove_from_plan(plan: ChapterPlan, *, goals: set[str], experience_items: dict[str, set[str]]) -> None:
    """Drop ``goals`` from the plan's goals and each key's items from its experience plan list."""
    plan.goals_json = _json([
        goal for item in _loads(plan.goals_json, []) if (goal := str(item).strip()) and goal not in goals
    ])
    experience = _loads(plan.experience_plan_json, {})
    if not isinstance(experience, dict):
        return
    for key, items in experience_items.items():
        if isinstance(experience.get(key), list):
            experience[key] = [item for item in experience[key] if str(item).strip() not in items]
    plan.experience_plan_json = _json(experience)


__all__ = ["FuturePlanPatchMixin"]
