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
    signals.extend(
        _analyze_stale_retrospective_references(
            project_id=project_id,
            chapter_number=chapter_number,
            draft_id=draft_id,
            text=text,
            previous_by_key=previous_by_key,
        )
    )
    last_is_reset = False
    last_is_resolution = False
    for mention in _iter_countdown_mentions(text):
        start = int(mention["start"])
        end = int(mention["end"])
        raw = str(mention["raw"])
        minutes = int(mention["minutes"])
        mention_context = str(mention["context"])
        countdown_key = str(mention.get("key") or "main")
        countdown_key = _prefer_active_memory_reset_key(countdown_key, minutes, previous_by_key)
        countdown_key = _prefer_short_clock_continuation_key(
            countdown_key, raw, previous_by_key, mention_context
        )
        previous_minutes = previous_by_key.get(countdown_key)
        minutes = _normalize_ambiguous_clock_minutes(
            raw=raw,
            minutes=minutes,
            previous_minutes=previous_minutes,
            countdown_key=countdown_key,
            context=mention_context,
        )
        is_reset = _is_reset_context(mention_context)
        is_resolution = text_has_resolution or _is_resolution_context(mention_context)
        last_is_reset = is_reset
        last_is_resolution = is_resolution
        status = "resolved" if is_resolution else "consistent"
        rounding_equivalent = (
            previous_minutes is not None
            and minutes > previous_minutes
            and _is_rounding_equivalent(
                raw,
                previous_minutes,
                minutes,
                is_upper_bound=bool(mention.get("is_upper_bound")),
                context=mention_context,
            )
        )
        if previous_minutes is not None and minutes > previous_minutes and not is_reset and not rounding_equivalent:
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
        elif rounding_equivalent and previous_minutes is not None and minutes - previous_minutes > 1:
            minutes = previous_minutes
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


def _looks_like_local_tactical_window(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    if any(
        keyword in clause
        for keyword in (
            "倒计时",
            "剩余",
            "重置",
            "归零",
            "计时",
            "核心窗口",
            "调度窗口",
            "审计窗口",
            "终端审计",
            "授权窗口",
            "档案清理",
            "档案抹除",
        )
    ):
        return False
    if "窗口" in clause and any(
        keyword in clause
        for keyword in (
            "最多还有",
            "至多还有",
            "还能维持",
            "可以维持",
            "搜索范围",
            "换防间隙",
            "封锁",
            "警报",
        )
    ):
        return True
    return any(
        keyword in clause
        for keyword in (
            "争取",
            "拖住",
            "拖延",
            "掩护",
            "排查",
            "抵达",
            "赶到",
            "到达",
            "接触",
            "派遣",
            "巡检员",
            "巡检部队",
            "追兵",
            "封锁圈",
            "搜索范围",
        )
    )


def _is_ignored_duration_reference(text: str, start: int, end: int) -> bool:
    return (
        _is_negated_duration_reference(text, start, end)
        or _is_negated_baseline_duration_reference(text, start, end)
        or _is_negated_cycle_extension_reference(text, start, end)
        or _is_retrospective_duration_reference(text, start, end)
        or _is_static_duration_reference(text, start, end)
        or _is_policy_threshold_duration_reference(text, start, end)
        or _is_observation_duration_reference(text, start, end)
        or _is_wait_duration_reference(text, start, end)
        or _is_frequency_duration_reference(text, start, end)
        or _is_countdown_cost_duration_reference(text, start, end)
        or _is_internal_audit_delay_reference(text, start, end)
        or _is_local_memory_erosion_threshold_duration_reference(text, start, end)
        or _is_local_operation_window_duration_reference(text, start, end)
        or _is_tracker_unlock_window_duration_reference(text, start, end)
        or _is_detention_review_window_duration_reference(text, start, end)
        or _is_travel_duration_reference(text, start, end)
        or _is_delta_duration_reference(text, start, end)
        or _is_elapsed_duration_reference(text, start, end)
        or _is_elapsed_to_baseline_duration_reference(text, start, end)
        or _is_hypothetical_completion_duration_reference(text, start, end)
        or _is_hypothetical_protocol_compression_reference(text, start, end)
        or _is_protocol_result_duration_reference(text, start, end)
        or _is_interception_eta_duration_reference(text, start, end)
        or _is_approximate_window_duration_reference(text, start, end)
        or _is_scheduled_remaining_threshold_reference(text, start, end)
        or _is_activation_window_threshold_reference(text, start, end)
        or _is_future_offset_duration_reference(text, start, end)
        or _is_local_operation_eta_duration_reference(text, start, end)
        or _is_action_deadline_duration_reference(text, start, end)
        or _is_decision_deadline_duration_reference(text, start, end)
        or _is_access_token_validity_duration_reference(text, start, end)
        or _is_hypothetical_letter_day_reference(text, start, end)
        or _is_generic_remaining_time_restatement(text, start, end)
    )


def _is_negated_duration_reference(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - 8) : start]
    local = text[max(0, start - 8) : min(len(text), end + 4)]
    if "不是还有" in local or "不是还剩" in local:
        return False
    clause = _current_clause(text, start, end)
    clause_offset = start - max(text.rfind(marker, 0, start) for marker in ("。", "！", "？", "；", "\n")) - 1
    prefix = clause[: max(0, clause_offset)]
    return (
        before.endswith(("不是", "并非", "绝非", "不再是", "而不是", "也不是"))
        or "不是之前说的" in prefix
        or "不是原本说的" in prefix
        or prefix.rstrip().endswith(("也不是", "并非", "而不是"))
    )


def _is_negated_baseline_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before_in_clause = clause[: max(0, start - _clause_start_index(text, start))]
    return (
        "不是" in clause
        and any(marker in clause for marker in ("这一次", "本次", "而是", "是"))
        and before_in_clause.rstrip().endswith(("是", "而是", "而是："))
    )


def _is_negated_cycle_extension_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before_in_clause = clause[: max(0, start - _clause_start_index(text, start))]
    if any(keyword in before_in_clause for keyword in ("剩余", "还剩", "只剩", "显示", "跳到", "跳至")):
        return False
    return any(marker in before_in_clause for marker in ("不会", "不再", "不得", "不能")) and any(
        marker in before_in_clause
        for marker in (
            "重启成",
            "重置成",
            "跳回",
            "扩展到",
            "延长到",
            "归零重启成",
            "触发新的",
        )
    )


def _is_retrospective_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 12) : start]
    after = text[end : min(len(text), end + 4)]
    if before.endswith(("现在只剩", "现在还剩", "此刻只剩", "此刻还剩")):
        return False
    if any(marker in clause for marker in ("一直以为", "以为还有")) and "时间" in clause:
        return True
    if "昏迷前" in clause and any(marker in clause for marker in ("倒计时", "计时器", "还有", "还剩", "显示")):
        return True
    if before.endswith(("昨天的", "标准")):
        return True
    if any(marker in clause for marker in ("记得", "回忆")) and any(
        marker in clause
        for marker in (
            "看到的是",
            "显示的是",
            "看到的时间",
            "看到的倒计时",
            "看到的终端显示的是",
        )
    ):
        return True
    if any(marker in clause for marker in ("说过", "曾说", "曾经说", "告诉过")) and any(
        marker in clause for marker in ("会在", "将在", "以内", "内触发", "触发", "启动")
    ):
        return True
    if any(marker in clause for marker in ("上一次", "上一回", "上一轮", "上回")) and "还是" in clause:
        return True
    if "上次" in clause and any(marker in clause for marker in ("看到", "还有", "还剩", "显示")):
        return True
    if any(
        marker in clause
        for marker in ("上周", "上一次", "上一回", "上一轮", "上回", "之前", "此前", "原本", "过去", "当时", "昨天")
    ):
        if any(
            marker in clause
            for marker in ("怎么会", "怎么可能", "那时", "那会", "内", "前", "后", "说的", "写的是", "还有", "还是", "压缩")
        ):
            return True
    return after.startswith(("前", "后", "以前", "之后")) and not any(
        keyword in clause for keyword in ("倒计时", "剩余", "还剩", "只剩", "距离", "窗口", "关闭")
    )


def _analyze_stale_retrospective_references(
    *,
    project_id: str,
    chapter_number: int,
    draft_id: str,
    text: str,
    previous_by_key: dict[str, int],
) -> list[CanonQualitySignal]:
    previous_minutes = previous_by_key.get("memory_reset")
    if previous_minutes is None or previous_minutes > 180:
        return []
    signals: list[CanonQualitySignal] = []
    duration_pattern = rf"([{_COUNTDOWN_NUMBER}]+)(?:个?多|多?个?)?(天|小时|分钟|分)"
    for match in re.finditer(duration_pattern, text):
        start, end = match.start(), match.end()
        clause = _current_clause(text, start, end)
        if not _looks_like_stale_retrospective_clause(clause):
            continue
        if _is_policy_threshold_duration_reference(text, start, end):
            continue
        if text[end : min(len(text), end + 1)] == "前":
            continue
        nearby = text[max(0, start - 48) : min(len(text), end + 120)]
        if _looks_like_public_decoy_clause(clause) or _looks_like_public_decoy_clause(nearby):
            continue
        minutes = parse_countdown_minutes(match.group(0))
        if minutes is None or minutes <= previous_minutes + 10:
            continue
        subject = "countdown:memory_reset"
        signals.append(
            CanonQualitySignal(
                signal_id=make_signal_id(
                    project_id,
                    chapter_number,
                    "countdown_stale_retrospective_reference",
                    subject,
                    len(signals) + 1,
                ),
                project_id=project_id,
                chapter_number=chapter_number,
                signal_type="countdown_stale_retrospective_reference",
                severity="error",
                target_scope="ledger",
                subject_key=subject,
                description=(
                    f"正文回溯旧倒计时为 {match.group(0)}，但 accepted canon 中记忆重置倒计时已是"
                    f" {previous_minutes} 分钟级别。不能把旧计划/旧草稿时间写成前文事实。"
                ),
                evidence_refs=[f"body:{start}-{end}"],
                span_start=start,
                span_end=end,
                payload={
                    "draft_id": draft_id,
                    "previous_minutes": previous_minutes,
                    "referenced_minutes": minutes,
                    "raw_mention": match.group(0),
                    "repair_hint": "删除几天/几小时的回溯说法，或明确写成公开伪数据而不是主角此前真实剩余时间。",
                },
            )
        )
    return signals


def _looks_like_stale_retrospective_clause(clause: str) -> bool:
    local = str(clause or "")
    if not any(keyword in local for keyword in ("倒计时", "重置周期", "周期", "计时器", "校准预告", "时间", "还有", "还剩", "显示", "屏幕")):
        return False
    return any(
        marker in local
        for marker in (
            "昏迷前",
            "之前",
            "此前",
            "先前",
            "刚才",
            "刚刚",
            "原本",
            "昨天",
            "上一回",
            "上一次",
            "上一轮",
            "上回",
            "一直以为",
            "以为还有",
        )
    )


def _looks_like_public_decoy_clause(clause: str) -> bool:
    local = str(clause or "")
    return any(
        marker in local
        for marker in (
            "公开数据",
            "官方数据",
            "对外数据",
            "对外公布",
            "公共时间",
            "伪称",
            "假时间",
            "心理缓冲",
            "普通市民",
        )
    )


def _is_static_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 16) : start]
    if any(keyword in before for keyword in ("剩余", "还剩", "只剩", "倒计时显示", "显示", "跳到", "跳至")):
        return False
    return (
        any(marker in before for marker in ("固定", "默认", "标准", "周期长度", "窗口长度"))
        and any(marker in clause for marker in ("不是固定", "并非固定", "并不是固定", "会根据", "取决于", "缩短", "延长"))
    )


def _is_policy_threshold_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 12) : start]
    if any(keyword in before for keyword in ("剩余", "还剩", "只剩", "显示", "跳到", "跳至")):
        return False
    return (
        (
            any(marker in clause for marker in ("从未", "不会", "通常", "正常状态", "公开的", "校准频率"))
            and any(marker in clause for marker in ("少于", "多于", "超过", "小于", "大于", "低于", "高于"))
            and any(marker in clause for marker in ("启动", "触发", "校准程序", "频率", "规则"))
        )
        or (
            any(marker in clause for marker in ("低于", "高于", "少于", "超过"))
            and any(marker in clause for marker in ("规则", "意味着", "阶段", "阈值"))
            and any(marker in clause for marker in ("重置周期", "系统", "校准", "主控", "扫描"))
        )
        or (
            any(marker in clause for marker in ("低于", "高于", "少于", "超过"))
            and any(marker in clause for marker in ("启动", "触发", "进入"))
            and any(marker in clause for marker in ("重置周期", "系统", "校准", "主控", "扫描"))
        )
        or (
            any(marker in clause for marker in ("不会", "从来不会", "通常", "正常"))
            and any(marker in clause for marker in ("短于", "低于", "少于", "小于"))
            and any(marker in clause for marker in ("维护周期", "维护窗口", "普通维护"))
        )
        or (
            any(marker in clause for marker in ("应急加速协议", "标准", "逐级缩减", "最终压缩"))
            and any(marker in clause for marker in ("压缩", "缩减", "以内", "协议"))
            and not any(marker in before for marker in ("现在的", "当前", "此刻", "实际"))
        )
        or (
            any(marker in clause for marker in ("必须按", "需要按", "只能按", "应按"))
            and any(marker in clause for marker in ("以下处理", "以下", "以内处理"))
        )
    )


def _is_observation_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 12) : start]
    if any(keyword in before for keyword in ("剩余", "还剩", "只剩", "倒计时", "距离")):
        return False
    return any(marker in clause for marker in ("观察", "留观", "监测")) and any(
        marker in clause for marker in ("生命体征", "伤口", "医疗", "医用", "建议继续", "继续观察")
    )


def _is_wait_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 16) : start]
    if any(keyword in before for keyword in ("剩余", "还剩", "只剩", "倒计时", "距离")):
        return False
    return any(marker in clause for marker in ("要等", "需要等", "至少要等", "等待", "再等")) and any(
        marker in clause for marker in ("下一次", "下次", "打开", "开启", "恢复")
    )


def _is_frequency_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    if "间隔" in clause and any(marker in clause for marker in ("巡逻", "换岗", "轮班", "刷新", "切换")):
        return True
    return "频率" in clause and ("每" in clause or "一次" in clause)


def _is_hypothetical_letter_day_reference(text: str, start: int, end: int) -> bool:
    raw = text[start:end]
    if raw not in {"一天", "一日"}:
        return False
    clause = _current_clause(text, start, end)
    clause_start = _clause_start_index(text, start)
    before_in_clause = clause[: max(0, start - clause_start)].rstrip()
    after_in_clause = clause[max(0, end - clause_start) : max(0, end - clause_start) + 32]
    if not before_in_clause.endswith(("如果有", "若有", "倘若有", "假如有")):
        return False
    return any(
        marker in after_in_clause
        for marker in (
            "看到这段话",
            "读到这段话",
            "看到这封信",
            "读到这封信",
            "收到这封信",
            "看到这些字",
            "读到这些字",
        )
    )


def _is_delta_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 12) : start]
    after = text[end : min(len(text), end + 12)]
    if "从" in before and any(marker in after for marker in ("降到", "降至", "直接降", "缩短到", "压到")):
        return True
    if any(keyword in before for keyword in ("剩余", "还剩", "只剩", "倒计时", "距离")):
        return False
    return any(marker in clause for marker in ("少了", "减少了", "缩短了", "又少了", "又减少"))


def _is_countdown_cost_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 18) : start].rstrip()
    local = text[max(0, start - 48) : min(len(text), end + 48)]
    if not any(
        marker in local
        for marker in (
            "倒计时",
            "剩余时间",
            "重置窗口",
            "记忆重置",
            "重置周期",
            "记忆片段",
            "记忆样本",
            "失去",
            "剥离",
        )
    ):
        return False
    if not any(
        marker in clause
        for marker in (
            "扣除",
            "扣掉",
            "扣减",
            "消耗",
            "耗费",
            "代价",
            "付出",
            "失去",
            "损失",
            "牺牲",
        )
    ):
        return False
    return not before.endswith(
        (
            "显示",
            "显示：",
            "显示:",
            "剩余：",
            "剩余:",
            "还剩",
            "只剩",
            "跳到",
            "跳至",
            "降到",
            "降至",
        )
    )


def _is_internal_audit_delay_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 16) : start]
    if any(keyword in before for keyword in ("剩余", "还剩", "只剩", "倒计时", "计时器", "窗口")):
        return False
    return (
        "审计" in clause
        and any(marker in clause for marker in ("触发", "启动", "开始", "激活"))
        and any(marker in clause for marker in ("后", "将在", "会在"))
        and not any(marker in clause for marker in ("记忆重置", "重置周期", "全域记忆", "熔铸倒计时"))
    )


def _is_local_memory_erosion_threshold_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 16) : start]
    if any(keyword in before for keyword in ("剩余", "还剩", "只剩", "倒计时", "计时器", "显示", "跳到", "跳至")):
        return False
    return (
        any(marker in clause for marker in ("超过", "多于", "停留", "每在", "每停留"))
        and any(marker in clause for marker in ("记忆剥离", "短期记忆", "自己是谁", "第三层", "局部记忆"))
        and not any(marker in clause for marker in ("记忆重置", "重置周期", "全域记忆", "熔铸倒计时"))
    )


def _is_detention_review_window_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 16) : start]
    if any(keyword in before for keyword in ("倒计时", "记忆重置", "重置周期", "距离")):
        return False
    return (
        any(marker in clause for marker in ("审查窗口", "审查时限", "审查时间"))
        and any(marker in clause for marker in ("预计", "关押", "捕获", "押送", "监室", "牢房", "隔离"))
        and not any(marker in clause for marker in ("记忆重置", "重置周期", "全域记忆", "熔铸倒计时"))
    )


def _is_travel_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 16) : start]
    if any(keyword in before for keyword in ("倒计时", "剩余", "还剩", "只剩", "重置")):
        return False
    return (
        "距离" in clause
        and any(marker in clause for marker in ("需要", "至少需要", "耗时", "路程", "赶到", "抵达", "到达"))
        and not any(marker in clause for marker in ("记忆重置", "重置周期", "倒计时", "归零"))
    )


def _is_elapsed_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 12) : start]
    if before.rstrip().endswith(("流逝了", "又流逝了", "过去了", "又过去了", "耗费了", "用了")):
        return True
    if any(keyword in before for keyword in ("剩余", "还剩", "只剩", "倒计时", "距离")):
        return False
    return any(
        marker in clause
        for marker in (
            "已经过去",
            "过去了",
            "已经持续",
            "已持续",
            "持续了",
            "持续",
            "耗时",
            "路上用了",
            "昏迷期间",
            "昏迷了",
            "昏过去",
            "失去意识",
        )
    )


def _is_elapsed_to_baseline_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    clause_start = _clause_start_index(text, start)
    relative_end = max(0, end - clause_start)
    after_mention = clause[relative_end:]
    return (
        any(marker in after_mention for marker in ("流逝到", "流逝至", "降到", "降至", "缩到", "缩至", "变成"))
        and any(marker in clause for marker in ("倒计时", "重置周期", "剩余时间"))
        and not str(clause[: max(0, start - clause_start)]).rstrip().endswith(("剩余", "还剩", "只剩", "显示", "跳到", "跳至"))
    )


def _is_hypothetical_completion_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 12) : start]
    after = text[end : min(len(text), end + 8)]
    if any(keyword in before for keyword in ("剩余", "还剩", "只剩", "倒计时", "距离")):
        return False
    return (
        any(marker in clause for marker in ("如果", "可能", "预计", "预估"))
        and after.startswith("内")
        and any(marker in clause for marker in ("完成", "结束", "归零"))
    )


def _is_hypothetical_protocol_compression_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    local = clause
    return (
        any(marker in local for marker in ("如果", "异常", "否则", "强制启动"))
        and "协议" in local
        and any(marker in local for marker in ("压缩到", "压缩至", "压到", "缩短到", "缩短至"))
        and any(marker in local for marker in ("所有剩余时间", "剩余时间", "整个记忆重置周期"))
        and not any(marker in clause for marker in ("当前", "此刻", "现在"))
    )


def _is_protocol_result_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 12) : start]
    local = text[max(0, start - 80) : min(len(text), end + 80)]
    if any(keyword in before for keyword in ("当前", "剩余", "还剩", "只剩", "倒计时", "距离")):
        return False
    return (
        "协议" in local
        and any(marker in clause for marker in ("激活后", "执行后", "将", "会"))
        and any(marker in clause for marker in ("缩短至", "缩短到", "重启", "恢复基准"))
    )


def _is_interception_eta_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 16) : start]
    if any(keyword in before for keyword in ("倒计时", "记忆重置", "重置周期", "计时器")):
        return False
    return (
        any(marker in clause for marker in ("拦截时间", "拦截抵达", "抵达时间", "赶到时间", "巡检员抵达时间"))
        and any(marker in clause for marker in ("预计", "剩余", "将在", "还需"))
        and not any(marker in clause for marker in ("记忆重置", "重置周期", "全域记忆", "熔铸倒计时"))
    )


def _is_approximate_window_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 24) : start]
    if any(keyword in before for keyword in ("显示", "跳到", "跳至", "还剩", "只剩")) and "窗口" not in before:
        return False
    return (
        any(marker in before for marker in ("约", "大约", "大概", "左右"))
        and any(marker in clause for marker in ("有效期", "权限", "密钥", "救援", "恢复路径"))
        and not any(marker in clause for marker in ("手腕", "腕表", "计时器显示", "倒计时显示"))
    )


def _is_scheduled_remaining_threshold_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 24) : start]
    after = text[end : min(len(text), end + 12)]
    if not before.rstrip().endswith(("倒计时剩余", "重置周期剩余", "记忆重置剩余")):
        return False
    return after.lstrip().startswith("时") and any(marker in clause for marker in ("将在", "将", "会")) and any(
        marker in clause for marker in ("留下", "接头", "出现", "发送", "启动", "触发", "转移", "关闭")
    )


def _is_activation_window_threshold_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 18) : start]
    after = text[end : min(len(text), end + 12)]
    if any(keyword in before for keyword in ("剩余", "还剩", "只剩", "显示", "跳到", "跳至")):
        return False
    return (
        "最后" in before
        and any(marker in (after + clause) for marker in ("窗口", "内激活", "内启动", "内完成", "内触发"))
        and (
            any(marker in clause for marker in ("激活", "启动", "触发", "锚点", "验证", "校准"))
            or ("窗口" in clause and any(marker in clause for marker in ("也就是", "某个时刻")))
        )
    )


def _is_future_offset_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 16) : start]
    after = text[end : min(len(text), end + 16)]
    if any(keyword in before for keyword in ("剩余", "还剩", "只剩", "显示", "跳到", "跳至")):
        return False
    return after.lstrip().startswith("后") and any(
        marker in clause for marker in ("也就是", "某个时刻", "执行前", "激活前", "触发前")
    )


def _is_action_deadline_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 18) : start]
    after = text[end : min(len(text), end + 8)]
    if any(keyword in before for keyword in ("剩余", "还剩", "只剩", "显示", "跳到", "跳至")):
        return False
    return (
        after.lstrip().startswith("内")
        and any(marker in clause for marker in ("必须", "需要", "要在", "只能在", "来得及"))
        and any(
            marker in clause
            for marker in (
                "离开",
                "赶到",
                "抵达",
                "到达",
                "完成",
                "进入",
                "撤离",
                "汇合",
                "验证",
                "救出",
                "拿到",
                "取得",
            )
        )
    )


def _is_decision_deadline_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    clause_start = _clause_start_index(text, start)
    before = clause[: max(0, start - clause_start)]
    after = text[end : min(len(text), end + 12)]
    if any(keyword in before for keyword in ("剩余", "还剩", "只剩", "显示", "跳到", "跳至")):
        return False
    return (
        any(marker in before for marker in ("你有", "给你", "给了你", "还有"))
        and any(marker in after + clause for marker in ("考虑", "答复", "回答", "答案", "选择"))
    )


def _is_access_token_validity_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    if any(marker in clause for marker in ("记忆重置", "重置周期", "终端审计", "审计窗口")):
        return False
    return any(marker in clause for marker in ("临时访问令牌", "访问令牌", "令牌有效", "权限有效", "有效时间"))


def _is_local_operation_window_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    if any(marker in clause for marker in ("记忆重置", "记忆熔铸", "全域记忆", "熔铸倒计时")):
        return False
    return any(
        marker in clause
        for marker in (
            "操作窗口",
            "局部倒计时",
            "后门关闭",
            "后门已启用",
            "数据板上的倒计时",
            "救援窗口",
            "隔离舱",
            "刷新间隙",
            "远程开锁",
            "救援条件",
        )
    )


def _is_tracker_unlock_window_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    if any(marker in clause for marker in ("记忆重置", "记忆熔铸", "全域记忆", "熔铸倒计时")):
        return False
    nearby = text[max(0, start - 96) : min(len(text), end + 96)]
    tracker_markers = (
        "解除操作",
        "解除程序",
        "解除协议",
        "解除点",
        "追踪器解除",
        "追踪器管理",
        "追踪器的屏蔽窗口",
        "追踪器屏蔽窗口",
        "信号屏蔽",
        "屏蔽窗口",
        "本地网络只能维持这个窗口",
        "被植入者进入指定解除点",
        "目标追踪器",
    )
    return any(
        marker in nearby
        for marker in tracker_markers
    ) and any(marker in nearby for marker in ("窗口", "剩余", "不到", "还剩", "维持", "恢复定位", "操作时间"))


def _is_local_operation_eta_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 24) : start]
    nearby = text[max(0, start - 96) : min(len(text), end + 32)]
    if any(marker in clause for marker in ("记忆重置", "重置周期", "记忆熔铸", "熔铸协议", "熔铸倒计时", "全域记忆")):
        return False
    if before.rstrip().endswith(("倒计时", "倒计时还剩", "倒计时剩余", "剩余", "还剩", "只剩")):
        return False
    operation_markers = (
        "注入",
        "写入",
        "读取",
        "上传",
        "下载",
        "传输",
        "解码",
        "认证",
        "验证",
        "扫描",
        "同步",
        "加载",
        "执行",
        "生成",
        "破解",
        "门禁",
    )
    if any(marker in nearby for marker in ("预计时间", "预计用时", "预计剩余时间")) and any(
        marker in nearby for marker in operation_markers
    ):
        return True
    return (
        any(marker in clause for marker in ("预计剩余时间", "预计还需", "预计需要", "进度条", "进度", "剩余操作时间"))
        and any(marker in clause for marker in operation_markers)
    )


def _bridge_mentions_competing_countdown(text: str) -> bool:
    bridge = str(text or "")
    if any(
        keyword in bridge
        for keyword in (
            "终端审计",
            "审计窗口",
            "授权窗口",
            "档案清理",
            "公开数据",
            "公开窗口",
            "对外数据",
            "救援窗口",
            "隔离舱",
            "刷新间隙",
            "追踪器解除",
            "解除窗口",
            "屏蔽窗口",
            "信号屏蔽",
            "剩余操作时间",
        )
    ):
        return True
    return False


def _is_generic_remaining_time_restatement(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before_in_clause = clause[: max(0, start - _clause_start_index(text, start))]
    prefix = before_in_clause.rstrip()
    subject_match = re.search(
        r"(他|她|它|他们|她们|两人|二人|[\u4e00-\u9fff]{2,4})(?:还)?(?:有|剩|只剩)不(?:到|足)$",
        prefix,
    )
    if not subject_match:
        return False
    subject = subject_match.group(1)
    if subject not in {"他", "她", "它", "他们", "她们", "两人", "二人"} and any(
        marker in subject for marker in ("重置", "倒计时", "距离", "剩余")
    ):
        return False
    if any(marker in clause for marker in ("窗口", "审计", "档案清理", "档案抹除", "授权")):
        return False
    nearby_before = text[max(0, start - 140) : start]
    return any(
        marker in nearby_before
        for marker in ("记忆重置倒计时", "记忆重置剩余", "重置倒计时", "重置周期剩余")
    )


def _current_clause(text: str, start: int, end: int) -> str:
    left = _clause_start_index(text, start) - 1
    right_candidates = [idx for marker in ("。", "！", "？", "；", "\n") if (idx := text.find(marker, end)) >= 0]
    right = min(right_candidates) if right_candidates else len(text)
    return text[left + 1 : right]


def _clause_start_index(text: str, start: int) -> int:
    return max(text.rfind(marker, 0, start) for marker in ("。", "！", "？", "；", "\n")) + 1


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
    context = context.replace("倒计时结束前", "").replace("重置周期结束前", "")
    if any(
        keyword in context
        for keyword in (
            "归零",
            "倒计时解除",
            "倒计时结束",
            "倒计时归零",
            "危机解除",
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
            "不再有记忆重置",
            "无法再进行下一次记忆重置",
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


def _has_upper_bound_prefix(text: str, start: int) -> bool:
    before = text[max(0, start - 4) : start]
    return before.endswith(("不到", "不超过", "至多"))


def _is_rounding_equivalent(
    raw: str,
    previous_minutes: int,
    minutes: int,
    *,
    is_upper_bound: bool = False,
    context: str = "",
) -> bool:
    if is_upper_bound and minutes > previous_minutes:
        return minutes - previous_minutes <= max(10, int(previous_minutes * 0.2))
    if minutes - previous_minutes > 1:
        return _is_coarse_threshold_reference(raw, previous_minutes, minutes, context=context)
    if minutes > previous_minutes and any(marker in str(context or "") for marker in ("约", "大约", "左右", "不到", "不超过")):
        return True
    if 0 < minutes - previous_minutes <= 1 and any(marker in str(context or "") for marker in ("只有", "约", "大约", "左右")):
        return True
    mention = str(raw or "")
    return "小时" in mention and not any(unit in mention for unit in ("分钟", "分", ":"))


def _is_coarse_threshold_reference(raw: str, previous_minutes: int, minutes: int, *, context: str = "") -> bool:
    mention = str(raw or "")
    local = str(context or "")
    if minutes <= previous_minutes or minutes - previous_minutes > 5:
        return False
    if minutes % 10 != 0:
        return False
    if "分钟" not in mention and "分" not in mention:
        return False
    return any(
        marker in local
        for marker in (
            "不到",
            "不足",
            "以内",
            "最多",
            "缩短到",
            "压缩到",
            "缩减到",
            "逼近",
        )
    )


def _prefer_active_memory_reset_key(countdown_key: str, minutes: int, previous_by_key: dict[str, int]) -> str:
    if countdown_key != "main":
        return countdown_key
    memory_previous = previous_by_key.get("memory_reset")
    main_previous = previous_by_key.get("main")
    if memory_previous is None:
        return countdown_key
    if main_previous is None:
        return "memory_reset"
    if main_previous < memory_previous and minutes > main_previous:
        return "memory_reset"
    return countdown_key


def _prefer_short_clock_continuation_key(
    countdown_key: str, raw: str, previous_by_key: dict[str, int], context: str
) -> str:
    match = re.fullmatch(r"([0-9]{2,3}):([0-9]{2})", str(raw or ""))
    if not match:
        return countdown_key
    if _short_clock_has_explicit_key_label(countdown_key, context):
        return countdown_key
    first = int(match.group(1))
    second = int(match.group(2))
    if first >= 60 or second >= 60:
        return countdown_key
    compatible_keys: list[tuple[int, int, str]] = []
    for order, candidate_key in enumerate(
        ("terminal_audit_window", "memory_reset", "core_access_window", "archive_cleanup", "main")
    ):
        candidate_previous = previous_by_key.get(candidate_key)
        if candidate_previous is not None and candidate_previous <= 180 and first <= candidate_previous:
            compatible_keys.append((candidate_previous - first, order, candidate_key))
    if compatible_keys:
        compatible_keys.sort()
        return compatible_keys[0][2]
    return countdown_key


def _short_clock_has_explicit_key_label(countdown_key: str, context: str) -> bool:
    labels = {
        "memory_reset": (
            "记忆重置窗口",
            "记忆重置倒计时",
            "重置窗口",
            "重置倒计时",
            "记忆熔铸倒计时",
        ),
        "terminal_audit_window": ("终端审计窗口", "终端审计倒计时"),
        "archive_cleanup": (
            "档案抹除倒计时",
            "档案清理倒计时",
            "抹除倒计时",
            "授权窗口",
            "档案审计窗口",
        ),
        "core_access_window": ("核心层入口", "核心层授权窗口", "核心层的授权窗口", "入口关闭倒计时"),
    }
    return any(label in str(context or "") for label in labels.get(countdown_key, ()))


def _normalize_ambiguous_clock_minutes(
    *,
    raw: str,
    minutes: int,
    previous_minutes: int | None,
    countdown_key: str,
    context: str,
) -> int:
    two_part_match = re.fullmatch(r"([0-9]{2,3}):([0-9]{2})", str(raw or ""))
    if two_part_match:
        first = int(two_part_match.group(1))
        second = int(two_part_match.group(2))
        if (
            second < 60
            and first < 60
            and previous_minutes is not None
            and previous_minutes <= 180
            and first <= previous_minutes
        ):
            return first
        if (
            second < 60
            and first < 60
            and previous_minutes is None
            and _explicit_minute_second_countdown_context(countdown_key, context)
        ):
            return first
        if first >= 60 and previous_minutes is not None and previous_minutes <= 180 and first <= previous_minutes:
            return first
        return minutes
    match = re.fullmatch(r"([0-9]{2,3}):([0-9]{2}):([0-9]{2})", str(raw or ""))
    if not match:
        return minutes
    first = int(match.group(1))
    if first < 60:
        return minutes
    if previous_minutes is not None and previous_minutes <= 180 and first <= previous_minutes:
        return first
    if countdown_key == "memory_reset" and first <= 180 and "分钟" in str(context or "") and "小时" not in str(context or ""):
        return first
    return minutes


def _explicit_minute_second_countdown_context(countdown_key: str, context: str) -> bool:
    local = str(context or "")
    if countdown_key not in {"main", "memory_reset", "terminal_audit_window", "archive_cleanup", "core_access_window"}:
        return False
    if any(marker in local for marker in ("小时", "时后", "时内", "时启动", "时关闭")):
        return False
    return any(marker in local for marker in ("倒计时", "计时器", "剩余", "跳到", "跳至", "归零"))


def _non_monotonic_repair_hint(*, countdown_key: str, raw: str, previous_minutes: int, minutes: int) -> str:
    if countdown_key == "terminal_audit_window":
        return (
            f"不要把同一个终端审计窗口从 {previous_minutes} 分钟延长到 {raw}。"
            "删除或改写这个数字，使终端审计窗口继续小于等于前值；如果这是档案清理或核心授权窗口，"
            "必须明确命名为不同倒计时。"
        )
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
    after_context = _current_clause(text, start, end)
    immediate_after = re.split(r"[。！？!?；;\n]", str(text[end : min(len(text), end + 24)]), maxsplit=1)[0]
    near_after = str(text[end : min(len(text), end + 80)])
    next_clause_after = re.split(
        r"[。！？!?；;\n]",
        str(text[end : min(len(text), end + 80)]).lstrip("。！？!?；;\n "),
        maxsplit=1,
    )[0]
    public_markers = (
        "公开数据",
        "公开窗口",
        "对外数据",
        "对外公布",
        "公布数据",
        "公共时间",
        "心理缓冲",
        "普通市民",
    )
    if any(marker in after_context for marker in ("不是还有", "不是还剩")) and any(
        keyword in near_after for keyword in public_markers
    ):
        return "public_countdown"
    if any(keyword in immediate_after for keyword in public_markers):
        return "public_countdown"
    if any(keyword in near_after for keyword in public_markers) and any(
        marker in after_context for marker in ("上一次", "上一回", "上一轮", "上回", "之前", "此前", "预告")
    ):
        return "public_countdown"
    if any(keyword in after_context for keyword in public_markers):
        return "public_countdown"
    local_before = str(text[max(0, start - 48) : start])
    local_after = str(text[end : min(len(text), end + 56)])
    local_after_label = re.split(r"[。！？!?；;\n]", local_after, maxsplit=1)[0]
    local_memory_markers = (
        "记忆重置",
        "重置记忆",
        "重置一次记忆",
        "重置周期",
        "记忆熔铸",
        "熔铸倒计时",
        "记忆剥离",
        "剥离倒计时",
        "全域记忆归零",
        "记忆覆盖",
        "覆盖进程",
        "覆盖协议",
    )
    local_core_access_markers = ("核心层入口", "核心层的授权窗口", "核心层授权窗口", "入口关闭")
    local_terminal_audit_markers = ("终端审计窗口", "终端审计")
    local_archive_markers = (
        "核心层入口",
        "入口关闭",
        "授权窗口",
        "审计窗口",
        "档案清理",
        "档案抹除",
        "抹除令",
        "抹除倒计时",
        "档案记录",
    )
    local_after_key = _nearest_countdown_key(local_after_label)
    if (
        local_after_key in {"memory_reset", "core_access_window", "terminal_audit_window", "archive_cleanup"}
        and _after_label_binds_to_mention(local_after)
    ):
        return local_after_key
    current_clause = _current_clause(text, start, end)
    clause_start = _clause_start_index(text, start)
    current_clause_before = str(text[clause_start:start])
    explicit_current_clause_key = _explicit_countdown_label_key(current_clause_before)
    if explicit_current_clause_key:
        return explicit_current_clause_key
    if (
        any(marker in current_clause for marker in ("手腕", "左腕", "右腕", "腕表"))
        and any(marker in current_clause for marker in ("倒计时", "计时器", "数字", "显示"))
        and ":" in str(text[start:end])
        and any(marker in next_clause_after for marker in ("终端审计窗口", "终端审计倒计时"))
    ):
        return "terminal_audit_window"
    if (
        any(marker in current_clause for marker in ("手腕", "左腕", "右腕", "腕表"))
        and any(marker in current_clause for marker in ("倒计时", "计时器", "数字", "显示"))
    ):
        return "memory_reset"
    explicit_before_key = _explicit_countdown_label_key(local_before)
    if explicit_before_key:
        return explicit_before_key
    local_before_memory_index = max(local_before.rfind(keyword) for keyword in local_memory_markers)
    local_before_core_index = max(local_before.rfind(keyword) for keyword in local_core_access_markers)
    local_before_auth_index = local_before.rfind("授权窗口")
    if local_before_core_index >= 0 and local_before_core_index >= local_before_memory_index:
        return "core_access_window"
    if local_before_auth_index >= 0 and local_before_auth_index >= local_before_memory_index:
        return "archive_cleanup"
    local_key_context = local_before + after_context
    if "倒计时" in local_key_context and not any(
        marker in local_key_context
        for marker in (
            "终端审计",
            "授权窗口",
            "授权码",
            "档案清理",
            "档案抹除",
            "抹除令",
            "抹除倒计时",
            "记录群组",
            "审计日志",
            "限制级信息",
            "核心层入口",
            "公开数据",
            "公开窗口",
            "对外数据",
            "记忆重置",
            "重置记忆",
            "重置一次记忆",
            "重置周期",
            "记忆熔铸",
            "熔铸倒计时",
            "记忆剥离",
            "剥离倒计时",
            "全域记忆归零",
            "记忆覆盖",
            "覆盖进程",
            "覆盖协议",
            "记忆清除",
            "系统级威胁",
            "威胁评级",
            "追踪协议",
        )
    ):
        return "main"
    local_before_key = _nearest_countdown_key(local_before)
    if local_before_key in {"memory_reset", "core_access_window", "terminal_audit_window", "archive_cleanup"}:
        return local_before_key
    if local_after_key in {"memory_reset", "core_access_window", "terminal_audit_window", "archive_cleanup"}:
        return local_after_key
    if any(keyword in near_after for keyword in local_core_access_markers) and not any(
        keyword in (local_before + after_context) for keyword in local_memory_markers
    ):
        return "core_access_window"
    if any(keyword in near_after for keyword in local_archive_markers) and not any(
        keyword in (local_before + after_context) for keyword in local_memory_markers
    ):
        return "archive_cleanup"
    if _looks_like_forced_memory_calibration_context(before_context + after_context + immediate_after):
        return "memory_reset"
    key = _nearest_countdown_key(before_context)
    if key:
        return key
    key = _nearest_countdown_key(after_context)
    if key:
        return key
    if any(keyword in after_context for keyword in ("档案清理", "导出", "授权码", "授权窗口", "访问", "记录群组")):
        return "archive_cleanup"
    if any(keyword in after_context for keyword in ("记忆重置", "重置周期", "历史记录")):
        return "memory_reset"
    return "main"


def _looks_like_forced_memory_calibration_context(context: str) -> bool:
    local = str(context or "")
    return any(
        marker in local
        for marker in (
            "强制记忆校准",
            "强制校准",
            "校准倒计时",
            "记忆校准周期",
            "记忆重置加速程序",
            "加速程序授权",
            "全域记忆",
            "所有区域",
            "记忆覆盖",
        )
    ) and any(marker in local for marker in ("系统", "记忆", "重置", "覆盖", "校准"))


def _explicit_countdown_label_key(context: str) -> str:
    labels = {
        "memory_reset": (
            "记忆重置",
            "记忆重置窗口",
            "记忆重置倒计时",
            "记忆重置周期",
            "记忆熔铸倒计时",
            "记忆剥离倒计时",
            "记忆覆盖",
            "重置一次记忆",
            "每十年重置",
        ),
        "terminal_audit_window": ("终端审计窗口", "终端审计倒计时"),
        "archive_cleanup": (
            "档案清理",
            "档案清理倒计时",
            "档案清理窗口",
            "档案清理窗口倒计时",
            "档案抹除",
            "档案抹除倒计时",
            "抹除倒计时",
            "授权窗口",
        ),
        "core_access_window": ("核心层入口", "核心层授权窗口", "核心层的授权窗口", "入口关闭倒计时"),
    }
    nearest_key = ""
    nearest_rank = (-1, -1)
    local = str(context or "")
    for key, key_labels in labels.items():
        for label in key_labels:
            index = local.rfind(label)
            rank = (index + len(label), len(label))
            if index >= 0 and rank > nearest_rank:
                nearest_key = key
                nearest_rank = rank
    return nearest_key


def _after_label_binds_to_mention(local_after: str) -> bool:
    return str(local_after or "").lstrip().startswith(("——", "—", "-", "：", ":"))


def _nearest_countdown_key(context: str) -> str:
    keyword_keys = {
        "threat_response": (
            "记忆清除",
            "系统级威胁",
            "威胁评级",
            "追踪协议",
            "锁定你的位置",
            "反向锁定",
        ),
        "memory_reset": (
            "记忆重置",
            "记忆校准",
            "记忆熔铸",
            "记忆剥离",
            "记忆覆盖",
            "覆盖进程",
            "覆盖协议",
            "熔铸倒计时",
            "剥离倒计时",
            "熔铸窗口",
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
        "terminal_audit_window": (
            "终端审计窗口",
            "终端审计",
        ),
        "archive_cleanup": (
            "档案清理",
            "档案抹除",
            "抹除令",
            "抹除倒计时",
            "档案记录",
            "导出",
            "访问",
            "记录群组",
            "授权码",
            "审计窗口",
            "身份核验",
            "审计日志",
            "查询",
            "限制级信息",
        ),
        "core_access_window": (
            "核心层入口",
            "核心层授权窗口",
            "核心层的授权窗口",
            "入口关闭",
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
            if keyword == "审计窗口" and index >= 0 and context[max(0, index - 2) : index] == "终端":
                continue
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
