from __future__ import annotations

from typing import Any


OPEN_COUNTDOWN_STATUSES = {"active", "paused", "reopened", "consistent", "warning", "conflict"}
TERMINAL_COUNTDOWN_STATUSES = {"closed", "fulfilled", "resolved"}
BLOCKING_SIGNAL_SEVERITIES = {"error", "critical", "blocker"}


def row_value(row: Any, key: str, default: Any = "") -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def select_characters_to_ask(*, rows: list[Any], chapter_text: str) -> list[Any]:
    latest_by_name: dict[str, Any] = {}
    for row in rows:
        name = str(row_value(row, "character_name") or row_value(row, "name") or "").strip()
        if not name:
            continue
        previous = latest_by_name.get(name)
        if previous is None or int(row_value(row, "chapter_number", 0) or 0) >= int(row_value(previous, "chapter_number", 0) or 0):
            latest_by_name[name] = row
    selected: list[Any] = []
    for name, row in sorted(latest_by_name.items()):
        payload = row_value(row, "payload", {}) or {}
        must_track = bool(payload.get("must_track") if isinstance(payload, dict) else False)
        if must_track or name in chapter_text:
            selected.append(row)
    return selected


def select_countdowns_to_ask(*, rows: list[Any], chapter_text: str) -> list[Any]:
    latest_by_key: dict[str, Any] = {}
    for row in rows:
        key = str(row_value(row, "countdown_key") or row_value(row, "key") or "main").strip() or "main"
        previous = latest_by_key.get(key)
        if previous is None or int(row_value(row, "chapter_number", 0) or 0) >= int(row_value(previous, "chapter_number", 0) or 0):
            latest_by_key[key] = row
    selected: list[Any] = []
    for key, row in sorted(latest_by_key.items()):
        label = str(row_value(row, "label") or key).strip()
        status = str(row_value(row, "status") or "active").strip()
        mentioned = key in chapter_text or bool(label and label in chapter_text)
        if status in OPEN_COUNTDOWN_STATUSES or mentioned:
            selected.append(row)
        elif status in TERMINAL_COUNTDOWN_STATUSES and mentioned:
            selected.append(row)
    return selected


def select_signals_to_ask(*, rows: list[Any], chapter_number: int) -> list[Any]:
    selected: list[Any] = []
    for row in rows:
        status = str(row_value(row, "status", "open") or "open")
        severity = str(row_value(row, "severity", "") or "")
        age = int(chapter_number or 0) - int(row_value(row, "chapter_number", chapter_number) or chapter_number)
        if status == "open" and severity in BLOCKING_SIGNAL_SEVERITIES and age >= 2:
            selected.append(row)
    return selected


def select_obligations_to_ask(*, obligations: list[Any], chapter_number: int) -> list[Any]:
    selected: list[Any] = []
    for obligation in obligations:
        status = str(row_value(obligation, "status", "active") or "active")
        if status in {"resolved", "fulfilled", "waived"}:
            continue
        deadline = int(row_value(obligation, "deadline_chapter", 0) or 0)
        must_resolve = bool(row_value(obligation, "must_resolve_now", False))
        if must_resolve or (deadline and deadline <= int(chapter_number or 0)):
            selected.append(obligation)
    return selected
