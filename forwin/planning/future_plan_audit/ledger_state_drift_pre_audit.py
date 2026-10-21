from __future__ import annotations

import json
from typing import Any

from forwin.canon_quality.invariants import InvariantDriftTarget, InvariantKind


def select_ledger_state_drift_targets(
    signals: list[Any],
    *,
    project_id: str = "",
    as_of_chapter: int = 0,
    book_state_query: Any | None = None,
) -> list[InvariantDriftTarget]:
    targets: list[InvariantDriftTarget] = []
    for signal in signals:
        signal_type = str(_row_value(signal, "signal_type") or "").strip()
        payload = _payload(signal)
        if payload.get("plan_patchable") is not True:
            continue
        if signal_type == "form_invariant_drift":
            target = _invariant_target(signal=signal, payload=payload)
        else:
            target = None
        if target is not None:
            targets.append(target)
    return targets


def _invariant_target(*, signal: Any, payload: dict[str, Any]) -> InvariantDriftTarget | None:
    invariant_key = str(payload.get("invariant_key") or _row_value(signal, "subject_key") or "").strip()
    if not invariant_key:
        return None
    kind = _normalize_kind(payload.get("invariant_kind") or payload.get("kind") or "custom")
    label = str(payload.get("label") or payload.get("invariant_label") or invariant_key).strip()
    expected = _dict(payload.get("expected"))
    observed = _dict(payload.get("observed"))
    task = str(payload.get("task") or "").strip() or _ledger_state_task(
        invariant_key=invariant_key,
        label=label,
        kind=kind,
        expected=expected,
        observed=observed,
    )
    return InvariantDriftTarget(
        suppression_key=str(payload.get("generic_suppression_key") or payload.get("suppression_key") or f"invariant:{invariant_key}"),
        invariant_key=invariant_key,
        kind=kind,
        subject_key=str(payload.get("subject_key") or _row_value(signal, "subject_key") or invariant_key),
        label=label,
        task=task,
        expected=expected,
        observed=observed,
        allowed_bridges=[str(item) for item in payload.get("allowed_bridges", []) or []],
        source_signal_id=str(_row_value(signal, "signal_id") or "").strip(),
        source_mode=str(payload.get("source_mode") or payload.get("source") or "chapter_review_form"),
    )


def _ledger_state_task(
    *,
    invariant_key: str,
    label: str,
    kind: InvariantKind,
    expected: dict[str, Any],
    observed: dict[str, Any],
) -> str:
    name = label or invariant_key
    if kind == "deadline":
        return (
            f"本章必须修复强状态 {name} 的截止条件。"
            f"必须承接既有截止状态 {expected or '未知'}；"
            f"如要改写为 {observed or '新状态'}，必须写出明确桥接事件、代价或授权来源。"
        )
    return (
        f"本章必须修复强状态 {name} 的 ledger drift。"
        f"必须承接既有状态 {expected or '未知'}；"
        f"如要改写为 {observed or '新状态'}，必须写出明确桥接事件。"
    )


def _payload(row: Any) -> dict[str, Any]:
    payload = _row_value(row, "payload", {}) or {}
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def _row_value(row: Any, key: str, default: Any = "") -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _normalize_kind(value: Any) -> InvariantKind:
    normalized = str(value or "").strip()
    allowed = {
        "monotonic_numeric",
        "deadline",
        "terminal_state",
        "state_transition",
        "set_count",
        "active_rule",
        "custom",
    }
    if normalized in allowed:
        return normalized  # type: ignore[return-value]
    return "custom"


__all__ = ["select_ledger_state_drift_targets"]
