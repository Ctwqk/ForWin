from __future__ import annotations

from typing import Any

from ..signals import CanonQualitySignal, CountdownLedgerEntry, make_signal_id
from .filters import _is_resolution_context, _is_rounding_equivalent, _text_has_countdown_resolution
from .keys import (
    _non_monotonic_repair_hint,
    _normalize_ambiguous_clock_minutes,
    _prefer_active_memory_reset_key,
    _prefer_short_clock_continuation_key,
)
from .mentions import _is_reset_context_for_mention, _iter_countdown_mentions
from .parsing import _latest_entries_by_key, _latest_unresolved_entries_by_key
from .retrospective import _analyze_stale_retrospective_references


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
        is_reset = _is_reset_context_for_mention(text, start, end)
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


__all__ = [
    'analyze_countdowns',
]
