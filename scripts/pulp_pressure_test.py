#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any


@dataclass
class ChapterMetric:
    chapter_number: int
    wall_time_seconds: float | None = None
    llm_call_count: int | None = None
    output_token_count: int | None = None
    prompt_char_count: int | None = None
    context_pack_char_count: int | None = None
    hard_floor_passed: bool | None = None
    hard_floor_fail_reasons: list[str] | None = None
    reward_beats_in_plan: int | None = None
    reward_gap_since_last: int | None = None
    selected_trope_ids: list[str] | None = None
    ending_hook_detected: bool | None = None
    chapter_length: int | None = None
    bookstate_compile_succeeded: bool | None = None
    rewrite_count: int | None = None
    verdict: str | None = None


def reward_gap_since_last(rows: list[ChapterMetric]) -> int | None:
    if not rows:
        return None
    current = rows[-1]
    if current.reward_beats_in_plan is not None and current.reward_beats_in_plan > 0:
        return 0
    for index in range(len(rows) - 2, -1, -1):
        reward_count = rows[index].reward_beats_in_plan
        if reward_count is not None and reward_count > 0:
            return len(rows) - 1 - index
    return None


def compute_summary(rows: list[ChapterMetric]) -> dict[str, object]:
    missing_metric_sources = sorted(
        field.name
        for field in fields(ChapterMetric)
        if any(getattr(row, field.name) is None for row in rows)
    )
    return {
        "chapter_count": len(rows),
        "average_llm_call_count": _average(row.llm_call_count for row in rows),
        "average_wall_time_seconds": _average(row.wall_time_seconds for row in rows),
        "missing_metric_sources": missing_metric_sources,
    }


def write_reports(rows: list[ChapterMetric], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    _write_metrics_csv(rows, output / "metrics.csv")
    (output / "summary.json").write_text(
        json.dumps(compute_summary(rows), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output / "README.md").write_text(
        "# Pulp Pressure Test Report\n\n"
        "This directory contains placeholder pressure-test metrics for the requested chapters.\n"
        "Future versions can replace placeholder rows with live ForWin generation metrics.\n",
        encoding="utf-8",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write pulp profile pressure-test reports.")
    parser.add_argument("--project-id", required=True, help="ForWin project id to pressure test.")
    parser.add_argument("--chapters", type=int, default=30, help="Number of placeholder chapter rows to emit.")
    parser.add_argument("--output", type=Path, required=True, help="Output report directory.")
    args = parser.parse_args(argv)
    if args.chapters < 1:
        parser.error("--chapters must be at least 1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _ = args.project_id
    rows = [ChapterMetric(chapter_number=chapter_number) for chapter_number in range(1, args.chapters + 1)]
    write_reports(rows, args.output)
    return 0


def _average(values: Any) -> float | int | None:
    present_values = [value for value in values if value is not None]
    if not present_values:
        return None
    total = sum(present_values)
    average = total / len(present_values)
    if average.is_integer():
        return int(average)
    return average


def _write_metrics_csv(rows: list[ChapterMetric], path: Path) -> None:
    fieldnames = [field.name for field in fields(ChapterMetric)]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(_csv_row(asdict(row)))


def _csv_row(row: dict[str, object]) -> dict[str, object]:
    return {
        key: json.dumps(value, ensure_ascii=False) if isinstance(value, list) else value
        for key, value in row.items()
    }


if __name__ == "__main__":
    raise SystemExit(main())
