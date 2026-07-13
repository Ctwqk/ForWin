from __future__ import annotations

import json
from typing import Any

from forwin.models.project import ChapterPlan


def select_stale_signal_targets(
    *,
    open_signals: list[dict[str, Any]],
    plans: list[ChapterPlan],
    current_chapter: int,
    include_current: bool,
    min_age_chapters: int = 2,
) -> list[dict[str, Any]]:
    target_plan = _first_eligible_plan(plans, current_chapter=current_chapter, include_current=include_current)
    if target_plan is None:
        return []
    result: list[dict[str, Any]] = []
    for signal in open_signals:
        if not isinstance(signal, dict):
            continue
        payload = _payload(signal)
        signal_type = str(signal.get("signal_type") or "").strip()
        is_form_plan_patchable = (
            signal_type == "form_open_signal_persisting"
            and payload.get("plan_patchable") is True
            and payload.get("patch_kind") == "signal_persisting"
        )
        severity = str(signal.get("severity") or "").strip().lower()
        if not is_form_plan_patchable and severity not in {"error", "fail", "critical", "blocker"}:
            continue
        signal_chapter = int(signal.get("chapter_number") or 0)
        if not is_form_plan_patchable and int(current_chapter or 0) - signal_chapter < int(min_age_chapters or 0):
            continue
        signal_id = str(signal.get("signal_id") or signal.get("subject_key") or "").strip()
        description = str(signal.get("description") or "").strip()
        if _plan_mentions_signal(target_plan, signal_id=signal_id, description=description):
            continue
        result.append(
            {
                "signal": signal,
                "plan": target_plan,
                "suppression_key": str(payload.get("suppression_key") or f"signal:{signal_id}"),
            }
        )
    return result


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


def _plan_mentions_signal(plan: ChapterPlan, *, signal_id: str, description: str) -> bool:
    text = _plan_text(plan)
    if signal_id and signal_id in text:
        return True
    return bool(description and description[:80] in text)


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
