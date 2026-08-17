from __future__ import annotations

import json
from typing import Any


def immutable_rule_invariants(
    canon_quality_context: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not isinstance(canon_quality_context, dict):
        return []
    return [
        item
        for item in canon_quality_context.get("invariant_constraints", []) or []
        if isinstance(item, dict)
        and str(item.get("kind") or "") == "active_rule"
        and bool((item.get("constraints") or {}).get("immutable_definition"))
        and str(item.get("invariant_key") or "").strip()
    ]


def render_invariant_anchor(invariant: dict[str, Any]) -> str:
    key = str(invariant.get("invariant_key") or "").strip()
    label = str(
        invariant.get("label") or invariant.get("subject_key") or key
    ).strip()
    current_value = json.dumps(
        invariant.get("current_value"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        f"canon invariant [{key}] {label} exact definition: {current_value}; "
        "do not change without an explicit canon bridge."
    )


__all__ = ["immutable_rule_invariants", "render_invariant_anchor"]
