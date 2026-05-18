from __future__ import annotations

import re
from typing import Any

from .filters import (
    _bridge_mentions_competing_countdown,
    _has_upper_bound_prefix,
    _is_countdown_context,
    _is_ignored_duration_reference,
    _is_reset_context,
    _is_resolution_context,
    _looks_like_effect_window,
    _looks_like_local_tactical_window,
    _looks_like_time_of_day_minute,
    _looks_like_wall_clock_reading,
)
from .keys import _countdown_key_for_mention
from .parsing import _COUNTDOWN_NUMBER, parse_chinese_number, parse_countdown_minutes
from .retrospective import _looks_like_retrospective_day_reference


def _iter_countdown_mentions(text: str) -> list[dict[str, Any]]:
    mentions: list[dict[str, Any]] = []
    consumed: list[tuple[int, int]] = []

    long_clock_pattern = r"(?<![0-9:])([0-9]{1,2}):([0-9]{1,3}):([0-9]{2}):([0-9]{2}):([0-9]{2})(?![0-9:])"
    for match in re.finditer(long_clock_pattern, text):
        if _looks_like_local_tactical_window(text, match.start(), match.end()):
            continue
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
        if _looks_like_local_tactical_window(text, match.start(), match.end()):
            continue
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

    chinese_unit_clock_pattern = (
        rf"([{_COUNTDOWN_NUMBER}]+)多?天"
        rf"[。．·、,\s]*([{_COUNTDOWN_NUMBER}]+)(?:小时|时)"
        rf"[。．·、,\s]*([{_COUNTDOWN_NUMBER}]+)(?:分钟|分)"
        rf"(?:[。．·、,\s]*([{_COUNTDOWN_NUMBER}]+)秒)?"
    )
    for match in re.finditer(chinese_unit_clock_pattern, text):
        if _overlaps(match.start(), match.end(), consumed):
            continue
        context = _mention_context(text, match.start(), match.end())
        if not _is_countdown_context(context):
            continue
        days = parse_chinese_number(match.group(1))
        hours = parse_chinese_number(match.group(2))
        minutes = parse_chinese_number(match.group(3))
        if days is None or hours is None or minutes is None:
            continue
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
        prev_char = text[match.start() - 1 : match.start()] if match.start() > 0 else ""
        if prev_char == "第":
            continue
        next_char = text[match.end() : match.end() + 1]
        next_two_chars = text[match.end() : match.end() + 2]
        if next_char in {"前", "后"} or next_two_chars in {"之前", "之后"}:
            continue
        if _looks_like_retrospective_day_reference(text, match.start(), match.end()):
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
        if _looks_like_wall_clock_reading(text, match.start(), match.end()):
            continue
        if _looks_like_local_tactical_window(text, match.start(), match.end()):
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

    simple_pattern = rf"([{_COUNTDOWN_NUMBER}]+)(?:个?多|多?个?)?(小时|分钟|分)"
    for match in re.finditer(simple_pattern, text):
        if _overlaps(match.start(), match.end(), consumed):
            continue
        if _looks_like_time_of_day_minute(text, match.start(), match.end()):
            continue
        if _looks_like_effect_window(text, match.start(), match.end()):
            continue
        if _looks_like_local_tactical_window(text, match.start(), match.end()):
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
                "is_upper_bound": _has_upper_bound_prefix(text, match.start()),
            }
        )
        consumed.append((match.start(), match.end()))

    mentions = sorted(mentions, key=lambda item: int(item["start"]))
    mentions = [
        mention
        for mention in mentions
        if not _is_ignored_duration_reference(text, int(mention["start"]), int(mention["end"]))
    ]
    previous: dict[str, Any] | None = None
    for mention in mentions:
        if (
            previous is not None
            and int(mention["minutes"]) == int(previous["minutes"])
            and int(mention["start"]) - int(previous["end"]) <= 96
        ):
            bridge = text[int(previous["end"]) : int(mention["start"])]
            if any(cue in bridge for cue in ("正好", "就是", "仍是", "还是", "也就")):
                mention["key"] = previous["key"]
        if (
            previous is not None
            and mention.get("key") == "main"
            and previous.get("key") != "main"
            and int(mention["start"]) - int(previous["end"]) <= 768
        ):
            bridge = text[int(previous["end"]) : int(mention["start"])]
            if (
                not _is_reset_context(bridge)
                and not _is_resolution_context(bridge)
                and not _bridge_mentions_competing_countdown(bridge)
            ):
                mention["key"] = previous["key"]
        previous = mention
    return mentions


def _mention_context(text: str, start: int, end: int, *, window: int = 40) -> str:
    return text[max(0, start - window) : min(len(text), end + window)]


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def _is_reset_context_for_mention(text: str, start: int, end: int) -> bool:
    context = _mention_context(text, start, end)
    if _is_reset_context(context):
        return True
    before = str(text[max(0, start - 56) : start])
    if not any(marker in before for marker in ("倒计时", "计时器", "剩余", "窗口", "调度窗口")):
        return False
    action_markers = (
        "修正",
        "修订",
        "调整",
        "改为",
        "改成",
        "修改",
        "延长",
        "重设",
        "校准",
        "回拨",
        "覆盖为",
        "设置为",
    )
    action_index = max(before.rfind(marker) for marker in action_markers)
    if action_index < 0:
        return False
    tail = before[action_index:]
    return any(marker in tail for marker in ("至", "到", "为", "成"))


__all__ = [
    '_iter_countdown_mentions',
    '_mention_context',
    '_overlaps',
    '_is_reset_context_for_mention',
]
