from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from forwin.governance import DecisionEventInfo, DecisionEventType


class DeferredMaintenanceRecord(BaseModel):
    project_id: str
    chapter_number: int = 0
    task_type: str
    reason: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


def record_deferred_maintenance(updater, record: DeferredMaintenanceRecord) -> None:  # noqa: ANN001
    updater.save_decision_event(
        DecisionEventInfo(
            project_id=record.project_id,
            chapter_number=record.chapter_number,
            scope="chapter" if record.chapter_number else "project",
            event_family="runtime_observation",
            event_type=DecisionEventType.DEFERRED_MAINTENANCE_RECORDED,
            actor_type="system",
            summary=f"Deferred maintenance recorded: {record.task_type}",
            reason=record.reason,
            payload={"task_type": record.task_type, **record.payload},
        )
    )
