from __future__ import annotations

import re
from typing import Any

from .signals import CanonQualitySignal, CountdownLedgerEntry, make_signal_id

_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


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
    match = re.search(r"([0-9一二两三四五六七八九十零]+)多?(小时|分钟|分)", text)
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


def analyze_countdowns(
    *,
    project_id: str,
    chapter_number: int,
    draft_id: str = "",
    body: str,
    previous_entries: list[dict[str, Any] | CountdownLedgerEntry] | None = None,
    is_final_chapter: bool = False,
) -> tuple[list[CanonQualitySignal], list[CountdownLedgerEntry]]:
    text = str(body or "")
    signals: list[CanonQualitySignal] = []
    entries: list[CountdownLedgerEntry] = []
    text_has_resolution = any(keyword in text for keyword in ("归零", "解除", "关闭倒计时", "倒计时结束", "危机解除"))
    previous_by_key = _latest_entries_by_key(previous_entries or [])
    last_is_reset = False
    last_is_resolution = False
    for mention in _iter_countdown_mentions(text):
        start = int(mention["start"])
        end = int(mention["end"])
        raw = str(mention["raw"])
        minutes = int(mention["minutes"])
        mention_context = str(mention["context"])
        countdown_key = str(mention.get("key") or "main")
        previous_minutes = previous_by_key.get(countdown_key)
        is_reset = _is_reset_context(mention_context)
        is_resolution = text_has_resolution or _is_resolution_context(mention_context)
        last_is_reset = is_reset
        last_is_resolution = is_resolution
        status = "resolved" if is_resolution else "consistent"
        if (
            previous_minutes is not None
            and minutes > previous_minutes
            and not is_reset
            and not _is_rounding_equivalent(raw, previous_minutes, minutes)
        ):
            status = "conflict"
            subject = f"countdown:{countdown_key}"
            signals.append(
                CanonQualitySignal(
                    signal_id=make_signal_id(project_id, chapter_number, "countdown_non_monotonic", subject),
                    project_id=project_id,
                    chapter_number=chapter_number,
                    signal_type="countdown_non_monotonic",
                    severity="error",
                    target_scope="ledger",
                    subject_key=subject,
                    description=f"倒计时从 {previous_minutes} 分钟回升到 {minutes} 分钟，但正文没有明确 reset。",
                    evidence_refs=[f"body:{start}-{end}"],
                    span_start=start,
                    span_end=end,
                    payload={"draft_id": draft_id, "previous_minutes": previous_minutes, "current_minutes": minutes},
                )
            )
        entries.append(
            CountdownLedgerEntry(
                project_id=project_id,
                countdown_key=countdown_key,
                label=countdown_key,
                chapter_number=chapter_number,
                normalized_remaining_minutes=minutes,
                raw_mention=raw,
                is_reset_event=is_reset,
                is_resolution_event=is_resolution,
                previous_remaining_minutes=previous_minutes,
                status=status,  # type: ignore[arg-type]
                evidence_refs=[f"body:{start}-{end}"],
                payload={"draft_id": draft_id, "is_final_chapter": is_final_chapter},
            )
        )
        previous_by_key[countdown_key] = minutes
    if is_final_chapter and entries and not last_is_resolution and not last_is_reset:
        subject = "countdown:main"
        signals.append(
            CanonQualitySignal(
                signal_id=make_signal_id(project_id, chapter_number, "final_countdown_unresolved", subject),
                project_id=project_id,
                chapter_number=chapter_number,
                signal_type="final_countdown_unresolved",
                severity="error",
                target_scope="book",
                subject_key=subject,
                description="终章仍留下未关闭倒计时。",
                evidence_refs=entries[-1].evidence_refs,
                payload={"draft_id": draft_id},
            )
        )
    return signals, entries


_COUNTDOWN_NUMBER = r"0-9一二两三四五六七八九十零"


def _iter_countdown_mentions(text: str) -> list[dict[str, Any]]:
    mentions: list[dict[str, Any]] = []
    consumed: list[tuple[int, int]] = []

    long_clock_pattern = r"(?<![0-9:])([0-9]{1,2}):([0-9]{1,3}):([0-9]{2}):([0-9]{2}):([0-9]{2})(?![0-9:])"
    for match in re.finditer(long_clock_pattern, text):
        context = _mention_context(text, match.start(), match.end())
        if not _is_countdown_context(context):
            continue
        days = int(match.group(2))
        hours = int(match.group(3))
        minutes = int(match.group(4))
        mentions.append(
            {
                "start": match.start(),
                "end": match.end(),
                "raw": match.group(0),
                "minutes": days * 24 * 60 + hours * 60 + minutes,
                "context": context,
                "key": _countdown_key_for_mention(text, match.start(), match.end()),
            }
        )
        consumed.append((match.start(), match.end()))

    day_clock_pattern = r"(?<![0-9:])([0-9]{1,3}):([0-9]{2}):([0-9]{2}):([0-9]{2})(?![0-9:])"
    for match in re.finditer(day_clock_pattern, text):
        context = _mention_context(text, match.start(), match.end())
        if not _is_countdown_context(context):
            continue
        days = int(match.group(1))
        hours = int(match.group(2))
        minutes = int(match.group(3))
        mentions.append(
            {
                "start": match.start(),
                "end": match.end(),
                "raw": match.group(0),
                "minutes": days * 24 * 60 + hours * 60 + minutes,
                "context": context,
                "key": _countdown_key_for_mention(text, match.start(), match.end()),
            }
        )
        consumed.append((match.start(), match.end()))

    compound_pattern = (
        rf"([{_COUNTDOWN_NUMBER}]+)多?天"
        rf"(?:[。．·、,\s]*([{_COUNTDOWN_NUMBER}]+)小时)?"
        rf"(?:[。．·、,\s]*([{_COUNTDOWN_NUMBER}]+)(?:分钟|分))?"
    )
    for match in re.finditer(compound_pattern, text):
        if _overlaps(match.start(), match.end(), consumed):
            continue
        next_char = text[match.end() : match.end() + 1]
        if next_char in {"前", "后"}:
            continue
        context = _mention_context(text, match.start(), match.end())
        if not _is_countdown_context(context):
            continue
        days = parse_chinese_number(match.group(1))
        hours = parse_chinese_number(match.group(2) or "") if match.group(2) else 0
        minutes = parse_chinese_number(match.group(3) or "") if match.group(3) else 0
        if days is None:
            continue
        mentions.append(
            {
                "start": match.start(),
                "end": match.end(),
                "raw": match.group(0),
                "minutes": days * 24 * 60 + int(hours or 0) * 60 + int(minutes or 0),
                "context": context,
                "key": _countdown_key_for_mention(text, match.start(), match.end()),
            }
        )
        consumed.append((match.start(), match.end()))

    clock_pattern = r"(?<![0-9:])([0-9]{1,3}):([0-9]{2})(?::([0-9]{2}))?(?![0-9:])"
    for match in re.finditer(clock_pattern, text):
        if _overlaps(match.start(), match.end(), consumed):
            continue
        context = _mention_context(text, match.start(), match.end())
        if not _is_countdown_context(context):
            continue
        mentions.append(
            {
                "start": match.start(),
                "end": match.end(),
                "raw": match.group(0),
                "minutes": int(match.group(1)) * 60 + int(match.group(2)),
                "context": context,
                "key": _countdown_key_for_mention(text, match.start(), match.end()),
            }
        )
        consumed.append((match.start(), match.end()))

    simple_pattern = rf"([{_COUNTDOWN_NUMBER}]+)多?(小时|分钟|分)"
    for match in re.finditer(simple_pattern, text):
        if _overlaps(match.start(), match.end(), consumed):
            continue
        context = _mention_context(text, match.start(), match.end())
        if not _is_countdown_context(context):
            continue
        minutes = parse_countdown_minutes(match.group(0))
        if minutes is None:
            continue
        mentions.append(
            {
                "start": match.start(),
                "end": match.end(),
                "raw": match.group(0),
                "minutes": minutes,
                "context": context,
                "key": _countdown_key_for_mention(text, match.start(), match.end()),
            }
        )
        consumed.append((match.start(), match.end()))

    mentions = sorted(mentions, key=lambda item: int(item["start"]))
    previous: dict[str, Any] | None = None
    for mention in mentions:
        if (
            previous is not None
            and mention.get("key") == "main"
            and previous.get("key") != "main"
            and int(mention["start"]) - int(previous["end"]) <= 48
        ):
            bridge = text[int(previous["end"]) : int(mention["start"])]
            if not _is_reset_context(bridge) and not _is_resolution_context(bridge):
                mention["key"] = previous["key"]
        previous = mention
    return mentions


def _mention_context(text: str, start: int, end: int, *, window: int = 40) -> str:
    return text[max(0, start - window) : min(len(text), end + window)]


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def _is_reset_context(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(
        keyword in lowered
        for keyword in (
            "倒计时重置",
            "倒计时被重置",
            "重新计时",
            "新的倒计时",
            "新倒计时",
            "clock reset",
            "timer reset",
        )
    )


def _is_countdown_context(text: str) -> bool:
    context = str(text or "")
    return any(keyword in context for keyword in ("倒计时", "剩余", "距离", "归零", "计时", "重置"))


def _is_resolution_context(text: str) -> bool:
    return any(keyword in str(text or "") for keyword in ("关闭倒计时", "倒计时结束", "倒计时归零", "危机解除"))


def _is_rounding_equivalent(raw: str, previous_minutes: int, minutes: int) -> bool:
    if minutes - previous_minutes > 1:
        return False
    mention = str(raw or "")
    return "小时" in mention and not any(unit in mention for unit in ("分钟", "分", ":"))


def _countdown_key_for_mention(text: str, start: int, end: int) -> str:
    before_context = str(text[max(0, start - 96) : end])
    after_context = str(text[start : min(len(text), end + 12)])
    key = _nearest_countdown_key(before_context)
    if key:
        return key
    if any(keyword in after_context for keyword in ("档案清理", "清除", "导出", "授权", "访问", "记录群组")):
        return "archive_cleanup"
    if any(keyword in after_context for keyword in ("记忆重置", "重置周期", "历史记录")):
        return "memory_reset"
    return "main"


def _nearest_countdown_key(context: str) -> str:
    keyword_keys = {
        "memory_reset": ("记忆重置", "重置周期", "历史记录"),
        "archive_cleanup": (
            "档案清理",
            "清除",
            "导出",
            "授权",
            "访问",
            "记录群组",
            "授权码",
            "身份核验",
            "审计日志",
            "查询",
            "限制级信息",
        ),
    }
    nearest_key = ""
    nearest_index = -1
    for key, keywords in keyword_keys.items():
        for keyword in keywords:
            index = context.rfind(keyword)
            if index > nearest_index:
                nearest_key = key
                nearest_index = index
    return nearest_key


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
