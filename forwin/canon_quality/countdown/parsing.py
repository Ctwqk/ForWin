from __future__ import annotations

import re
from typing import Any

from ..signals import CountdownLedgerEntry

_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_COUNTDOWN_NUMBER = r"0-9一二两三四五六七八九十零"


def parse_chinese_number(value: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text in _DIGITS:
        return _DIGITS[text]
    if text == "十":
        return 10
    if "十" in text:
        left, right = text.split("十", 1)
        tens = _DIGITS.get(left, 1) if left else 1
        ones = _DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(text) == 2 and all(char in _DIGITS for char in text):
        return _DIGITS[text[0]] * 10 + _DIGITS[text[1]]
    return None


def parse_countdown_minutes(raw: str) -> int | None:
    text = str(raw or "").strip()
    half_match = re.search(r"([0-9一二两三四五六七八九十零]+)个?半(小时|分钟|分)", text)
    if half_match:
        number = parse_chinese_number(half_match.group(1))
        if number is None:
            return None
        unit = half_match.group(2)
        if unit == "小时":
            return number * 60 + 30
        return number + 1
    compound = re.search(
        r"([0-9一二两三四五六七八九十零]+)多?天(?:([0-9一二两三四五六七八九十零]+)小时)?",
        text,
    )
    if compound:
        days = parse_chinese_number(compound.group(1))
        hours = parse_chinese_number(compound.group(2) or "") if compound.group(2) else 0
        if days is None:
            return None
        return days * 24 * 60 + int(hours or 0) * 60
    match = re.search(r"([0-9一二两三四五六七八九十零]+)(?:个?多|多?个?)?(小时|分钟|分)", text)
    if not match:
        return None
    number = parse_chinese_number(match.group(1))
    if number is None:
        return None
    unit = match.group(2)
    if unit == "天":
        return number * 24 * 60
    if unit == "小时":
        return number * 60
    return number


def _latest_entry(entries: list[dict[str, Any] | CountdownLedgerEntry]) -> dict[str, Any] | None:
    if not entries:
        return None
    normalized = [
        item.model_dump(mode="json") if isinstance(item, CountdownLedgerEntry) else dict(item)
        for item in entries
    ]
    return sorted(normalized, key=lambda item: int(item.get("chapter_number", 0) or 0))[-1]


def _latest_entries_by_key(entries: list[dict[str, Any] | CountdownLedgerEntry]) -> dict[str, int]:
    result: dict[str, int] = {}
    normalized = [
        item.model_dump(mode="json") if isinstance(item, CountdownLedgerEntry) else dict(item)
        for item in entries
    ]
    for item in sorted(normalized, key=lambda raw: int(raw.get("chapter_number", 0) or 0)):
        key = str(item.get("countdown_key") or "main")
        result[key] = int(item.get("normalized_remaining_minutes", 0) or 0)
    return result


def _latest_unresolved_entries_by_key(entries: list[dict[str, Any] | CountdownLedgerEntry]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    normalized = [
        item.model_dump(mode="json") if isinstance(item, CountdownLedgerEntry) else dict(item)
        for item in entries
    ]
    for item in sorted(normalized, key=lambda raw: int(raw.get("chapter_number", 0) or 0)):
        key = str(item.get("countdown_key") or "main")
        if str(item.get("status") or "") == "resolved" or bool(item.get("is_resolution_event")):
            result.pop(key, None)
            continue
        result[key] = item
    return result


__all__ = [
    'parse_chinese_number',
    'parse_countdown_minutes',
    '_latest_entry',
    '_latest_entries_by_key',
    '_latest_unresolved_entries_by_key',
]
