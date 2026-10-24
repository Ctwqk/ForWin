from __future__ import annotations

from typing import Any
from forwin.models.project import (
    ArcPlanVersion,
    Project,
)
from forwin.observability.payloads import (
    audit_payload,
    event_error_payload,
)
from forwin.audit.events import (
    DecisionEventInfo,
    DecisionEventType,
)
from forwin.map.service import ensure_book_map_from_genesis_atlas
from sqlalchemy.orm import Session
from forwin.state.updater import StateUpdater


def materialize_book_arcs(
    self,
    *,
    session: Session,
    updater: StateUpdater,
    project: Project,
    revision,
) -> list[ArcPlanVersion]:
    return self.handoff.arc_materializer.materialize_book_arcs(
        session=session,
        updater=updater,
        project=project,
        revision=revision,
    )


def _ensure_arc_map_expansion(
    self,
    *,
    session: Session,
    updater: StateUpdater,
    project: Project,
    revision,
    pack: dict[str, Any],
    arc_row: ArcPlanVersion,
    parent_event_id: str = "",
) -> None:
    world = pack.get("world") if isinstance(pack.get("world"), dict) else {}
    map_atlas = (
        world.get("map_atlas") if isinstance(world.get("map_atlas"), dict) else {}
    )
    if not map_atlas and isinstance(pack.get("map_atlas"), dict):
        map_atlas = pack["map_atlas"]
    updater.save_decision_event(
        DecisionEventInfo(
            project_id=project.id,
            scope="project",
            event_family="runtime_observation",
            event_type=DecisionEventType.MAP_EXPANSION_STARTED,
            actor_type="system",
            summary=f"开始为 Arc {arc_row.arc_number} 补齐 Genesis BookMap。",
            payload=audit_payload(
                stage="map_expansion",
                status="started",
                arc_number=arc_row.arc_number,
            ),
            related_object_type="arc_plan_version",
            related_object_id=arc_row.id,
            parent_event_id=parent_event_id,
        )
    )
    try:
        result = ensure_book_map_from_genesis_atlas(
            session,
            project_id=project.id,
            genesis_revision_id=str(getattr(revision, "id", "") or ""),
            map_atlas=map_atlas,
            commit=False,
        )
    except Exception as exc:
        updater.save_decision_event(
            DecisionEventInfo(
                project_id=project.id,
                scope="project",
                event_family="runtime_observation",
                event_type=DecisionEventType.MAP_EXPANSION_FAILED,
                actor_type="system",
                summary=f"Arc {arc_row.arc_number} BookMap expansion 失败。",
                reason=str(exc),
                payload=event_error_payload(
                    exc,
                    stage="map_expansion",
                    arc_number=arc_row.arc_number,
                ),
                related_object_type="arc_plan_version",
                related_object_id=arc_row.id,
                parent_event_id=parent_event_id,
            )
        )
        raise
    if not result.validation_report.valid:
        message = (
            "；".join(result.validation_report.errors)
            or "BookMap expansion validation failed."
        )
        updater.save_decision_event(
            DecisionEventInfo(
                project_id=project.id,
                scope="project",
                event_family="runtime_observation",
                event_type=DecisionEventType.MAP_EXPANSION_FAILED,
                actor_type="system",
                summary=f"Arc {arc_row.arc_number} BookMap expansion 校验失败。",
                reason=message,
                payload=audit_payload(
                    stage="map_expansion",
                    status="failed",
                    arc_number=arc_row.arc_number,
                    validation_report=result.validation_report.model_dump(mode="json"),
                ),
                related_object_type="arc_plan_version",
                related_object_id=arc_row.id,
                parent_event_id=parent_event_id,
            )
        )
        raise ValueError(message)
    updater.save_decision_event(
        DecisionEventInfo(
            project_id=project.id,
            scope="project",
            event_family="runtime_observation",
            event_type=DecisionEventType.MAP_EXPANSION_SUCCEEDED,
            actor_type="system",
            summary=f"Arc {arc_row.arc_number} BookMap expansion 已完成。",
            payload=audit_payload(
                stage="map_expansion",
                status="succeeded",
                arc_number=arc_row.arc_number,
                summary=dict(result.summary),
                validation_report=result.validation_report.model_dump(mode="json"),
            ),
            related_object_type="arc_plan_version",
            related_object_id=arc_row.id,
            parent_event_id=parent_event_id,
        )
    )


__all__ = [
    "materialize_book_arcs",
    "_ensure_arc_map_expansion",
]
