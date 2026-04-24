from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select

from forwin.models.phase import ProvisionalBandExecution
from forwin.models.world_v4 import ScenarioRehearsalRunRow


DisplayDatetime = Callable[[datetime | None], str]


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, str) and value.strip():
        try:
            timestamp = datetime.fromisoformat(value.strip())
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
    else:
        return datetime.min.replace(tzinfo=timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _task_has_stage(task: dict[str, Any], stage: str) -> bool:
    history = task.get("stage_history", [])
    if not isinstance(history, list):
        return False
    return any(str(entry.get("stage", "")).strip() == stage for entry in history if isinstance(entry, dict))


def _task_time_window(task: dict[str, Any]) -> tuple[datetime, datetime]:
    created_at = _coerce_datetime(task.get("created_at"))
    finished_at = _coerce_datetime(task.get("finished_at"))
    updated_at = _coerce_datetime(task.get("updated_at"))
    window_end = max(finished_at, updated_at)
    if window_end == datetime.min.replace(tzinfo=timezone.utc):
        window_end = datetime.now(timezone.utc)
    return created_at, window_end


def _task_first_chapter(raw_numbers: str) -> int:
    try:
        chapter_numbers = json.loads(raw_numbers or "[]")
    except json.JSONDecodeError:
        chapter_numbers = []
    if not isinstance(chapter_numbers, list):
        return 0
    for item in chapter_numbers:
        try:
            chapter = int(item)
        except (TypeError, ValueError):
            continue
        if chapter > 0:
            return chapter
    return 0


def _new_stage_history_entry(
    stage: str,
    *,
    display_datetime: DisplayDatetime,
    now: datetime | None = None,
    current_chapter: int = 0,
    message: str = "",
) -> dict[str, Any]:
    timestamp = now or datetime.now(timezone.utc)
    return {
        "stage": stage,
        "at": display_datetime(timestamp),
        "chapter": int(current_chapter or 0),
        "message": str(message or "").strip(),
    }


def _augment_with_scenario_rehearsal_history(
    session,
    task: dict[str, Any],
    *,
    display_datetime: DisplayDatetime,
) -> dict[str, Any]:
    project_id = str(task.get("project_id", "") or "").strip()
    if not project_id or _task_has_stage(task, "running_scenario_rehearsal"):
        return task

    created_at, window_end = _task_time_window(task)
    if created_at == datetime.min.replace(tzinfo=timezone.utc):
        return task

    rehearsal = session.execute(
        select(ScenarioRehearsalRunRow)
        .where(
            ScenarioRehearsalRunRow.project_id == project_id,
            ScenarioRehearsalRunRow.created_at >= created_at - timedelta(seconds=5),
            ScenarioRehearsalRunRow.created_at <= window_end + timedelta(seconds=5),
        )
        .order_by(ScenarioRehearsalRunRow.created_at.asc(), ScenarioRehearsalRunRow.id.asc())
        .limit(1)
    ).scalar_one_or_none()
    if rehearsal is None:
        return task

    chapter = _task_first_chapter(rehearsal.chapter_numbers_json)
    augmented = dict(task)
    history = list(augmented.get("stage_history", []))
    history.append(
        _new_stage_history_entry(
            "running_scenario_rehearsal",
            display_datetime=display_datetime,
            now=rehearsal.created_at,
            current_chapter=chapter,
            message=str(augmented.get("message", "")).strip(),
        )
    )
    recommendation = str(rehearsal.recommendation or "").strip().lower()
    if recommendation in {"patch", "replan"}:
        history.append(
            _new_stage_history_entry(
                "scenario_rehearsal_patch_required",
                display_datetime=display_datetime,
                now=rehearsal.created_at,
                current_chapter=chapter,
                message="Scenario rehearsal 要求计划补丁或重排。",
            )
        )
    elif recommendation == "block":
        history.append(
            _new_stage_history_entry(
                "scenario_rehearsal_blocked",
                display_datetime=display_datetime,
                now=rehearsal.created_at,
                current_chapter=chapter,
                message="Scenario rehearsal 阻断当前计划。",
            )
        )
    augmented["stage_history"] = history
    return augmented


def augment_task_with_rehearsal_history(
    session,
    task: dict[str, Any],
    *,
    display_datetime: DisplayDatetime,
) -> dict[str, Any]:
    task = _augment_with_scenario_rehearsal_history(
        session,
        task,
        display_datetime=display_datetime,
    )
    project_id = str(task.get("project_id", "") or "").strip()
    if not project_id or _task_has_stage(task, "running_provisional_preview"):
        return task

    created_at, window_end = _task_time_window(task)
    if created_at == datetime.min.replace(tzinfo=timezone.utc):
        return task

    execution = session.execute(
        select(ProvisionalBandExecution)
        .where(
            ProvisionalBandExecution.project_id == project_id,
            ProvisionalBandExecution.created_at >= created_at - timedelta(seconds=5),
            ProvisionalBandExecution.created_at <= window_end + timedelta(seconds=5),
        )
        .order_by(ProvisionalBandExecution.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()
    if execution is None:
        return task

    chapter = _task_first_chapter(execution.chapter_numbers_json)
    augmented = dict(task)
    history = list(augmented.get("stage_history", []))
    history.append(
        _new_stage_history_entry(
            "running_provisional_preview",
            display_datetime=display_datetime,
            now=execution.created_at,
            current_chapter=chapter,
            message=str(augmented.get("message", "")).strip(),
        )
    )
    if (
        str(execution.aggregate_verdict or "").strip().lower() == "fail"
        or int(execution.failure_count or 0) > 0
    ) and not _task_has_stage(augmented, "provisional_failed"):
        history.append(
            _new_stage_history_entry(
                "provisional_failed",
                display_datetime=display_datetime,
                now=execution.created_at,
                current_chapter=chapter,
                message="Provisional 预演失败，已阻断正式写作。",
            )
        )
    augmented["stage_history"] = history
    return augmented
