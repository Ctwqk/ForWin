from __future__ import annotations

from ..types import DecisionRule
from .repair_v2 import build_repair_v2_rules


def build_scope_driven_repair_rules(
    *,
    repair_v2_enabled: bool,
    policy: object | None = None,
) -> list[DecisionRule]:
    del repair_v2_enabled, policy
    return build_repair_v2_rules(enabled=True)
