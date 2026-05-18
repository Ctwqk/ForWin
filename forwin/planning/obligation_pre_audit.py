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
    form_signals: list[dict[str, Any]] | None = None,
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
    for obligation, suppression_key in _form_obligation_signals_to_obligations(
        form_signals or [],
        current_chapter=current_chapter,
    ):
        if _plan_already_covers_obligation(first_plan, obligation):
            continue
        candidates.append(
            {
                "obligation": obligation,
                "plan": first_plan,
                "suppression_key": suppression_key,
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


def _form_obligation_signals_to_obligations(
    signals: list[dict[str, Any]],
    *,
    current_chapter: int,
) -> list[tuple[NarrativeObligation, str]]:
    obligations: list[tuple[NarrativeObligation, str]] = []
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        payload = _payload(signal)
        if signal.get("signal_type") != "form_obligation_unresolved":
            continue
        if payload.get("plan_patchable") is not True or payload.get("patch_kind") != "obligation_unresolved":
            continue
        obligation_id = str(signal.get("subject_key") or payload.get("obligation_id") or "").strip()
        if not obligation_id:
            continue
        signal_id = str(signal.get("signal_id") or "").strip()
        description = str(signal.get("description") or "").strip() or f"叙事义务 {obligation_id} 仍未解决。"
        payoff_test = f"本章必须给出 {obligation_id} 的解决、延期或明确失败证据。"
        source_metadata = {
            "source_mode": str(payload.get("source_mode") or payload.get("source") or "chapter_review_form"),
            "source_signal_id": signal_id,
            "plan_patchable": True,
            "patch_kind": "obligation_unresolved",
        }
        obligation = NarrativeObligation(
            id=obligation_id,
            project_id=str(signal.get("project_id") or ""),
            origin_chapter_number=int(signal.get("chapter_number") or current_chapter or 0),
            origin_signal_ids=[signal_id] if signal_id else [],
            obligation_type="form_obligation_unresolved",
            priority="P1",
            status="active",
            summary=description,
            hardness="canon_risk",
            subject_refs=[obligation_id],
            evidence_refs=[f"signal:{signal_id}"] if signal_id else [],
            deadline_chapter=max(int(current_chapter or 0) + 1, int(signal.get("chapter_number") or 0) + 1),
            payoff_test=payoff_test,
            must_resolve_now=True,
            metadata=source_metadata,
        )
        obligations.append((obligation, str(payload.get("suppression_key") or f"obligation:{obligation_id}")))
    return obligations


def _payload(signal: dict[str, Any]) -> dict[str, Any]:
    payload = signal.get("payload", {}) or {}
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}
