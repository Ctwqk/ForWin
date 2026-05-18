from __future__ import annotations

import re
from typing import Any

from .signals import ArtifactLedgerEntry, CanonQualitySignal, make_signal_id


def analyze_artifact_counts(
    *,
    project_id: str,
    chapter_number: int,
    draft_id: str = "",
    body: str,
    previous_ledgers: list[dict[str, Any] | ArtifactLedgerEntry] | None = None,
    target_total: int = 0,
) -> tuple[list[CanonQualitySignal], list[ArtifactLedgerEntry]]:
    text = str(body or "")
    signals: list[CanonQualitySignal] = []
    entries: list[ArtifactLedgerEntry] = []
    if int(target_total or 0) <= 0:
        return signals, entries
    mentioned_totals: list[tuple[int, int, int]] = []
    for match in re.finditer(r"([0-9一二两三四五六七八九十零]+)份(?:档案)?", text):
        number = parse_chinese_number(match.group(1))
        if number is not None:
            mentioned_totals.append((number, match.start(), match.end()))

    distinct_totals = {value for value, _start, _end in mentioned_totals}
    if len(distinct_totals) > 1 or (target_total and any(value > target_total for value in distinct_totals)):
        subject = "artifact:main"
        signals.append(
            CanonQualitySignal(
                signal_id=make_signal_id(project_id, chapter_number, "artifact_count_conflict", subject),
                project_id=project_id,
                chapter_number=chapter_number,
                signal_type="artifact_count_conflict",
                severity="error",
                target_scope="ledger",
                subject_key=subject,
                description=f"核心档案数量出现冲突：{sorted(distinct_totals)}。",
                evidence_refs=[f"body:{start}-{end}" for _value, start, end in mentioned_totals],
                payload={"draft_id": draft_id, "target_total": target_total},
            )
        )

    for match in re.finditer(r"第([0-9]+)(?:[-—~至到]([0-9]+))?份档案", text):
        start_index = int(match.group(1))
        end_index = int(match.group(2) or start_index)
        if end_index < start_index:
            start_index, end_index = end_index, start_index
        new_items = [str(index) for index in range(start_index, end_index + 1)]
        entries.append(
            ArtifactLedgerEntry(
                project_id=project_id,
                target_total=target_total,
                chapter_number=chapter_number,
                mentioned_index=start_index,
                collected_count_after=_previous_collected_count(previous_ledgers or []) + len(new_items),
                new_items=new_items,
                status="consistent",
                evidence_refs=[f"body:{match.start()}-{match.end()}"],
                payload={"draft_id": draft_id},
            )
        )
    if not entries and mentioned_totals:
        value, start, end = mentioned_totals[0]
        entries.append(
            ArtifactLedgerEntry(
                project_id=project_id,
                target_total=target_total,
                chapter_number=chapter_number,
                mentioned_index=value,
                collected_count_after=_previous_collected_count(previous_ledgers or []),
                status="conflict" if signals else "consistent",
                evidence_refs=[f"body:{start}-{end}"],
                payload={"draft_id": draft_id, "mentioned_totals": sorted(distinct_totals)},
            )
        )
    return signals, entries


def _previous_collected_count(entries: list[dict[str, Any] | ArtifactLedgerEntry]) -> int:
    if not entries:
        return 0
    values: list[int] = []
    for raw in entries:
        item = raw.model_dump(mode="json") if isinstance(raw, ArtifactLedgerEntry) else dict(raw)
        values.append(int(item.get("collected_count_after", 0) or 0))
    return max(values or [0])


def parse_chinese_number(raw: str) -> int | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if text in digits:
        return digits[text]
    if "十" in text:
        left, _, right = text.partition("十")
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    return None


__all__ = ["analyze_artifact_counts", "parse_chinese_number"]
