from __future__ import annotations

import csv
import json

from scripts.pulp_pressure_test import (
    ChapterMetric,
    compute_summary,
    reward_gap_since_last,
    write_reports,
)


def test_reward_gap_since_last_counts_chapters_since_prior_reward() -> None:
    rows = [
        ChapterMetric(chapter_number=1, reward_beats_in_plan=1),
        ChapterMetric(chapter_number=2, reward_beats_in_plan=0),
        ChapterMetric(chapter_number=3, reward_beats_in_plan=0),
        ChapterMetric(chapter_number=4, reward_beats_in_plan=2),
    ]

    gaps: list[int | None] = []
    for index in range(len(rows)):
        gaps.append(reward_gap_since_last(rows[: index + 1]))

    assert gaps == [0, 1, 2, 0]


def test_reward_gap_since_last_returns_none_when_current_reward_metric_is_missing() -> None:
    rows = [
        ChapterMetric(chapter_number=1, reward_beats_in_plan=1),
        ChapterMetric(chapter_number=2, reward_beats_in_plan=None),
    ]

    assert reward_gap_since_last(rows) is None


def test_reward_gap_since_last_returns_none_when_intermediate_reward_metric_is_missing() -> None:
    rows = [
        ChapterMetric(chapter_number=1, reward_beats_in_plan=1),
        ChapterMetric(chapter_number=2, reward_beats_in_plan=None),
        ChapterMetric(chapter_number=3, reward_beats_in_plan=0),
    ]

    assert reward_gap_since_last(rows) is None


def test_compute_summary_ignores_missing_values_but_not_zeroes() -> None:
    rows = [
        ChapterMetric(
            chapter_number=1,
            llm_call_count=2,
            prompt_char_count=1000,
            wall_time_seconds=10,
        ),
        ChapterMetric(
            chapter_number=2,
            llm_call_count=0,
            prompt_char_count=None,
            wall_time_seconds=20,
        ),
    ]

    summary = compute_summary(rows)

    assert summary["chapter_count"] == 2
    assert summary["average_llm_call_count"] == 1
    assert summary["average_wall_time_seconds"] == 15
    assert "prompt_char_count" in summary["missing_metric_sources"]


def test_write_reports_writes_expected_files_and_json_encodes_lists(tmp_path) -> None:
    output_dir = tmp_path / "pressure"
    rows = [
        ChapterMetric(
            chapter_number=1,
            hard_floor_fail_reasons=["too_short", "missing_hook"],
            selected_trope_ids=["mentor", "betrayal"],
        )
    ]

    write_reports(rows, output_dir)

    metrics_path = output_dir / "metrics.csv"
    assert metrics_path.exists()
    assert (output_dir / "summary.json").exists()
    assert (output_dir / "README.md").exists()

    with metrics_path.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))

    assert json.loads(row["hard_floor_fail_reasons"]) == ["too_short", "missing_hook"]
    assert json.loads(row["selected_trope_ids"]) == ["mentor", "betrayal"]
