from __future__ import annotations

import json
from collections import OrderedDict
from typing import Any, Iterable


def build_waiting_review_breakdown(
    events: Iterable[Any],
    *,
    limit: int = 12,
) -> list[dict[str, object]]:
    grouped: OrderedDict[str, dict[str, object]] = OrderedDict()
    for event in events:
        payload = _event_payload(event)
        if str(payload.get("outcome") or "").strip() != "manual_review":
            continue
        rule_id = str(payload.get("rule_id") or "").strip()
        if not rule_id:
            continue
        row = grouped.setdefault(
            rule_id,
            {
                "rule_id": rule_id,
                "outcome": "manual_review",
                "reason": str(payload.get("reason") or getattr(event, "reason", "") or ""),
                "count": 0,
                "status_chip": _status_chip(payload),
            },
        )
        row["count"] = int(row.get("count") or 0) + 1
        if not str(row.get("reason") or "").strip():
            row["reason"] = str(payload.get("reason") or getattr(event, "reason", "") or "")
    rows = sorted(grouped.values(), key=lambda item: int(item.get("count") or 0), reverse=True)
    return rows[: max(1, int(limit or 12))]


def _event_payload(event: Any) -> dict[str, Any]:
    payload = getattr(event, "payload", None)
    if isinstance(payload, dict):
        return payload
    raw = getattr(event, "payload_json", "") or ""
    if not str(raw).strip():
        return {}
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _status_chip(payload: dict[str, Any]) -> str:
    rule_id = str(payload.get("rule_id") or "")
    reason = str(payload.get("reason") or "")
    if "policy_disabled" in rule_id or "policy disabled:" in reason:
        return "可自动处理但策略关闭"
    return "需要人工判断"
