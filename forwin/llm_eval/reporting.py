from __future__ import annotations

from collections import Counter, defaultdict
from statistics import median
from typing import Any

from .schemas import EvalAttemptResult


def _rate(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def _percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percentile))))
    return int(ordered[index])


def _grade(metrics: dict[str, Any]) -> str:
    if metrics["http_400_rate"] >= 0.5 or metrics["transport_success_rate"] < 0.5:
        return "fail"
    if metrics["clean_success_rate"] >= 0.9 and metrics["format_success_rate"] >= 0.95:
        return "pass"
    if metrics["format_success_rate"] < 0.5:
        return "fail"
    return "warn"


def summarize_attempts(attempts: list[EvalAttemptResult]) -> dict[str, Any]:
    grouped: dict[str, list[EvalAttemptResult]] = defaultdict(list)
    for attempt in attempts:
        grouped[attempt.profile_id].append(attempt)

    profiles: dict[str, Any] = {}
    for profile_id, rows in sorted(grouped.items()):
        total = len(rows)
        durations = [int(row.duration_ms or 0) for row in rows]
        transport_ok = [
            row for row in rows
            if not row.error_category and int(row.http_status or 0) < 400
        ]
        format_ok = [row for row in rows if row.parse_ok and row.schema_ok]
        clean_ok = [
            row for row in rows
            if row.clean_success or (
                row in transport_ok
                and row in format_ok
                and int(row.retry_count or 0) == 0
            )
        ]
        hash_counts = Counter(row.output_hash for row in rows if row.output_hash)
        duplicate_outputs = sum(count for count in hash_counts.values() if count > 1)
        metrics = {
            "total_attempts": total,
            "clean_success_rate": _rate(len(clean_ok), total),
            "transport_success_rate": _rate(len(transport_ok), total),
            "format_success_rate": _rate(len(format_ok), total),
            "http_529_rate": _rate(sum(1 for row in rows if int(row.http_status or 0) == 529), total),
            "http_429_rate": _rate(sum(1 for row in rows if int(row.http_status or 0) == 429), total),
            "http_400_rate": _rate(sum(1 for row in rows if int(row.http_status or 0) == 400), total),
            "timeout_rate": _rate(sum(1 for row in rows if row.error_category == "timeout"), total),
            "output_empty_rate": _rate(sum(1 for row in rows if int(row.output_chars or 0) <= 0), total),
            "duplicate_output_rate": _rate(duplicate_outputs, total),
            "retry_count": sum(int(row.retry_count or 0) for row in rows),
            "p50_duration_ms": int(median(durations)) if durations else 0,
            "p95_duration_ms": _percentile(durations, 0.95),
        }
        metrics["grade"] = _grade(metrics)
        profiles[profile_id] = metrics

    return {
        "total_attempts": len(attempts),
        "profiles": profiles,
    }


def render_markdown_summary(summary: dict[str, Any]) -> str:
    lines = [
        "# ForWin LLM Reliability Summary",
        "",
        f"Total attempts: {summary.get('total_attempts', 0)}",
        "",
        "| Profile | Grade | Clean | Transport | Format | 529 | 429 | 400 | Timeout | p95 ms |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for profile_id, metrics in sorted((summary.get("profiles") or {}).items()):
        lines.append(
            "| {profile} | {grade} | {clean:.2%} | {transport:.2%} | {format:.2%} | "
            "{r529:.2%} | {r429:.2%} | {r400:.2%} | {timeout:.2%} | {p95} |".format(
                profile=profile_id,
                grade=metrics.get("grade", ""),
                clean=float(metrics.get("clean_success_rate", 0.0)),
                transport=float(metrics.get("transport_success_rate", 0.0)),
                format=float(metrics.get("format_success_rate", 0.0)),
                r529=float(metrics.get("http_529_rate", 0.0)),
                r429=float(metrics.get("http_429_rate", 0.0)),
                r400=float(metrics.get("http_400_rate", 0.0)),
                timeout=float(metrics.get("timeout_rate", 0.0)),
                p95=int(metrics.get("p95_duration_ms", 0)),
            )
        )
    lines.append("")
    return "\n".join(lines)
