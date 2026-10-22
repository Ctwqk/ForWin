"""Who decides a world-plan conflict, and which options the decider may choose."""

from __future__ import annotations

from forwin.runtime.policy import RuntimePolicy

from .conflict_types import ConflictDecider, ConflictDraft, ConflictOption


# What an auto-routed conflict does: the world gives way.
AUTO_OPTION: ConflictOption = "veto_world"
# P3 has no chapter-extraction source, so rewrite_chapter is never offered.
_PLAN_OPTIONS: tuple[ConflictOption, ...] = ("veto_world", "plot_responds", "amend_plan")
_OBLIGATION_IMPACTS = frozenset({"minor", "moderate"})
_CODEX_IMPACTS = frozenset({"moderate", "major"})


def available_options(draft: ConflictDraft) -> tuple[ConflictOption, ...]:
    """A rewrite can only undo what the world did; lighter conflicts may also leave an obligation."""
    if draft.source == "historical_rewrite":
        return ("veto_world",)
    if draft.impact_level in _OBLIGATION_IMPACTS:
        return (*_PLAN_OPTIONS, "accept_with_obligation")
    return _PLAN_OPTIONS


def route_conflict(draft: ConflictDraft, *, policy: RuntimePolicy) -> ConflictDecider:
    """Rewrites and critical conflicts go to a human, minor ones are vetoed automatically, and the
    rest go to Codex unless the policy keeps them for a human."""
    if draft.source == "historical_rewrite" or draft.impact_level == "critical":
        return "human"
    if draft.impact_level == "minor":
        return "auto"
    if draft.impact_level in _CODEX_IMPACTS and policy.world.conflict_delegate == "codex":
        return "codex"
    return "human"


__all__ = ["AUTO_OPTION", "available_options", "route_conflict"]
