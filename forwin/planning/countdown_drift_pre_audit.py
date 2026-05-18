from __future__ import annotations

import json
from typing import Any


def select_countdown_drift_targets(signals: list[Any]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for signal in signals:
        payload = _payload(signal)
        if _row_value(signal, "signal_type") != "form_countdown_inconsistency":
            continue
        if payload.get("plan_patchable") is not True:
            continue
        countdown_key = str(_row_value(signal, "subject_key") or payload.get("countdown_key") or "").strip()
        if not countdown_key:
            continue
        source_signal_id = str(_row_value(signal, "signal_id") or "").strip()
        prior_value = _optional_int(payload.get("prior_value_minutes"))
        targets.append(
            {
                "patch_kind": "countdown_drift",
                "suppression_key": str(payload.get("suppression_key") or f"countdown:{countdown_key}"),
                **({"prior_value_minutes": prior_value} if prior_value is not None else {}),
                "task": _countdown_drift_task(countdown_key=countdown_key, prior_value_minutes=prior_value),
                "source_signal_id": source_signal_id,
                "source_mode": str(payload.get("source_mode") or payload.get("source") or "chapter_review_form"),
            }
        )
    return targets


def _countdown_drift_task(*, countdown_key: str, prior_value_minutes: int | None) -> str:
    if prior_value_minutes is None:
        return (
            f"本章必须明确处理 {countdown_key} 的当前状态。"
            "如继续，必须先承接最新 ledger 的既有值并不得增大；"
            "如已 closed，不得再次出现正数剩余时间；"
            "如确实重新开启，必须显式写出 reopen 事件并命名为新的局部窗口。"
        )
    return (
        f"本章必须明确处理 {countdown_key} 的当前状态：{prior_value_minutes} 分钟。"
        "如继续，必须 ≤ 该值；如已 closed，不得再次出现正数剩余时间；"
        "如确实重新开启，必须显式写出 reopen 事件并命名为新的局部窗口。"
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


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = ["select_countdown_drift_targets"]
