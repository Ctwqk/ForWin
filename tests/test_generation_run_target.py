from __future__ import annotations

import pytest

from forwin.generation.run_target import (
    GenerationRunTarget,
    resolve_generation_run_target,
)


class ProjectStub:
    def __init__(self, target_total_chapters: int) -> None:
        self.target_total_chapters = target_total_chapters


def test_run_target_defaults_to_project_total() -> None:
    target = resolve_generation_run_target(
        ProjectStub(target_total_chapters=60),
        next_chapter=13,
    )

    assert target == GenerationRunTarget(
        target_total_chapters=60,
        run_until_chapter=60,
        next_chapter=13,
        max_chapters=None,
        effective_max_chapters=48,
    )


def test_run_target_uses_explicit_run_until() -> None:
    target = resolve_generation_run_target(
        ProjectStub(target_total_chapters=60),
        next_chapter=25,
        run_until_chapter=36,
    )

    assert target.run_until_chapter == 36
    assert target.effective_max_chapters == 12


def test_run_target_combines_run_until_and_max_chapters() -> None:
    target = resolve_generation_run_target(
        ProjectStub(target_total_chapters=60),
        next_chapter=25,
        run_until_chapter=60,
        max_chapters=5,
    )

    assert target.run_until_chapter == 29
    assert target.effective_max_chapters == 5


def test_run_target_rejects_past_run_until() -> None:
    with pytest.raises(ValueError, match="run_until_chapter must be >= next_chapter"):
        resolve_generation_run_target(
            ProjectStub(target_total_chapters=60),
            next_chapter=25,
            run_until_chapter=24,
        )


def test_run_target_rejects_explicit_zero_run_until() -> None:
    with pytest.raises(ValueError, match="run_until_chapter must be >= next_chapter"):
        resolve_generation_run_target(
            ProjectStub(target_total_chapters=60),
            next_chapter=25,
            run_until_chapter=0,
        )


def test_run_target_rejects_run_until_beyond_book_total() -> None:
    with pytest.raises(ValueError, match="run_until_chapter must be <= target_total_chapters"):
        resolve_generation_run_target(
            ProjectStub(target_total_chapters=60),
            next_chapter=25,
            run_until_chapter=61,
        )
