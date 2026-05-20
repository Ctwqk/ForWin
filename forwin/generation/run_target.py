from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GenerationRunTarget:
    target_total_chapters: int
    run_until_chapter: int
    next_chapter: int
    max_chapters: int | None
    effective_max_chapters: int


def resolve_generation_run_target(
    project: Any,
    *,
    next_chapter: int,
    run_until_chapter: int | None = None,
    max_chapters: int | None = None,
) -> GenerationRunTarget:
    target_total = int(getattr(project, "target_total_chapters", 0) or 0)
    if target_total <= 0:
        raise ValueError("target_total_chapters must be positive")

    normalized_next = int(next_chapter or 0)
    if normalized_next <= 0:
        raise ValueError("next_chapter must be positive")

    normalized_until = int(run_until_chapter or target_total)
    if normalized_until < normalized_next:
        raise ValueError("run_until_chapter must be >= next_chapter")
    if normalized_until > target_total:
        raise ValueError("run_until_chapter must be <= target_total_chapters")

    normalized_max = int(max_chapters) if max_chapters is not None else None
    if normalized_max is not None and normalized_max < 1:
        raise ValueError("max_chapters must be positive when provided")

    remaining_to_until = normalized_until - normalized_next + 1
    effective_max = remaining_to_until
    if normalized_max is not None:
        effective_max = min(effective_max, normalized_max)
        normalized_until = normalized_next + effective_max - 1

    return GenerationRunTarget(
        target_total_chapters=target_total,
        run_until_chapter=normalized_until,
        next_chapter=normalized_next,
        max_chapters=normalized_max,
        effective_max_chapters=effective_max,
    )
