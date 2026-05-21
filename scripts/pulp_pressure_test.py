#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Iterable

from forwin.config import Config
from forwin.governance import DecisionEventType
from forwin.models.base import get_engine, get_session_factory
from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.models.genesis import PromptTrace
from forwin.models.governance import DecisionEvent
from forwin.models.observability import PerformanceSpan
from forwin.models.project import ChapterPlan
from forwin.models.task import GenerationTask


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
    visible_payoff_present: bool | None = None
    pulp_missing_fields: list[str] | None = None
    structured_extraction_status: str | None = None
    reward_beats_in_plan: int | None = None
    reward_gap_since_last: int | None = None
    selected_trope_ids: list[str] | None = None
    selected_trope_categories: list[str] | None = None
    ending_hook_detected: bool | None = None
    chapter_length: int | None = None
    bookstate_compile_succeeded: bool | None = None
    rewrite_count: int | None = None
    verdict: str | None = None


def reward_gap_since_last(rows: list[ChapterMetric]) -> int | None:
    if not rows:
        return None
    current = rows[-1]
    if current.reward_beats_in_plan is None:
        return None
    if current.reward_beats_in_plan > 0:
        return 0
    for index in range(len(rows) - 2, -1, -1):
        reward_count = rows[index].reward_beats_in_plan
        if reward_count is None:
            return None
        if reward_count > 0:
            return len(rows) - 1 - index
    return None


def collect_rows(session, project_id: str, chapters: int) -> list[ChapterMetric]:  # noqa: ANN001
    plans = {
        int(plan.chapter_number or 0): plan
        for plan in session.query(ChapterPlan)
        .filter(ChapterPlan.project_id == project_id)
        .order_by(ChapterPlan.chapter_number.asc())
        .all()
    }
    drafts = _latest_drafts_by_chapter(session, project_id)
    task_statuses = _task_status_by_chapter(session, project_id)
    decision_events = _decision_events_by_chapter(session, project_id)
    spans = _spans_by_chapter(session, project_id)
    prompt_trace_count = (
        session.query(PromptTrace)
        .filter(PromptTrace.project_id == project_id)
        .count()
    )

    rows: list[ChapterMetric] = []
    for chapter_number in range(1, chapters + 1):
        plan = plans.get(chapter_number)
        draft = drafts.get(chapter_number)
        events = decision_events.get(chapter_number, [])
        row_spans = spans.get(chapter_number, [])
        metric = ChapterMetric(
            chapter_number=chapter_number,
            verdict=_chapter_verdict(plan, task_statuses.get(chapter_number)),
            chapter_length=int(getattr(draft, "char_count", 0) or 0) if draft else None,
            reward_beats_in_plan=_reward_beats_in_plan(plan),
            selected_trope_ids=_selected_trope_ids(plan),
            selected_trope_categories=_selected_trope_categories(plan),
            rewrite_count=int(getattr(plan, "repair_attempt_count", 0) or 0) if plan else None,
            hard_floor_passed=_hard_floor_passed(events, plan),
            hard_floor_fail_reasons=_hard_floor_fail_reasons(events),
            visible_payoff_present=_visible_payoff_present(events),
            pulp_missing_fields=_pulp_missing_fields(events),
            structured_extraction_status=_structured_extraction_status(events),
            ending_hook_detected=_ending_hook_detected(events),
            bookstate_compile_succeeded=_bookstate_compile_succeeded(events),
            wall_time_seconds=_wall_time_seconds(row_spans),
            llm_call_count=_llm_call_count(row_spans),
            output_token_count=_sum_metric(row_spans, "output_token_count", "completion_tokens"),
            prompt_char_count=_sum_metric(row_spans, "prompt_char_count", "prompt_chars"),
            context_pack_char_count=_sum_metric(
                row_spans,
                "context_pack_char_count",
                "context_chars",
            ),
        )
        rows.append(metric)
        metric.reward_gap_since_last = reward_gap_since_last(rows)

    if prompt_trace_count:
        for row in rows:
            if row.llm_call_count is None:
                row.llm_call_count = 0
    return rows


def compute_summary(
    rows: list[ChapterMetric],
    *,
    events: list[DecisionEvent] | None = None,
    tasks: list[GenerationTask] | None = None,
) -> dict[str, object]:
    events = events or []
    tasks = tasks or []
    missing_metric_sources = sorted(
        field.name
        for field in fields(ChapterMetric)
        if any(getattr(row, field.name) is None for row in rows)
    )
    reward_gaps = [row.reward_gap_since_last for row in rows if row.reward_gap_since_last is not None]
    wall_times = [row.wall_time_seconds for row in rows if row.wall_time_seconds is not None]
    return {
        "chapter_count": len(rows),
        "accepted_chapter_count": sum(1 for row in rows if row.verdict == "accepted"),
        "average_llm_call_count": _average(row.llm_call_count for row in rows),
        "average_wall_time_seconds": _average(row.wall_time_seconds for row in rows),
        "avg_llm_calls_per_chapter": _average(row.llm_call_count for row in rows),
        "p95_wall_time_seconds": _percentile(wall_times, 0.95),
        "prompt_char_count_slope": _slope(row.prompt_char_count for row in rows),
        "context_pack_char_count_slope": _slope(row.context_pack_char_count for row in rows),
        "reward_gap_p95": _percentile(reward_gaps, 0.95),
        "max_reward_gap": max(reward_gaps) if reward_gaps else None,
        "visible_payoff_missing_rate": _missing_rate(
            row.visible_payoff_present for row in rows
        ),
        "hard_floor_fail_rate": _false_rate(row.hard_floor_passed for row in rows),
        "canon_extraction_failure_rate": _extraction_failure_rate(rows),
        "repeat_trope_template_rate": _repeat_rate(row.selected_trope_ids for row in rows),
        "repeat_trope_category_rate": _repeat_rate(
            row.selected_trope_categories for row in rows
        ),
        "task_resume_success_rate": _task_resume_success_rate(tasks),
        "arc_macro_boundary_failure_rate": _future_plan_issue_rate(
            events,
            issue_type="arc_macro_progression_not_met",
        ),
        "progression_rule_violation_rate": _progression_rule_violation_rate(events),
        "macro_status_evidence_gap_rate": _future_plan_issue_rate(
            events,
            issue_type="macro_status_evidence_gap",
        ),
        "missing_metric_sources": missing_metric_sources,
    }


def write_reports(
    rows: list[ChapterMetric],
    output: Path,
    *,
    events: list[DecisionEvent] | None = None,
    tasks: list[GenerationTask] | None = None,
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    _write_metrics_csv(rows, output / "metrics.csv")
    (output / "summary.json").write_text(
        json.dumps(
            compute_summary(rows, events=events, tasks=tasks),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (output / "README.md").write_text(
        "# Pulp Pressure Test Report\n\n"
        "This report was generated from existing ForWin project/task/chapter telemetry.\n"
        "It does not start generation or mutate project state.\n",
        encoding="utf-8",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write pulp profile pressure-test reports.")
    parser.add_argument("--project-id", required=True, help="ForWin project id to pressure test.")
    parser.add_argument("--chapters", type=int, default=30, help="Number of chapter rows to collect.")
    parser.add_argument("--output", type=Path, required=True, help="Output report directory.")
    args = parser.parse_args(argv)
    if args.chapters < 1:
        parser.error("--chapters must be at least 1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    database_url = _database_url()
    engine = get_engine(database_url)
    Session = get_session_factory(engine)
    try:
        with Session() as session:
            rows = collect_rows(session, args.project_id, args.chapters)
            events = _project_decision_events(session, args.project_id)
            tasks = _project_generation_tasks(session, args.project_id)
        write_reports(rows, args.output, events=events, tasks=tasks)
    finally:
        engine.dispose()
    return 0


def _database_url() -> str:
    return os.environ.get("DATABASE_URL") or Config.from_env().database_url


def _latest_drafts_by_chapter(session, project_id: str) -> dict[int, ChapterDraft]:  # noqa: ANN001
    rows = (
        session.query(CandidateDraftRecord, ChapterDraft)
        .join(ChapterDraft, ChapterDraft.id == CandidateDraftRecord.candidate_draft_id)
        .filter(CandidateDraftRecord.project_id == project_id)
        .order_by(
            CandidateDraftRecord.chapter_number.asc(),
            CandidateDraftRecord.created_at.desc(),
            CandidateDraftRecord.version.desc(),
        )
        .all()
    )
    drafts: dict[int, ChapterDraft] = {}
    for record, draft in rows:
        chapter_number = int(record.chapter_number or 0)
        if chapter_number and chapter_number not in drafts:
            drafts[chapter_number] = draft
    return drafts


def _task_status_by_chapter(session, project_id: str) -> dict[int, str]:  # noqa: ANN001
    statuses: dict[int, str] = {}
    tasks = _project_generation_tasks(session, project_id)
    for task in tasks:
        for chapter_number in _json_list_ints(task.completed_chapters_json):
            statuses[chapter_number] = "accepted"
        for chapter_number in _json_list_ints(task.paused_chapters_json):
            statuses[chapter_number] = "paused"
        for chapter_number in _json_list_ints(task.failed_chapters_json):
            statuses[chapter_number] = "failed"
    return statuses


def _project_generation_tasks(session, project_id: str) -> list[GenerationTask]:  # noqa: ANN001
    return (
        session.query(GenerationTask)
        .filter(
            GenerationTask.project_id == project_id,
            GenerationTask.task_kind == "generation",
        )
        .order_by(GenerationTask.updated_at.asc(), GenerationTask.created_at.asc())
        .all()
    )


def _decision_events_by_chapter(session, project_id: str) -> dict[int, list[DecisionEvent]]:  # noqa: ANN001
    grouped: dict[int, list[DecisionEvent]] = {}
    events = _project_decision_events(session, project_id)
    for event in events:
        if int(event.chapter_number or 0) <= 0:
            continue
        grouped.setdefault(int(event.chapter_number or 0), []).append(event)
    return grouped


def _project_decision_events(session, project_id: str) -> list[DecisionEvent]:  # noqa: ANN001
    return (
        session.query(DecisionEvent)
        .filter(DecisionEvent.project_id == project_id)
        .order_by(DecisionEvent.chapter_number.asc(), DecisionEvent.created_at.asc())
        .all()
    )


def _spans_by_chapter(session, project_id: str) -> dict[int, list[PerformanceSpan]]:  # noqa: ANN001
    grouped: dict[int, list[PerformanceSpan]] = {}
    spans = (
        session.query(PerformanceSpan)
        .filter(PerformanceSpan.project_id == project_id, PerformanceSpan.chapter_number > 0)
        .order_by(PerformanceSpan.chapter_number.asc(), PerformanceSpan.created_at.asc())
        .all()
    )
    for span in spans:
        grouped.setdefault(int(span.chapter_number or 0), []).append(span)
    return grouped


def _chapter_verdict(plan: ChapterPlan | None, task_status: str | None) -> str:
    if plan is None:
        return "missing_plan"
    return str(getattr(plan, "status", "") or task_status or "")


def _reward_beats_in_plan(plan: ChapterPlan | None) -> int | None:
    if plan is None:
        return None
    beats = 0
    for item in _json_loads(getattr(plan, "task_contract_json", "[]"), []):
        if not isinstance(item, dict):
            continue
        text = json.dumps(item, ensure_ascii=False)
        if any(keyword in text for keyword in ("experience_delivery", "payoff", "reward", "爽点", "收益")):
            beats += 1
    experience_plan = _json_loads(getattr(plan, "experience_plan_json", "{}"), {})
    if isinstance(experience_plan, dict) and experience_plan:
        text = json.dumps(experience_plan, ensure_ascii=False)
        if any(keyword in text for keyword in ("payoff", "reward", "爽点", "visible_payoff")):
            beats = max(beats, 1)
    return beats


def _selected_trope_ids(plan: ChapterPlan | None) -> list[str] | None:
    if plan is None:
        return None
    experience_plan = _json_loads(getattr(plan, "experience_plan_json", "{}"), {})
    if not isinstance(experience_plan, dict):
        return []
    candidates: list[Any] = []
    for key in (
        "selected_template_ids",
        "selected_trope_ids",
        "template_ids",
        "trope_ids",
        "active_band_template_ids",
    ):
        value = experience_plan.get(key)
        if isinstance(value, list):
            candidates.extend(value)
        elif isinstance(value, str) and value.strip():
            candidates.append(value)
    return [str(item).strip() for item in candidates if str(item).strip()]


def _selected_trope_categories(plan: ChapterPlan | None) -> list[str] | None:
    if plan is None:
        return None
    experience_plan = _json_loads(getattr(plan, "experience_plan_json", "{}"), {})
    if not isinstance(experience_plan, dict):
        return []
    candidates: list[Any] = []
    for key in ("planned_reward_tags", "selected_trope_categories", "reward_tags"):
        value = experience_plan.get(key)
        if isinstance(value, list):
            candidates.extend(value)
        elif isinstance(value, str) and value.strip():
            candidates.append(value)
    return [str(item).strip() for item in candidates if str(item).strip()]


def _hard_floor_passed(events: list[DecisionEvent], plan: ChapterPlan | None) -> bool | None:
    for event in events:
        if event.event_type == DecisionEventType.HARD_GATE_HIT:
            payload = _json_loads(event.payload_json, {})
            if isinstance(payload, dict) and "passed" in payload:
                return bool(payload.get("passed"))
            return False
    if plan is None:
        return None
    if str(plan.status or "") in {"accepted", "needs_review"}:
        return True
    return None


def _hard_floor_fail_reasons(events: list[DecisionEvent]) -> list[str] | None:
    for event in events:
        if event.event_type != DecisionEventType.HARD_GATE_HIT:
            continue
        payload = _json_loads(event.payload_json, {})
        if isinstance(payload, dict) and isinstance(payload.get("fail_reasons"), list):
            return [str(item) for item in payload["fail_reasons"]]
        if event.reason:
            return [part.strip() for part in str(event.reason).split(";") if part.strip()]
        return []
    return None


def _visible_payoff_present(events: list[DecisionEvent]) -> bool | None:
    payload = _pulp_beat_payload(events)
    if not payload:
        return None
    value = payload.get("visible_payoff_present")
    return bool(value) if isinstance(value, bool) else None


def _pulp_missing_fields(events: list[DecisionEvent]) -> list[str] | None:
    payload = _pulp_beat_payload(events)
    if not payload:
        return None
    value = payload.get("missing_fields")
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _pulp_beat_payload(events: list[DecisionEvent]) -> dict[str, Any]:
    for event in events:
        if event.event_type != DecisionEventType.PULP_BEAT_EVALUATED:
            continue
        payload = _json_loads(event.payload_json, {})
        if isinstance(payload, dict) and isinstance(payload.get("pulp_beat"), dict):
            return payload["pulp_beat"]
    return {}


def _structured_extraction_status(events: list[DecisionEvent]) -> str | None:
    for event in events:
        payload = _json_loads(event.payload_json, {})
        if not isinstance(payload, dict):
            continue
        if (
            event.event_type == DecisionEventType.DEFERRED_MAINTENANCE_RECORDED
            and payload.get("task_type") == "structured_extraction"
        ):
            return str(payload.get("structured_extraction") or "deferred")
    return None


def _ending_hook_detected(events: list[DecisionEvent]) -> bool | None:
    for event in events:
        payload = _json_loads(event.payload_json, {})
        if isinstance(payload, dict):
            checks = payload.get("checks")
            if isinstance(checks, dict) and "ending_hook" in checks:
                return bool(checks.get("ending_hook"))
    return None


def _bookstate_compile_succeeded(events: list[DecisionEvent]) -> bool | None:
    seen_compile = False
    for event in events:
        if event.event_type == DecisionEventType.BOOK_STATE_COMPILE_SUCCEEDED:
            return True
        if event.event_type == DecisionEventType.BOOK_STATE_COMPILE_FAILED:
            return False
        if event.event_type == DecisionEventType.BOOK_STATE_COMPILE_STARTED:
            seen_compile = True
    return None if not seen_compile else False


def _wall_time_seconds(spans: list[PerformanceSpan]) -> float | None:
    if not spans:
        return None
    duration_ms = sum(max(0, int(span.duration_ms or 0)) for span in spans)
    return round(duration_ms / 1000, 3)


def _llm_call_count(spans: list[PerformanceSpan]) -> int | None:
    if not spans:
        return None
    return sum(
        1
        for span in spans
        if str(span.span_kind or "") == "llm" or str(span.span_name or "").startswith("llm.")
    )


def _sum_metric(spans: list[PerformanceSpan], *keys: str) -> int | None:
    total = 0
    found = False
    for span in spans:
        metrics = _json_loads(span.metrics_json, {})
        if not isinstance(metrics, dict):
            continue
        for key in keys:
            if key not in metrics:
                continue
            try:
                total += int(metrics[key] or 0)
                found = True
            except (TypeError, ValueError):
                continue
    return total if found else None


def _average(values: Iterable[float | int | None]) -> float | int | None:
    present_values = [value for value in values if value is not None]
    if not present_values:
        return None
    total = sum(present_values)
    average = total / len(present_values)
    if average.is_integer():
        return int(average)
    return round(average, 3)


def _percentile(values: Iterable[float | int | None], percentile: float) -> float | int | None:
    present_values = sorted(value for value in values if value is not None)
    if not present_values:
        return None
    index = min(
        len(present_values) - 1,
        max(0, int((len(present_values) - 1) * percentile + 0.999999)),
    )
    value = present_values[index]
    return int(value) if isinstance(value, float) and value.is_integer() else value


def _slope(values: Iterable[float | int | None]) -> float | int | None:
    present_values = [value for value in values if value is not None]
    if len(present_values) < 2:
        return None
    slope = (present_values[-1] - present_values[0]) / (len(present_values) - 1)
    if float(slope).is_integer():
        return int(slope)
    return round(float(slope), 3)


def _false_rate(values: Iterable[bool | None]) -> float | None:
    present_values = [value for value in values if value is not None]
    if not present_values:
        return None
    return round(sum(1 for value in present_values if value is False) / len(present_values), 3)


def _missing_rate(values: Iterable[bool | None]) -> float | None:
    return _false_rate(values)


def _extraction_failure_rate(rows: list[ChapterMetric]) -> float | None:
    statuses = [row.structured_extraction_status for row in rows if row.structured_extraction_status is not None]
    if not statuses:
        return None
    failures = {"degraded", "partial_degraded", "deferred"}
    return round(sum(1 for status in statuses if status in failures) / len(statuses), 3)


def _repeat_rate(values_by_row: Iterable[list[str] | None]) -> float | None:
    seen: set[str] = set()
    total = 0
    repeats = 0
    for values in values_by_row:
        for value in values or []:
            if not value:
                continue
            total += 1
            if value in seen:
                repeats += 1
            else:
                seen.add(value)
    if total == 0:
        return None
    return round(repeats / total, 3)


def _task_resume_success_rate(tasks: list[GenerationTask]) -> float | None:
    resume_tasks = [
        task
        for task in tasks
        if int(getattr(task, "resume_from_chapter", 0) or 0) > 0
        or int(getattr(task, "run_until_chapter", 0) or 0) > 0
    ]
    if not resume_tasks:
        return None
    successes = sum(1 for task in resume_tasks if str(task.status or "") == "completed")
    return round(successes / len(resume_tasks), 3)


def _future_plan_issue_rate(
    events: list[DecisionEvent],
    *,
    issue_type: str,
) -> float | None:
    audit_events = [
        event
        for event in events
        if str(event.event_type or "")
        in {DecisionEventType.FUTURE_PLAN_AUDIT_RUN, "future_plan_audit_completed"}
    ]
    if not audit_events:
        return None
    matches = sum(1 for event in audit_events if _event_has_issue_type(event, issue_type))
    return round(matches / len(audit_events), 3)


def _event_has_issue_type(event: DecisionEvent, issue_type: str) -> bool:
    payload = _json_loads(event.payload_json, {})
    if not isinstance(payload, dict):
        return False
    issues = payload.get("issues")
    if isinstance(issues, list):
        for issue in issues:
            if isinstance(issue, dict) and issue.get("issue_type") == issue_type:
                return True
    blocking_reasons = payload.get("blocking_reasons")
    if isinstance(blocking_reasons, list):
        return any(str(reason).startswith(issue_type) for reason in blocking_reasons)
    return False


def _progression_rule_violation_rate(events: list[DecisionEvent]) -> float | None:
    rule_events = [
        event
        for event in events
        if str(event.event_type or "") == "progression_rule_evaluated"
    ]
    if not rule_events:
        return None
    violations = 0
    for event in rule_events:
        payload = _json_loads(event.payload_json, {})
        if isinstance(payload, dict) and bool(payload.get("violated")):
            violations += 1
    return round(violations / len(rule_events), 3)


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


def _json_loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _json_list_ints(value: Any) -> list[int]:
    raw = _json_loads(value, [])
    if not isinstance(raw, list):
        return []
    normalized: list[int] = []
    for item in raw:
        try:
            normalized.append(int(item))
        except (TypeError, ValueError):
            continue
    return normalized


if __name__ == "__main__":
    raise SystemExit(main())
