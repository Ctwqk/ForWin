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
    match = re.search(r"([0-9一二两三四五六七八九十零]+)多?个?(小时|分钟|分)", text)
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
    text_has_resolution = _text_has_countdown_resolution(text)
    previous_by_key = _latest_entries_by_key(previous_entries or [])
    previous_unresolved_by_key = _latest_unresolved_entries_by_key(previous_entries or [])
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
            repair_hint = _non_monotonic_repair_hint(
                countdown_key=countdown_key,
                raw=raw,
                previous_minutes=previous_minutes,
                minutes=minutes,
            )
            signals.append(
                CanonQualitySignal(
                    signal_id=make_signal_id(project_id, chapter_number, "countdown_non_monotonic", subject),
                    project_id=project_id,
                    chapter_number=chapter_number,
                    signal_type="countdown_non_monotonic",
                    severity="error",
                    target_scope="ledger",
                    subject_key=subject,
                    description=(
                        f"倒计时从 {previous_minutes} 分钟回升到 {minutes} 分钟，但正文没有明确 reset。"
                        f" 修复要求：{repair_hint}"
                    ),
                    evidence_refs=[f"body:{start}-{end}"],
                    span_start=start,
                    span_end=end,
                    payload={
                        "draft_id": draft_id,
                        "previous_minutes": previous_minutes,
                        "current_minutes": minutes,
                        "raw_mention": raw,
                        "repair_hint": repair_hint,
                    },
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
        subject = f"countdown:{entries[-1].countdown_key or 'main'}"
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
    elif is_final_chapter and not entries and previous_unresolved_by_key and not text_has_resolution:
        key, previous = sorted(
            previous_unresolved_by_key.items(),
            key=lambda item: int(item[1].get("chapter_number", 0) or 0),
        )[-1]
        subject = f"countdown:{key}"
        signals.append(
            CanonQualitySignal(
                signal_id=make_signal_id(project_id, chapter_number, "final_countdown_unresolved", subject),
                project_id=project_id,
                chapter_number=chapter_number,
                signal_type="final_countdown_unresolved",
                severity="error",
                target_scope="book",
                subject_key=subject,
                description="终章没有明确关闭前文未解决倒计时。",
                evidence_refs=list(previous.get("evidence_refs") or [f"chapter:{previous.get('chapter_number', 0)}"]),
                payload={"draft_id": draft_id, "previous_countdown": previous},
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

    simple_pattern = rf"([{_COUNTDOWN_NUMBER}]+)多?个?(小时|分钟|分)"
    for match in re.finditer(simple_pattern, text):
        if _overlaps(match.start(), match.end(), consumed):
            continue
        if _looks_like_time_of_day_minute(text, match.start(), match.end()):
            continue
        if _looks_like_effect_window(text, match.start(), match.end()):
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


def _looks_like_time_of_day_minute(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - 3) : start]
    after = text[end : min(len(text), end + 2)]
    return bool(before) and not before.endswith("计时") and before[-1:] in {"点", "时"} and after[:1] not in {"钟"}


def _looks_like_effect_window(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - 18) : start]
    after = text[end : min(len(text), end + 10)]
    if not after.startswith("内"):
        return False
    after_clause = re.split(r"[。！？!?；;]", after, maxsplit=1)[0]
    local = before + after_clause
    if any(keyword in local for keyword in ("倒计时", "剩余", "重置前", "调度窗口", "核心窗口")):
        return False
    return any(keyword in local for keyword in ("崩塌", "扩散", "传播", "传遍", "发酵", "瘫痪", "恢复"))


def _looks_like_retrospective_day_reference(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - 6) : start]
    after = text[end : min(len(text), end + 4)]
    return (
        before.endswith(("这", "那", "过去", "此前", "前面", "最近", "最后", "重置前最后"))
        or after.startswith(("里", "中", "以来", "发生", "发现", "内发生"))
    )


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
    return any(keyword in context for keyword in ("倒计时", "剩余", "距离", "归零", "计时", "重置", "窗口", "调度窗口"))


def _is_resolution_context(text: str) -> bool:
    return _text_has_countdown_resolution(str(text or ""))


def _text_has_countdown_resolution(text: str) -> bool:
    context = str(text or "")
    if any(
        keyword in context
        for keyword in (
            "归零",
            "解除",
            "关闭倒计时",
            "倒计时结束",
            "倒计时归零",
            "危机解除",
            "白塔系统关闭",
            "白塔系统已关闭",
            "白塔记忆重置系统失效",
            "白塔的记忆重置系统已经失效",
            "记忆重置系统失效",
            "记忆重置系统已经失效",
            "系统关闭",
            "系统已关闭",
            "记忆重置停止",
            "记忆重置被阻止",
            "记忆重置已取消",
            "重置程序终止",
            "重置程序已终止",
            "重置周期被阻止",
            "重置周期终止",
            "重置周期已终止",
            "重置周期被永久终止",
            "重置周期将被永久终止",
            "记忆重置周期被永久终止",
            "记忆重置周期将被永久终止",
            "记忆重置周期永久终止",
            "旧城将不再有记忆重置",
            "不再有记忆重置",
            "白塔将无法再进行下一次记忆重置",
        )
    ):
        return True
    if any(keyword in context for keyword in ("重置周期结束", "重置周期结束了")) and any(
        keyword in context
        for keyword in (
            "没有人会忘记",
            "记忆没有被抹去",
            "无人遗忘",
            "旧城终于自由",
            "旧城，终于自由",
            "被抹除的记忆",
        )
    ):
        return True
    return False


def _is_rounding_equivalent(raw: str, previous_minutes: int, minutes: int) -> bool:
    if minutes - previous_minutes > 1:
        return False
    mention = str(raw or "")
    return "小时" in mention and not any(unit in mention for unit in ("分钟", "分", ":"))


def _non_monotonic_repair_hint(*, countdown_key: str, raw: str, previous_minutes: int, minutes: int) -> str:
    if countdown_key == "archive_cleanup":
        return (
            f"不要把同一个终端审计/授权窗口从 {previous_minutes} 分钟延长到 {raw}。"
            "删除或改写这个数字，使审计窗口继续小于等于前值；如果要写七天记忆重置，"
            "必须明确它是 memory_reset 另一个倒计时，不能继续称为终端审计窗口。"
        )
    if countdown_key == "memory_reset":
        return (
            f"不要把同一个记忆重置周期从 {previous_minutes} 分钟延长到 {raw}。"
            "只能写剩余时间继续减少；若发生 reset，必须用明确重置事件解释。"
        )
    return (
        f"不要把同一个倒计时从 {previous_minutes} 分钟延长到 {raw}。"
        "删除或改写该数字，让剩余时间单调减少；若这是新时钟，必须明确命名为不同倒计时。"
    )


def _countdown_key_for_mention(text: str, start: int, end: int) -> str:
    before_context = str(text[max(0, start - 160) : end])
    after_context = str(text[start : min(len(text), end + 32)])
    immediate_after = str(text[end : min(len(text), end + 24)])
    if any(keyword in immediate_after for keyword in ("公开数据", "公开窗口", "对外数据", "公布数据", "心理缓冲", "普通市民")):
        return "public_countdown"
    key = _nearest_countdown_key(before_context)
    if key:
        return key
    key = _nearest_countdown_key(after_context)
    if key:
        return key
    if any(keyword in after_context for keyword in ("档案清理", "清除", "导出", "授权", "访问", "记录群组")):
        return "archive_cleanup"
    if any(keyword in after_context for keyword in ("记忆重置", "重置周期", "历史记录")):
        return "memory_reset"
    return "main"


def _nearest_countdown_key(context: str) -> str:
    keyword_keys = {
        "memory_reset": (
            "记忆重置",
            "重置周期",
            "重置窗口",
            "重置前",
            "重置结束",
            "重置倒计时",
            "真实窗口",
            "真正的核心",
            "核心调度窗口",
            "重置一次记忆",
            "每十年一次",
            "每十年重置",
            "全域记忆",
            "记忆归零",
            "全域重置",
            "历史记录",
        ),
        "archive_cleanup": (
            "档案清理",
            "清除",
            "导出",
            "授权",
            "访问",
            "记录群组",
            "授权码",
            "身份核验",
            "终端审计",
            "审计日志",
            "查询",
            "限制级信息",
        ),
        "public_countdown": (
            "公开数据",
            "公开窗口",
            "对外数据",
            "公布数据",
            "心理缓冲",
            "普通市民",
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
