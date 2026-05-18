from __future__ import annotations

import json
from typing import Any

from forwin.models.project import ChapterPlan
from forwin.narrative_obligations.types import NarrativeObligation


def select_urgent_obligation_targets(
    *,
    obligations: list[NarrativeObligation],
    plans: list[ChapterPlan],
    current_chapter: int,
    include_current: bool,
) -> list[dict[str, Any]]:
    eligible_plans = _eligible_plans(plans, current_chapter=current_chapter, include_current=include_current)
    plans_by_chapter = {int(plan.chapter_number or 0): plan for plan in eligible_plans}
    if not eligible_plans:
        return []
    first_plan = eligible_plans[0]
    candidates: list[dict[str, Any]] = []
    for obligation in obligations:
        if obligation.status not in {"active", "planned"}:
            continue
        if not obligation.must_resolve_now:
            continue
        plan = plans_by_chapter.get(int(obligation.deadline_chapter or 0)) or first_plan
        if _plan_already_covers_obligation(plan, obligation):
            continue
        candidates.append(
            {
                "obligation": obligation,
                "plan": plan,
                "suppression_key": f"obligation:{obligation.id}",
            }
        )
    return candidates


def _eligible_plans(
    plans: list[ChapterPlan],
    *,
    current_chapter: int,
    include_current: bool,
) -> list[ChapterPlan]:
    output = [
        plan
        for plan in plans
        if (include_current or int(plan.chapter_number or 0) > int(current_chapter or 0))
        and str(plan.status or "") != "accepted"
    ]
    return sorted(output, key=lambda plan: int(plan.chapter_number or 0))


def _plan_already_covers_obligation(plan: ChapterPlan, obligation: NarrativeObligation) -> bool:
    text = _plan_text(plan)
    obligation_id = str(obligation.id or "").strip()
    payoff_test = str(obligation.payoff_test or "").strip()
    if obligation_id and obligation_id in text:
        return True
    return bool(payoff_test and payoff_test in text)


def _plan_text(plan: ChapterPlan) -> str:
    payload = {
        "title": str(plan.title or ""),
        "one_line": str(plan.one_line or ""),
        "goals": _loads(plan.goals_json, []),
        "task_contract": _loads(plan.task_contract_json, []),
        "experience_plan": _loads(plan.experience_plan_json, {}),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _loads(raw: str, default: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (TypeError, json.JSONDecodeError):
        return default
    return value if value is not None else default
