from __future__ import annotations

import json
from typing import Any


from forwin.models.phase import BandExperiencePlan
from forwin.models.project import ChapterPlan
from forwin.narrative_obligations.types import NarrativeObligation
from forwin.protocol.experience import BandDelightSchedule


def _open_signal_constraints(canon_quality_context: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(canon_quality_context, dict):
        return []
    return [
        item
        for item in canon_quality_context.get("open_signals", []) or []
        if isinstance(item, dict)
    ]

def _chapter_plan_contract(plan: ChapterPlan) -> dict[str, Any]:
    return {
        "chapter_number": int(plan.chapter_number or 0),
        "title": str(plan.title or ""),
        "one_line": str(plan.one_line or ""),
        "goals": _loads(plan.goals_json, []),
        "world_bindings": [
            {"how": binding.get("how", "")}
            for binding in _loads(plan.world_bindings_json, [])
            if isinstance(binding, dict)
        ],
        "intended_changes": _loads(plan.intended_changes_json, []),
        "hook": str(plan.hook or ""),
        "experience_plan": _loads(plan.experience_plan_json, {}),
    }


def plan_text(plan: ChapterPlan) -> str:
    payload = _chapter_plan_contract(plan)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _minimum_scope_for_obligation(obligation: NarrativeObligation) -> str:
    metadata = obligation.metadata if isinstance(obligation.metadata, dict) else {}
    explicit = str(metadata.get("minimum_scope") or "").strip()
    if explicit:
        return explicit
    if obligation.obligation_type in {
        "reader_promise_payoff",
        "reveal_escalation_needed",
        "style_repetition_pressure",
        "repeated_scene_pattern",
    }:
        return "band"
    return "chapter"

def _band_row_for_obligation(
    *,
    obligation: NarrativeObligation,
    band_rows: list[BandExperiencePlan],
    current_chapter: int,
) -> BandExperiencePlan | None:
    deadline = int(obligation.deadline_chapter or 0)
    for row in band_rows:
        if int(row.chapter_start or 0) <= deadline <= int(row.chapter_end or 0):
            return row
    for row in band_rows:
        if int(row.chapter_end or 0) > int(current_chapter or 0):
            return row
    return None

def _band_contract_covers_obligation(row: BandExperiencePlan, obligation: NarrativeObligation) -> bool:
    obligation_id = str(obligation.id or "").strip()
    if not obligation_id:
        return False
    try:
        payload = json.loads(row.schedule_json or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        schedule = BandDelightSchedule.model_validate(payload)
    except Exception:
        return False
    contract = schedule.band_obligation_contract
    if obligation_id not in contract.open_obligations:
        return False
    if obligation_id not in contract.payoff_tests:
        return False
    return bool(str(contract.payoff_tests.get(obligation_id) or "").strip())


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)

def _loads(raw: str, default: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (TypeError, json.JSONDecodeError):
        return default
    return value if value is not None else default


__all__ = [
    name
    for name, value in globals().items()
    if name.startswith("_") and (callable(value) or name[1:].isupper())
]
