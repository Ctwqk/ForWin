"""Who decides a world-plan conflict, and which options the decider may choose."""

from __future__ import annotations

from forwin.runtime.policy import RuntimePolicy
from forwin.book_state.runtime import BookStateRuntime

from .conflict_types import ConflictDecider, ConflictDraft, ConflictOption


# What an auto-routed conflict does: the world gives way.
AUTO_OPTION: ConflictOption = "veto_world"
# A plan the planner could not make feasible: only a human amends it, through the plan amender.
PLAN_CONFLICT_SOURCES = frozenset({"plan_feasibility", "band_planning"})
_PLAN_CONFLICT_OPTIONS: tuple[ConflictOption, ...] = ("amend_plan",)
# P3 has no chapter-extraction source, so rewrite_chapter is never offered.
_PLAN_OPTIONS: tuple[ConflictOption, ...] = ("veto_world", "plot_responds", "amend_plan")
# A change that breaks a narrative boundary lands only once the user amends the plan around it.
PLAN_BOUNDARY_VIOLATIONS = frozenset({"milestone", "reveal_schedule"})
_BOUNDARY_VIOLATIONS = PLAN_BOUNDARY_VIOLATIONS | {"protection"}
_BOUNDARY_OPTIONS: tuple[ConflictOption, ...] = ("veto_world", "amend_plan")
_OBLIGATION_IMPACTS = frozenset({"minor", "moderate"})
_CODEX_IMPACTS = frozenset({"moderate", "major"})


def live_intent(runtime: BookStateRuntime, intent_ref: str) -> bool:
    node = runtime.world.get_node(intent_ref.strip()) if intent_ref else None
    return (node is not None and node.is_active and str(node.node_type) == "intent"
            and runtime.world.get_state(node.id).get("status") in {"planned", "due", "held"})


def available_options(
    draft: ConflictDraft, *, live_intent: bool = False, plot_responded: bool = False,
) -> tuple[ConflictOption, ...]:
    """A rewrite can only undo what the world did, an infeasible plan can only be amended, a broken
    boundary is vetoed or amended around, and lighter conflicts may also leave an obligation."""
    if draft.source == "historical_rewrite":
        return ("veto_world",)
    if draft.source in PLAN_CONFLICT_SOURCES:
        return _PLAN_CONFLICT_OPTIONS
    if draft.violated in _BOUNDARY_VIOLATIONS:
        return _BOUNDARY_OPTIONS
    options = _PLAN_OPTIONS if draft.intent_ref and live_intent and not plot_responded else _BOUNDARY_OPTIONS
    if draft.impact_level in _OBLIGATION_IMPACTS:
        return (*options, "accept_with_obligation")
    return options


def route_conflict(draft: ConflictDraft, *, policy: RuntimePolicy) -> ConflictDecider:
    """Rewrites, plan conflicts and critical conflicts go to a human, minor ones are vetoed automatically,
    and the rest go to Codex unless the policy keeps them for a human."""
    if (draft.source == "historical_rewrite" or draft.source in PLAN_CONFLICT_SOURCES
            or draft.impact_level == "critical"):
        return "human"
    if draft.impact_level == "minor":
        return "auto"
    if draft.impact_level in _CODEX_IMPACTS and policy.world.conflict_delegate == "codex":
        return "codex"
    return "human"


__all__ = ["PLAN_BOUNDARY_VIOLATIONS", "AUTO_OPTION", "PLAN_CONFLICT_SOURCES", "available_options", "live_intent", "route_conflict"]
