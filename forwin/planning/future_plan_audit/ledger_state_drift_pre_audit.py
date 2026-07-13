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
    live_countdowns = _live_countdowns(
        project_id=project_id,
        as_of_chapter=as_of_chapter,
        book_state_query=book_state_query,
    )
    targets: list[InvariantDriftTarget] = []
    for signal in signals:
        signal_type = str(_row_value(signal, "signal_type") or "").strip()
        payload = _payload(signal)
        if payload.get("plan_patchable") is not True:
            continue
        if signal_type == "form_invariant_drift":
            target = _invariant_target(signal=signal, payload=payload)
        elif signal_type == "form_countdown_inconsistency":
            target = _countdown_compat_target(signal=signal, payload=payload, live_countdowns=live_countdowns)
        else:
            target = None
        if target is not None:
            targets.append(target)
    return targets


def select_countdown_compat_drift_targets(
    signals: list[Any],
    *,
    project_id: str = "",
    as_of_chapter: int = 0,
    book_state_query: Any | None = None,
) -> list[dict[str, Any]]:
    live_countdowns = _live_countdowns(
        project_id=project_id,
        as_of_chapter=as_of_chapter,
        book_state_query=book_state_query,
    )
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
        prior_value = _optional_int(payload.get("prior_value_minutes"))
        if countdown_key in live_countdowns:
            prior_value = _optional_int(getattr(live_countdowns[countdown_key], "remaining_minutes", None))
        targets.append(
            {
                "patch_kind": "countdown_drift",
                "suppression_key": str(payload.get("suppression_key") or f"countdown:{countdown_key}"),
                **({"prior_value_minutes": prior_value} if prior_value is not None else {}),
                "task": _countdown_drift_task(countdown_key=countdown_key, prior_value_minutes=prior_value),
                "source_signal_id": str(_row_value(signal, "signal_id") or "").strip(),
                "source_mode": str(payload.get("source_mode") or payload.get("source") or "chapter_review_form"),
            }
        )
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


def _countdown_compat_target(
    *,
    signal: Any,
    payload: dict[str, Any],
    live_countdowns: dict[str, Any],
) -> InvariantDriftTarget | None:
    countdown_key = str(_row_value(signal, "subject_key") or payload.get("countdown_key") or "").strip()
    if not countdown_key:
        return None
    invariant_key = str(payload.get("invariant_key") or f"countdown:{countdown_key}").strip()
    prior_value = _optional_int(payload.get("prior_value_minutes"))
    if countdown_key in live_countdowns:
        prior_value = _optional_int(getattr(live_countdowns[countdown_key], "remaining_minutes", None))
    observed_value = _optional_int(payload.get("new_value_minutes"))
    expected = {"value_unit": "minutes"}
    if prior_value is not None:
        expected["current_value"] = prior_value
    observed = {"value_unit": "minutes"}
    if observed_value is not None:
        observed["current_value"] = observed_value
    task = str(payload.get("generic_task") or payload.get("task") or "").strip()
    if not task:
        task = _countdown_drift_task(countdown_key=countdown_key, prior_value_minutes=prior_value)
    return InvariantDriftTarget(
        suppression_key=str(payload.get("generic_suppression_key") or f"invariant:{invariant_key}"),
        invariant_key=invariant_key,
        kind="monotonic_numeric",
        subject_key=countdown_key,
        label=str(payload.get("label") or countdown_key),
        task=task,
        expected=expected,
        observed=observed,
        allowed_bridges=[str(item) for item in payload.get("allowed_bridges", []) or ["reset", "reopened", "branch_clock"]],
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
    if kind == "monotonic_numeric" and str(expected.get("value_unit") or "") == "minutes":
        prior = _optional_int(expected.get("current_value"))
        return _countdown_drift_task(countdown_key=name, prior_value_minutes=prior)
    return (
        f"本章必须修复强状态 {name} 的 ledger drift。"
        f"必须承接既有状态 {expected or '未知'}；"
        f"如要改写为 {observed or '新状态'}，必须写出明确桥接事件。"
    )


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


def _live_countdowns(*, project_id: str, as_of_chapter: int, book_state_query: Any | None) -> dict[str, Any]:
    if book_state_query is None or not project_id or not int(as_of_chapter or 0):
        return {}
    getter = getattr(book_state_query, "get_current_countdown_values", None)
    if getter is None:
        return {}
    return getter(project_id=project_id, as_of_chapter=int(as_of_chapter or 0)) or {}


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


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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


__all__ = ["select_ledger_state_drift_targets", "select_countdown_compat_drift_targets"]
