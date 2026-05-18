from __future__ import annotations

import logging
from typing import Any


OPEN_COUNTDOWN_STATUSES = {"active", "paused", "reopened", "consistent", "warning", "conflict"}
TERMINAL_COUNTDOWN_STATUSES = {"closed", "fulfilled", "resolved"}
BLOCKING_SIGNAL_SEVERITIES = {"error", "critical", "blocker"}
SIGNAL_SEVERITY_ORDER = {"blocker": 0, "critical": 1, "error": 2, "warning": 3, "info": 4}
COUNTDOWN_STATUS_PRIORITY = {
    "reopened": 0,
    "active": 1,
    "paused": 2,
    "warning": 3,
    "conflict": 4,
    "fulfilled": 5,
    "closed": 6,
    "resolved": 7,
    "consistent": 8,
}

_PROTECTED_SIGNAL_SEVERITIES = {"blocker", "critical", "error"}
_PROTECTED_COUNTDOWN_STATUSES = {"reopened", "active"}
_LOGGER = logging.getLogger(__name__)


class FormBudgetExceeded(RuntimeError):
    def __init__(
        self,
        protected_counts: dict[str, int],
        *,
        form: Any | None = None,
        max_chars: int = 0,
        actual_chars: int = 0,
    ) -> None:
        self.protected_counts = protected_counts
        self.form = form
        self.max_chars = int(max_chars or 0)
        self.actual_chars = int(actual_chars or 0)
        super().__init__(f"Chapter review form protected items exceed budget: {protected_counts}")


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
        age = int(chapter_number or 0) - int(row_value(row, "chapter_number", chapter_number) or chapter_number)
        if status == "open" and age >= 2:
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


def fit_form_budget(form: Any, *, max_chars: int) -> Any:
    max_chars = int(max_chars or 0)
    _sort_for_pruning(form)
    dropped = {"open_signals": 0, "obligations": 0, "countdowns": 0, "characters": 0}

    while _form_size(form) > max_chars:
        if _drop_lowest_priority(form.open_signals, _is_protected_signal):
            dropped["open_signals"] += 1
            continue
        if _drop_lowest_priority(form.obligations, _is_protected_obligation):
            dropped["obligations"] += 1
            continue
        if _drop_lowest_priority(form.countdowns, _is_protected_countdown):
            dropped["countdowns"] += 1
            continue
        if _drop_lowest_priority(form.characters, _is_protected_character):
            dropped["characters"] += 1
            continue
        raise FormBudgetExceeded(
            _protected_counts(form),
            form=form,
            max_chars=max_chars,
            actual_chars=_form_size(form),
        )

    if any(dropped.values()):
        _LOGGER.info(
            "Pruned chapter review form to fit budget: open_signals=%s obligations=%s countdowns=%s characters=%s final_chars=%s max_chars=%s",
            dropped["open_signals"],
            dropped["obligations"],
            dropped["countdowns"],
            dropped["characters"],
            _form_size(form),
            max_chars,
        )
    return form


def _sort_for_pruning(form: Any) -> None:
    form.open_signals.sort(key=_signal_sort_key)
    form.obligations.sort(key=_obligation_sort_key)
    form.countdowns.sort(key=_countdown_sort_key)
    form.characters.sort(key=_character_sort_key)


def _drop_lowest_priority(items: list[Any], is_protected: Any) -> bool:
    for index in range(len(items) - 1, -1, -1):
        if not is_protected(items[index]):
            items.pop(index)
            return True
    return False


def _signal_sort_key(item: Any) -> tuple[int, str]:
    severity = str(row_value(item, "severity", "warning") or "warning").strip().lower()
    return (SIGNAL_SEVERITY_ORDER.get(severity, SIGNAL_SEVERITY_ORDER["warning"]), str(row_value(item, "id", "")))


def _obligation_sort_key(item: Any) -> tuple[bool, int, str]:
    deadline = int(row_value(item, "deadline_chapter", 0) or 0)
    return (
        not bool(row_value(item, "must_resolve_now", False)),
        -deadline,
        str(row_value(item, "id", "")),
    )


def _countdown_sort_key(item: Any) -> tuple[int, str]:
    status = str(row_value(item, "prior_status", row_value(item, "status", "active")) or "active").strip().lower()
    return (COUNTDOWN_STATUS_PRIORITY.get(status, COUNTDOWN_STATUS_PRIORITY["consistent"]), str(row_value(item, "key", "")))


def _character_sort_key(item: Any) -> tuple[bool, int, str]:
    return (
        not bool(row_value(item, "must_track", False)),
        -int(row_value(item, "last_seen_chapter", row_value(item, "chapter_number", 0)) or 0),
        str(row_value(item, "name", row_value(item, "character_name", ""))),
    )


def _is_protected_signal(item: Any) -> bool:
    severity = str(row_value(item, "severity", "warning") or "warning").strip().lower()
    return severity in _PROTECTED_SIGNAL_SEVERITIES


def _is_protected_obligation(item: Any) -> bool:
    return bool(row_value(item, "must_resolve_now", False))


def _is_protected_countdown(item: Any) -> bool:
    status = str(row_value(item, "prior_status", row_value(item, "status", "active")) or "active").strip().lower()
    return status in _PROTECTED_COUNTDOWN_STATUSES


def _is_protected_character(item: Any) -> bool:
    return bool(row_value(item, "must_track", False))


def _protected_counts(form: Any) -> dict[str, int]:
    return {
        "open_signals": sum(1 for item in form.open_signals if _is_protected_signal(item)),
        "obligations": sum(1 for item in form.obligations if _is_protected_obligation(item)),
        "countdowns": sum(1 for item in form.countdowns if _is_protected_countdown(item)),
        "characters": sum(1 for item in form.characters if _is_protected_character(item)),
    }


def _form_size(form: Any) -> int:
    return len(form.model_dump_json())
