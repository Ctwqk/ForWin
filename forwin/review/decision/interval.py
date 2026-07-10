from __future__ import annotations


def chapters_since_last_full_review(
    *,
    accepted_chapter_count: int,
    review_interval_chapters: int,
) -> int:
    interval = max(0, int(review_interval_chapters or 0))
    accepted = max(0, int(accepted_chapter_count or 0))
    if not interval:
        return accepted
    remainder = accepted % interval
    return interval if remainder == 0 and accepted else remainder


def full_review_boundary(
    *,
    accepted_chapter_count: int,
    review_interval_chapters: int,
) -> bool:
    interval = max(0, int(review_interval_chapters or 0))
    accepted = max(0, int(accepted_chapter_count or 0))
    return bool(interval and accepted and accepted % interval == 0)
