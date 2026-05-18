from __future__ import annotations

import re
from typing import Any

from ..signals import CanonQualitySignal, make_signal_id
from .filters import _current_clause, _is_policy_threshold_duration_reference
from .parsing import _COUNTDOWN_NUMBER, parse_countdown_minutes


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


def _looks_like_retrospective_day_reference(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - 6) : start]
    after = text[end : min(len(text), end + 4)]
    return (
        before.endswith(("这", "那", "过去", "此前", "前面", "最近", "最后", "重置前最后"))
        or after.startswith(("里", "中", "以来", "发生", "发现", "内发生"))
    )


__all__ = [
    '_analyze_stale_retrospective_references',
    '_looks_like_stale_retrospective_clause',
    '_looks_like_public_decoy_clause',
    '_looks_like_retrospective_day_reference',
]
