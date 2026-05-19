from __future__ import annotations

from dataclasses import asdict, is_dataclass
import hashlib
import json
from typing import Any

from .types import Decision, DecisionInput


def build_decision_event_payload(
    *,
    decision: Decision,
    input_digest: str,
    shadow_mismatch: bool,
    live_or_shadow: str = "shadow",
    legacy_outcome: str = "",
    engine_outcome: str = "",
) -> dict[str, object]:
    return {
        "rule_id": decision.rule_id,
        "outcome": decision.outcome,
        "reason": decision.reason,
        "missing_evidence": list(decision.missing_evidence),
        "routed_from": decision.routed_from,
        "sub_action": dict(decision.sub_action),
        "input_digest": input_digest,
        "shadow_mismatch": bool(shadow_mismatch),
        "live_or_shadow": str(live_or_shadow or "shadow"),
        "legacy_outcome": str(legacy_outcome or ""),
        "engine_outcome": str(engine_outcome or ""),
    }


def digest_decision_input(input: DecisionInput) -> str:
    payload = {
        "project_id": input.project_id,
        "chapter_number": input.chapter_number,
        "review": _jsonable(input.review),
        "signals": [_jsonable(signal) for signal in input.signals],
        "open_obligations": [_jsonable(item) for item in input.open_obligations],
        "operation_mode": input.operation_mode,
        "attempts_completed": input.attempts_completed,
        "prior_scope_history": list(input.prior_scope_history),
        "budget": _jsonable(input.budget),
        "target_total_chapters": input.target_total_chapters,
        "plan_layer_health": _jsonable(input.plan_layer_health),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return asdict(value)
    return value
