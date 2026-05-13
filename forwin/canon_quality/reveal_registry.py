from __future__ import annotations

from typing import Any

from .signals import CanonQualitySignal, RevealRegistryEntry, make_signal_id

NEW_REVEAL_MARKERS = ("第一次发现", "首次发现", "新发现", "终于发现", "第一次")
ESCALATION_MARKERS = ("新增证据", "新证据", "代价", "反转", "指向", "因此决定", "付出")


def analyze_reveals(
    *,
    project_id: str,
    chapter_number: int,
    draft_id: str = "",
    reveal_claims: list[str],
    previous_entries: list[dict[str, Any] | RevealRegistryEntry] | None = None,
    body: str = "",
) -> tuple[list[CanonQualitySignal], list[RevealRegistryEntry]]:
    previous_by_key = {
        _normalize_key(str(item.get("reveal_key") if isinstance(item, dict) else item.reveal_key))
        if isinstance(item, dict)
        else _normalize_key(item.reveal_key): (item if isinstance(item, dict) else item.model_dump(mode="json"))
        for item in (previous_entries or [])
    }
    text = str(body or "")
    signals: list[CanonQualitySignal] = []
    entries: list[RevealRegistryEntry] = []
    for claim in reveal_claims:
        normalized = _normalize_key(claim)
        if not normalized:
            continue
        previous = previous_by_key.get(normalized)
        if previous is None:
            entries.append(
                RevealRegistryEntry(
                    project_id=project_id,
                    reveal_key=normalized,
                    claim_summary=claim,
                    first_revealed_chapter=chapter_number,
                    latest_chapter=chapter_number,
                    status="new",
                    evidence_refs=[f"reveal:{normalized}"],
                    payload={"draft_id": draft_id},
                )
            )
            continue
        repeat_count = int(previous.get("repeat_count", 0) or 0) + 1
        escalated = any(marker in text for marker in ESCALATION_MARKERS)
        status = "escalated" if escalated else "repeated"
        entry = RevealRegistryEntry(
            project_id=project_id,
            reveal_key=normalized,
            claim_summary=str(previous.get("claim_summary") or claim),
            first_revealed_chapter=int(previous.get("first_revealed_chapter", chapter_number) or chapter_number),
            latest_chapter=chapter_number,
            repeat_count=repeat_count,
            status=status,  # type: ignore[arg-type]
            evidence_refs=[f"reveal:{normalized}"],
            payload={"draft_id": draft_id},
        )
        entries.append(entry)
        if not escalated and any(marker in text for marker in NEW_REVEAL_MARKERS):
            subject = f"reveal:{normalized}"
            signals.append(
                CanonQualitySignal(
                    signal_id=make_signal_id(project_id, chapter_number, "repeated_reveal_as_new", subject),
                    project_id=project_id,
                    chapter_number=chapter_number,
                    signal_type="repeated_reveal_as_new",
                    severity="error",
                    target_scope="chapter",
                    subject_key=subject,
                    description=f"旧 reveal「{claim}」被再次包装成新发现。",
                    evidence_refs=entry.evidence_refs,
                    payload={"draft_id": draft_id, "first_revealed_chapter": entry.first_revealed_chapter},
                )
            )
    return signals, entries


def _normalize_key(value: str) -> str:
    return "".join(str(value or "").split())
