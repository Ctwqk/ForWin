from __future__ import annotations

from forwin.planning.checkpoints import IssueGroup


_FACT_CONFLICT_HINTS = {
    "continuity",
    "future_constraint",
    "timeline",
    "state",
    "state_conflict",
    "character",
    "relationship",
    "relation",
    "intra_band_consistency",
    "world_consistency",
}


_DIRECTOR_IMBALANCE_HINTS = {
    "director_imbalance",
    "plan_task_fulfillment",
    "payoff",
    "pacing",
    "experience",
    "experience_delivery",
    "stall",
    "immersion",
}


_RUNTIME_HINTS = {
    "runtime",
    "llm",
    "stage",
    "memory",
    "fallback",
    "timeout",
    "retry",
}


_OPERATOR_ACTION_HINTS = {
    "operator",
    "manual",
    "override",
    "approve",
    "checkpoint_action",
    "constraint_update",
}


_PLAN_FEASIBILITY_GROUPS: dict[str, IssueGroup] = {
    "onstage_unavailable": "fact_conflict",
    "location_unreachable": "fact_conflict",
    "reveal_too_early": "fact_conflict",
    "milestone_uncovered": "director_imbalance",
}


def issue_group_for_issue(
    *, issue_type: str = "", rule_name: str = "", code: str = ""
) -> IssueGroup:
    for part in (issue_type, rule_name, code):
        group = _PLAN_FEASIBILITY_GROUPS.get(str(part or "").strip().lower())
        if group:
            return group
    text = " ".join(str(part or "") for part in (issue_type, rule_name, code)).lower()
    if not text.strip():
        return ""
    if any(hint in text for hint in _RUNTIME_HINTS):
        return "runtime_observation"
    if any(hint in text for hint in _OPERATOR_ACTION_HINTS):
        return "operator_action"
    if any(hint in text for hint in _DIRECTOR_IMBALANCE_HINTS):
        return "director_imbalance"
    if any(hint in text for hint in _FACT_CONFLICT_HINTS):
        return "fact_conflict"
    return "fact_conflict"


__all__ = [
    "issue_group_for_issue",
]
