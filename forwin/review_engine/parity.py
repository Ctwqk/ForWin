from __future__ import annotations

from dataclasses import dataclass

from .types import Decision


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
