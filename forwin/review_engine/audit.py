from __future__ import annotations

from dataclasses import asdict, is_dataclass
import hashlib
import json
from typing import Any, Iterable, Mapping

from .types import Decision, DecisionInput


def build_decision_event_payload(
    *,
    decision: Decision,
    input_digest: str,
    shadow_mismatch: bool,
    live_or_shadow: str = "shadow",
    legacy_outcome: str = "",
    engine_outcome: str = "",
    live_source: str = "",
    shadow_source: str = "",
    engine_live: bool = False,
    legacy_shadow_evaluated: bool = False,
    legacy_safety_net_used: bool = False,
    severe_shadow_mismatch: bool = False,
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
        "live_source": str(live_source or ""),
        "shadow_source": str(shadow_source or ""),
        "engine_live": bool(engine_live),
        "legacy_shadow_evaluated": bool(legacy_shadow_evaluated),
        "legacy_safety_net_used": bool(legacy_safety_net_used),
        "severe_shadow_mismatch": bool(severe_shadow_mismatch),
    }


def summarize_live_cutover_audit(
    rows: Iterable[Mapping[str, Any]],
    *,
    expected_chapters: int = 60,
) -> dict[str, object]:
    expected = max(0, int(expected_chapters or 0))
    by_chapter: dict[int, list[Mapping[str, Any]]] = {}
    for row in rows:
        payload = row.get("payload", row)
        if not isinstance(payload, Mapping):
            payload = {}
        chapter = int(row.get("chapter_number") or payload.get("chapter_number") or 0)
        if chapter <= 0:
            continue
        by_chapter.setdefault(chapter, []).append(payload)

    expected_range = list(range(1, expected + 1)) if expected else sorted(by_chapter)
    missing_chapters = [chapter for chapter in expected_range if chapter not in by_chapter]
    legacy_safety_net_chapters = [
        chapter
        for chapter, payloads in sorted(by_chapter.items())
        if any(_uses_legacy_safety_net(payload) for payload in payloads)
    ]
    severe_mismatch_chapters = [
        chapter
        for chapter, payloads in sorted(by_chapter.items())
        if any(bool(payload.get("severe_shadow_mismatch")) for payload in payloads)
    ]
    non_live_chapters = [
        chapter
        for chapter, payloads in sorted(by_chapter.items())
        if not any(_is_engine_live_payload(payload) for payload in payloads)
    ]
    engine_live_chapters = [
        chapter
        for chapter, payloads in sorted(by_chapter.items())
        if any(_is_engine_live_payload(payload) for payload in payloads)
    ]
    passed = not (
        missing_chapters
        or legacy_safety_net_chapters
        or severe_mismatch_chapters
        or non_live_chapters
    )
    return {
        "passed": passed,
        "expected_chapters": expected,
        "observed_chapters": len(by_chapter),
        "engine_live_chapters": len(engine_live_chapters),
        "missing_chapters": missing_chapters,
        "legacy_safety_net_chapters": legacy_safety_net_chapters,
        "severe_mismatch_chapters": severe_mismatch_chapters,
        "non_live_chapters": non_live_chapters,
    }


def _uses_legacy_safety_net(payload: Mapping[str, Any]) -> bool:
    return (
        bool(payload.get("legacy_safety_net_used"))
        or str(payload.get("live_source") or "") == "legacy"
        or (
            str(payload.get("live_or_shadow") or "") == "live"
            and str(payload.get("routed_from") or "") in {"ReviewOutcomeRouter", "RepairPolicy"}
        )
    )


def _is_engine_live_payload(payload: Mapping[str, Any]) -> bool:
    return (
        str(payload.get("live_or_shadow") or "") == "live"
        and bool(payload.get("engine_live"))
        and str(payload.get("live_source") or "") == "engine"
    )


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
