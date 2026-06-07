from __future__ import annotations

from forwin.book_genesis_core.constants import *
from forwin.book_genesis_core.helpers import *
from forwin.book_genesis_core.fallbacks import *
from forwin.book_genesis_core.names_paths import *

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

def materialize_arc_chapter_plans(
    self,
    *,
    session: Session,
    updater: StateUpdater,
    project: Project,
    revision,
    arc_number: int,
    decision_event_id: str = "",
    ensure_arc_map: bool = True,
) -> ArcPlanVersion:
    return self.handoff.chapter_materializer.materialize_arc_chapter_plans(
        session=session,
        updater=updater,
        project=project,
        revision=revision,
        arc_number=arc_number,
        decision_event_id=decision_event_id,
        ensure_arc_map=ensure_arc_map,
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
    map_atlas = world.get("map_atlas") if isinstance(world.get("map_atlas"), dict) else {}
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
        message = "；".join(result.validation_report.errors) or "BookMap expansion validation failed."
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

def promote_next_arc_if_needed(
    self,
    *,
    session: Session,
    updater: StateUpdater,
    project: Project,
    revision,
) -> bool:
    next_arc = session.execute(
        select(ArcPlanVersion)
        .where(
            ArcPlanVersion.project_id == project.id,
            ArcPlanVersion.status == "planned",
        )
        .order_by(ArcPlanVersion.arc_number.asc(), ArcPlanVersion.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()
    if next_arc is None:
        return False
    active_rows = session.execute(
        select(ArcPlanVersion)
        .where(
            ArcPlanVersion.project_id == project.id,
            ArcPlanVersion.status == "active",
        )
    ).scalars().all()
    for row in active_rows:
        row.status = "completed"
        session.add(row)
    next_arc.status = "active"
    session.add(next_arc)
    self.materialize_arc_chapter_plans(
        session=session,
        updater=updater,
        project=project,
        revision=revision,
        arc_number=next_arc.arc_number,
    )
    session.flush()
    return True



__all__ = ['materialize_book_arcs', 'materialize_arc_chapter_plans', '_ensure_arc_map_expansion', 'promote_next_arc_if_needed']
