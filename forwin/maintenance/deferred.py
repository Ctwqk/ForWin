from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel, Field

from forwin.audit.events import (
    DecisionEventInfo,
    DecisionEventType,
)
from forwin.models.audit import DecisionEvent


class DeferredMaintenanceRecord(BaseModel):
    project_id: str
    task_id: str = ""
    chapter_number: int = 0
    task_type: str
    reason: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    maintenance_run_ids: list[str] = Field(default_factory=list)
    related_object_type: str = ""
    related_object_id: str = ""


def record_deferred_maintenance(updater, record: DeferredMaintenanceRecord) -> None:  # noqa: ANN001
    run_ids = [
        str(item).strip() for item in record.maintenance_run_ids if str(item).strip()
    ]
    related_object_type = record.related_object_type
    related_object_id = record.related_object_id
    if run_ids and not related_object_type and not related_object_id:
        related_object_type = "post_canon_maintenance_run"
        related_object_id = run_ids[0]
    event_id = _deferred_event_id(record, run_ids)
    session = getattr(updater, "session", None)
    if (
        event_id
        and session is not None
        and session.get(DecisionEvent, event_id) is not None
    ):
        return
    updater.save_decision_event(
        DecisionEventInfo(
            id=event_id,
            project_id=record.project_id,
            task_id=record.task_id,
            chapter_number=record.chapter_number,
            scope="chapter" if record.chapter_number else "project",
            event_family="runtime_observation",
            event_type=DecisionEventType.DEFERRED_MAINTENANCE_RECORDED,
            actor_type="system",
            summary=f"Deferred maintenance recorded: {record.task_type}",
            reason=record.reason,
            payload={
                "task_type": record.task_type,
                "maintenance_run_ids": run_ids,
                **record.payload,
            },
            related_object_type=related_object_type,
            related_object_id=related_object_id,
        )
    )


def _deferred_event_id(
    record: DeferredMaintenanceRecord,
    run_ids: list[str],
) -> str:
    canon_commit_id = str(record.payload.get("canon_commit_id") or "").strip()
    anchors = sorted(set(run_ids))
    if not anchors and canon_commit_id:
        anchors = [f"canon:{canon_commit_id}"]
    if not anchors:
        return ""
    identity = "\0".join(
        (
            "deferred-maintenance:v1",
            record.project_id,
            str(int(record.chapter_number or 0)),
            record.task_type,
            *anchors,
        )
    )
    return "deferred-maintenance-" + hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()
