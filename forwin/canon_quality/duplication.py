from __future__ import annotations

import hashlib

from .signals import CanonQualitySignal, ChapterBodyMetrics, make_signal_id


def analyze_full_body_duplication(
    *,
    project_id: str,
    chapter_number: int,
    draft_id: str = "",
    body: str,
) -> tuple[list[CanonQualitySignal], ChapterBodyMetrics]:
    text = str(body or "")
    paragraphs = [part.strip() for part in text.splitlines() if part.strip()]
    positions = _paragraph_positions(text, paragraphs)
    seen: dict[str, tuple[int, str]] = {}
    duplicate_spans: list[dict[str, int]] = []
    signals: list[CanonQualitySignal] = []
    paragraph_hashes: list[str] = []
    for index, paragraph in enumerate(paragraphs):
        digest = hashlib.sha1(paragraph.encode("utf-8")).hexdigest()
        paragraph_hashes.append(digest)
        if digest not in seen:
            seen[digest] = (index, paragraph)
            continue
        first_index, first_paragraph = seen[digest]
        if len(paragraph) < 12:
            continue
        start = positions[index]
        first_start = positions[first_index]
        span = {
            "start": start,
            "end": start + len(paragraph),
            "matching_start": first_start,
            "matching_end": first_start + len(first_paragraph),
        }
        duplicate_spans.append(span)
        subject = f"body_duplicate:{digest[:10]}"
        signals.append(
            CanonQualitySignal(
                signal_id=make_signal_id(project_id, chapter_number, "body_duplicate_span", subject, index + 1),
                project_id=project_id,
                chapter_number=chapter_number,
                signal_type="body_duplicate_span",
                severity="error",
                target_scope="body",
                subject_key=subject,
                description="章节正文存在重复段落。",
                evidence_refs=[f"body:{span['start']}-{span['end']}", f"body:{span['matching_start']}-{span['matching_end']}"],
                span_start=span["start"],
                span_end=span["end"],
                payload={"draft_id": draft_id},
            )
        )
    metrics = ChapterBodyMetrics(
        project_id=project_id,
        chapter_number=chapter_number,
        draft_id=draft_id,
        paragraph_hashes=paragraph_hashes,
        duplicate_spans=duplicate_spans,
        metrics={"paragraph_count": len(paragraphs), "duplicate_span_count": len(duplicate_spans)},
    )
    return signals, metrics


def _paragraph_positions(text: str, paragraphs: list[str]) -> list[int]:
    positions: list[int] = []
    cursor = 0
    for paragraph in paragraphs:
        found = text.find(paragraph, cursor)
        if found < 0:
            found = text.find(paragraph)
        positions.append(max(0, found))
        cursor = max(0, found) + len(paragraph)
    return positions
