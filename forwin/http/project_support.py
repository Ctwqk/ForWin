"""ForWin Web API – FastAPI interface for the novel generation system."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import HTTPException
from sqlalchemy import delete, select

from forwin.application.project_control import support as project_control_support
from forwin.api_schema import (
    BandCheckpointDetail,
    CausalReplayResponse,
    AuditInsightsResponse,
    ProjectAutomationSettings,
    TaskMutationResponse,
)
from forwin.audit.events import DecisionEventInfo, DecisionEventType
from forwin.generation.task_repository import GenerationTaskRepository
from forwin.planning.constraints import NarrativeConstraintInfo
from forwin.models.base import Base
from forwin.models.project import Project, ChapterPlan
from forwin.models.planning_control import (
    BandCheckpoint,
    NarrativeConstraint,
)
from forwin.models.audit import DecisionEvent
from forwin.models.task import GenerationTask
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.phase import (
    ChapterRewriteAttempt,
)
import forwin.models.phase  # noqa: F401
from forwin.http.request_support import (
    _get_session,
    _utcnow,
)
from forwin.http.runtime import (
    GENERATION_TERMINAL_STAGE_BY_STATUS,
    HttpRuntime,
)
from forwin.http.tasks import (
    _apply_generation_task_to_row,
    _coerce_task_datetime,
    _generation_task_from_row,
    _load_generation_task,
    _new_stage_history_entry,
    _run_generation_task_db_write,
    _task_is_deletable,
    _task_is_pausable,
    _task_is_terminable,
)


logger = logging.getLogger(__name__)


def _delete_project(session, project_id: str) -> None:
    chapter_plan_ids = (
        session.execute(
            select(ChapterPlan.id).where(ChapterPlan.project_id == project_id)
        )
        .scalars()
        .all()
    )
    if chapter_plan_ids:
        draft_ids = (
            session.execute(
                select(ChapterDraft.id).where(
                    ChapterDraft.chapter_plan_id.in_(chapter_plan_ids)
                )
            )
            .scalars()
            .all()
        )
        session.execute(
            delete(CandidateDraftRecord).where(
                CandidateDraftRecord.project_id == project_id
            )
        )
        session.execute(
            delete(ChapterRewriteAttempt).where(
                ChapterRewriteAttempt.project_id == project_id
            )
        )
        if draft_ids:
            session.execute(
                delete(ChapterReview).where(ChapterReview.draft_id.in_(draft_ids))
            )
            session.execute(delete(ChapterDraft).where(ChapterDraft.id.in_(draft_ids)))

    for table in reversed(Base.metadata.sorted_tables):
        if table.name == "projects" or "project_id" not in table.c:
            continue
        session.execute(delete(table).where(table.c.project_id == project_id))

    session.execute(delete(Project).where(Project.id == project_id))


def _apply_locked_task_update(
    row: GenerationTask,
    changes: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    task = _generation_task_from_row(row)
    normalized = dict(changes)
    normalized.pop("requested_chapters", None)
    current_status = str(task.get("status", "") or "").strip()
    if current_status == "queued":
        if bool(normalized.get("cancel_requested")):
            normalized["status"] = "cancelled"
        elif bool(normalized.get("pause_requested")):
            normalized["status"] = "paused"
    if task.get("cancel_requested") and normalized.get("status") in {
        "starting",
        "running",
        "needs_review",
    }:
        normalized.pop("status", None)
    if task.get("cancel_requested") and normalized.get("current_stage") not in {
        "terminating",
        "cancelled",
    }:
        normalized.pop("current_stage", None)
    if task.get("pause_requested") and normalized.get("status") in {
        "queued",
        "starting",
        "running",
    }:
        normalized.pop("status", None)
    if task.get("pause_requested") and normalized.get("current_stage") not in {
        "paused",
        "cancelled",
        "terminating",
    }:
        normalized.pop("current_stage", None)
    if "message" in normalized:
        normalized["message"] = str(normalized.get("message") or "")
    if "status" in normalized and normalized["status"] == "terminating":
        normalized["current_stage"] = "terminating"
    elif "status" in normalized:
        terminal_stage = GENERATION_TERMINAL_STAGE_BY_STATUS.get(
            str(normalized["status"]).strip()
        )
        if terminal_stage:
            normalized["current_stage"] = terminal_stage
    if "current_chapter" in normalized:
        try:
            normalized["current_chapter"] = int(
                normalized["current_chapter"] or 0
            )
        except (TypeError, ValueError):
            normalized["current_chapter"] = 0
    timestamp = now or _utcnow()
    next_status = str(
        normalized.get("status", task.get("status", "")) or ""
    ).strip()
    if next_status == "running" and str(task.get("lease_owner", "") or "").strip():
        normalized["heartbeat_at"] = timestamp
        normalized["lease_expires_at"] = timestamp + timedelta(
            seconds=_running_task_lease_seconds(task)
        )
    if normalized.get("status") == "paused":
        if bool(task.get("pause_requested")) or bool(
            normalized.get("pause_requested")
        ):
            normalized["pause_requested"] = True
        normalized["paused_at"] = timestamp
    next_stage = str(normalized.get("current_stage", "")).strip()
    if next_stage and next_stage != str(task.get("current_stage", "")).strip():
        history = list(task.get("stage_history", []))
        history.append(
            _new_stage_history_entry(
                next_stage,
                now=timestamp,
                current_chapter=int(
                    normalized.get(
                        "current_chapter", task.get("current_chapter", 0)
                    )
                    or 0
                ),
                message=str(
                    normalized.get("message", task.get("message", ""))
                ).strip(),
            )
        )
        normalized["stage_history"] = history

    task.update(normalized)
    task["updated_at"] = timestamp
    _apply_generation_task_to_row(row, task, now=timestamp)
    return task


def _update_task(runtime: HttpRuntime, task_id: str, **changes: Any) -> None:
    def _operation() -> None:
        with _get_session(runtime) as session:
            row = GenerationTaskRepository(session).get_for_update(task_id)
            if row is None or row.deleted_at is not None:
                return
            _apply_locked_task_update(row, changes)
            session.add(row)
            session.commit()

    _run_generation_task_db_write(
        _operation,
        context=f"update_generation_task:{task_id}",
    )


def _mutate_generation_task(
    runtime: HttpRuntime,
    task_id: str,
    action: Literal["pause", "terminate", "delete"],
) -> TaskMutationResponse:
    response: dict[str, TaskMutationResponse] = {}

    def _operation() -> None:
        with _get_session(runtime) as session:
            row = GenerationTaskRepository(session).get_for_update(task_id)
            if row is None or row.deleted_at is not None:
                raise HTTPException(404, "任务不存在")
            task = _generation_task_from_row(row)
            status = str(task.get("status", "") or "").strip()
            project_id = str(task.get("project_id", "") or "").strip()
            event_type: str | None = None
            event_summary = ""

            if action == "terminate":
                if not _task_is_terminable(task):
                    raise HTTPException(400, "当前任务状态不支持终止")
                queued = status == "queued"
                changes = {
                    "cancel_requested": True,
                    "status": "cancelled" if queued else "terminating",
                    "current_stage": "cancelled" if queued else "terminating",
                    "message": (
                        "任务尚未开始，已取消。"
                        if queued
                        else "已请求终止生成任务，系统会在下一个安全检查点停止。"
                    ),
                }
                event_type = DecisionEventType.TERMINATE_REQUESTED
                event_summary = "已请求终止生成任务。"
            elif action == "pause":
                if not _task_is_pausable(task):
                    raise HTTPException(400, "当前任务状态不支持安全暂停")
                queued = status == "queued"
                changes = {
                    "pause_requested": True,
                    "status": "paused" if queued else status,
                    "current_stage": (
                        "paused"
                        if queued
                        else str(task.get("current_stage", "") or "")
                    ),
                    "message": (
                        "任务尚未开始，已安全暂停。"
                        if queued
                        else "已请求安全暂停，系统会在下一个安全检查点保存进度并暂停。"
                    ),
                }
                event_type = DecisionEventType.PAUSE_REQUESTED
                event_summary = "已请求安全暂停生成任务。"
            elif action == "delete":
                if not _task_is_deletable(task):
                    raise HTTPException(400, "只有终态任务可以删除")
                changes = {"deleted": True, "message": "任务已删除。"}
            else:
                raise ValueError(f"unknown generation task mutation: {action}")

            updated = _apply_locked_task_update(row, changes)
            if project_id and event_type is not None:
                parent = _latest_related_decision_event(
                    session,
                    project_id=project_id,
                    related_object_type="generation_task",
                    related_object_id=task_id,
                )
                _log_decision_event(
                    session,
                    project_id=project_id,
                    task_id=task_id,
                    scope="task",
                    event_family="audit_action",
                    event_type=event_type,
                    actor_type="manual_ui",
                    summary=event_summary,
                    related_object_type="generation_task",
                    related_object_id=task_id,
                    parent_event_id=str(parent.id if parent is not None else ""),
                    causal_root_id=str(
                        parent.causal_root_id if parent is not None else ""
                    ),
                )
            snapshot = TaskMutationResponse(
                ok=True,
                task_kind="generation",
                task_id=task_id,
                status=str(updated.get("status", "")),
                message=(
                    "任务已删除。"
                    if action == "delete"
                    else str(updated.get("message", ""))
                ),
            )
            session.add(row)
            session.commit()
            response["value"] = snapshot

    _run_generation_task_db_write(
        _operation,
        context=f"{action}_generation_task:{task_id}",
    )
    return response["value"]


def _running_task_lease_seconds(task: dict[str, Any]) -> int:
    heartbeat_at = _coerce_task_datetime(task.get("heartbeat_at"))
    lease_expires_at = _coerce_task_datetime(task.get("lease_expires_at"))
    if (
        heartbeat_at > datetime.min.replace(tzinfo=timezone.utc)
        and lease_expires_at > heartbeat_at
    ):
        return max(30, int((lease_expires_at - heartbeat_at).total_seconds()))
    return 300


def _get_generation_task_or_404(
    runtime: HttpRuntime,
    task_id: str,
) -> dict[str, Any]:
    task = _load_generation_task(runtime, task_id)
    if task is None or task.get("deleted"):
        raise HTTPException(404, "任务不存在")
    return task


def _require_reason(reason: str, *, action: str) -> str:
    normalized = str(reason or "").strip()
    if not normalized:
        raise HTTPException(400, f"{action} 必须填写 reason。")
    return normalized


def _validate_constraint_payload(
    *, constraint_type: str, level: str, status: str
) -> tuple[str, str, str]:
    return project_control_support.validate_constraint_payload(
        constraint_type=constraint_type,
        level=level,
        status=status,
    )


def _persist_project_automation(
    session,
    project: Project,
    automation: ProjectAutomationSettings,
) -> ProjectAutomationSettings:
    return project_control_support.persist_project_automation(
        session, project, automation
    )


def _log_decision_event(session, **kwargs):
    return project_control_support.log_decision_event(session, **kwargs)


def _latest_band_checkpoint_row(session, *, project_id: str, band_id: str = ""):
    return project_control_support.latest_band_checkpoint_row(
        session,
        project_id=project_id,
        band_id=band_id,
    )


def _serialize_band_checkpoint(
    row: BandCheckpoint, *, session=None
) -> BandCheckpointDetail:
    return project_control_support.serialize_band_checkpoint(row, session=session)


def _serialize_constraint(row: NarrativeConstraint) -> NarrativeConstraintInfo:
    return project_control_support.serialize_constraint(row)


def _serialize_decision_event(row: DecisionEvent) -> DecisionEventInfo:
    return project_control_support.serialize_decision_event(row)


def _list_decision_event_rows(session, **kwargs) -> list[DecisionEvent]:
    return project_control_support.list_decision_event_rows(session, **kwargs)


def _latest_related_decision_event(session, **kwargs) -> DecisionEvent | None:
    return project_control_support.latest_related_decision_event(session, **kwargs)


def _decision_refs_for_chapter_review(
    session,
    *,
    project_id: str,
    chapter_number: int,
    review_id: str,
) -> list[DecisionEventInfo]:
    return project_control_support.decision_refs_for_chapter_review(
        session,
        project_id=project_id,
        chapter_number=chapter_number,
        review_id=review_id,
    )


def _build_causal_replay(
    session,
    *,
    project_id: str,
    scope: str = "",
    arc_id: str = "",
    band_id: str = "",
    chapter_number: int = 0,
    task_id: str = "",
) -> CausalReplayResponse:
    return project_control_support.build_causal_replay(
        session,
        project_id=project_id,
        scope=scope,
        arc_id=arc_id,
        band_id=band_id,
        chapter_number=chapter_number,
        task_id=task_id,
    )


def _build_audit_insights(session, *, project_id: str) -> AuditInsightsResponse:
    return project_control_support.build_audit_insights(
        session, project_id=project_id
    )


__all__ = [name for name in globals() if not name.startswith("__")]
