from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from forwin.audit.events import DecisionEventType


def repair_word_budget_patch(context) -> dict[str, object]:
    patch: dict[str, object] = {
        "repair_max_growth_ratio": 1.08,
        "must_replace_not_append": True,
    }
    for key in ("target_chapter_chars", "min_chapter_chars", "max_chapter_chars"):
        value = getattr(context, key, 0)
        try:
            normalized = int(value or 0)
        except (TypeError, ValueError):
            normalized = 0
        if normalized > 0:
            patch[key] = normalized
    return patch


@dataclass(frozen=True)
class RepairBodyBudgetDecision:
    event_type: str
    reason: str
    payload: dict[str, Any]


def evaluate_repair_body_budget(
    *,
    source_char_count: int,
    result_char_count: int,
    design_patch: dict[str, object],
) -> RepairBodyBudgetDecision | None:
    source_chars = max(0, int(source_char_count or 0))
    result_chars = max(0, int(result_char_count or 0))
    max_chars = _positive_int(design_patch.get("max_chapter_chars"))
    target_chars = _positive_int(design_patch.get("target_chapter_chars"))
    growth_ratio = _positive_float(design_patch.get("repair_max_growth_ratio"))
    must_replace = bool(design_patch.get("must_replace_not_append"))
    growth_limit = (
        int(source_chars * growth_ratio) if source_chars and growth_ratio else 0
    )
    over_max = bool(max_chars and result_chars > max_chars)
    over_growth = bool(growth_limit and result_chars > growth_limit)
    if not over_max and not over_growth:
        return None

    needs_human = bool(max_chars and result_chars > int(max_chars * 1.5))
    event_type = (
        DecisionEventType.REPAIR_NEEDS_HUMAN_COMPRESSION
        if needs_human
        else DecisionEventType.REPAIR_BODY_OVER_BUDGET
    )
    reason = (
        "repair-needs-human-compression" if needs_human else "repair-body-over-budget"
    )
    return RepairBodyBudgetDecision(
        event_type=event_type,
        reason=reason,
        payload={
            "source_char_count": source_chars,
            "result_char_count": result_chars,
            "target_chapter_chars": target_chars,
            "max_chapter_chars": max_chars,
            "repair_max_growth_ratio": growth_ratio,
            "growth_limit_chars": growth_limit,
            "must_replace_not_append": must_replace,
            "over_max_chapter_chars": over_max,
            "over_growth_ratio": over_growth,
            "needs_human_compression": needs_human,
        },
    )


def _positive_int(value: object) -> int:
    try:
        normalized = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return normalized if normalized > 0 else 0


def _positive_float(value: object) -> float:
    try:
        normalized = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return normalized if normalized > 0 else 0.0


__all__ = [
    "RepairBodyBudgetDecision",
    "evaluate_repair_body_budget",
    "repair_word_budget_patch",
]
