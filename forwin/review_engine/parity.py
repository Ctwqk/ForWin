from __future__ import annotations

from dataclasses import dataclass

from .types import Decision

_FATE_CHANGING_OUTCOMES = {
    "auto_approve",
    "local_repair",
    "chapter_patch",
    "band_patch",
    "arc_patch",
    "book_patch",
    "commit_with_obligation",
    "manual_review",
    "system_block",
}


@dataclass(frozen=True)
class ShadowDecisionComparison:
    live: Decision
    shadow: Decision
    shadow_mismatch: bool


def compare_shadow_decisions(*, live: Decision, shadow: Decision) -> ShadowDecisionComparison:
    return ShadowDecisionComparison(
        live=live,
        shadow=shadow,
        shadow_mismatch=(live.outcome, live.sub_action) != (shadow.outcome, shadow.sub_action),
    )


def severe_shadow_mismatch(comparison: ShadowDecisionComparison) -> bool:
    if not comparison.shadow_mismatch:
        return False
    live_outcome = str(comparison.live.outcome or "")
    shadow_outcome = str(comparison.shadow.outcome or "")
    return (
        live_outcome in _FATE_CHANGING_OUTCOMES
        and shadow_outcome in _FATE_CHANGING_OUTCOMES
        and live_outcome != shadow_outcome
    )
