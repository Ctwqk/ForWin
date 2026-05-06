from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forwin.governance import DecisionEventInfo, DecisionEventType
from forwin.map.genesis_adapter import build_subworld_map_specs_from_genesis
from forwin.map.models import MapNodeRow
from forwin.map.service import build_interconnections_from_genesis_atlas, create_or_update_book_map
from forwin.models.genesis import BookGenesisRevision
from forwin.models.project import Project
from forwin.observability.payloads import audit_payload
from forwin.state.updater import StateUpdater


class GenesisMapBootstrap:
    def bootstrap_book_map_from_genesis(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project: Project,
        revision: BookGenesisRevision,
        pack: dict[str, Any],
        decision_event_id: str,
    ) -> dict[str, Any]:
        existing_nodes = int(
            session.execute(
                select(func.count(MapNodeRow.id)).where(MapNodeRow.project_id == project.id)
            ).scalar_one()
            or 0
        )
        if existing_nodes > 0:
            summary = {
                "skipped": True,
                "reason": "existing_book_map",
                "map_node_count": existing_nodes,
            }
            updater.save_decision_event(
                DecisionEventInfo(
                    project_id=project.id,
                    scope="project",
                    event_family="runtime_observation",
                    event_type=DecisionEventType.MAP_GENERATION_SUCCEEDED,
                    actor_type="system",
                    summary="项目已有 BookMap，跳过 Genesis 自动地图生成。",
                    payload=summary,
                    related_object_type="book_genesis_revision",
                    related_object_id=str(getattr(revision, "id", "") or ""),
                    parent_event_id=decision_event_id,
                )
            )
            return summary

        world = pack.get("world") if isinstance(pack.get("world"), dict) else {}
        if not world:
            world = {
                "map_atlas": pack.get("map_atlas") if isinstance(pack.get("map_atlas"), dict) else {},
            }
        map_atlas = world.get("map_atlas") if isinstance(world.get("map_atlas"), dict) else {}
        specs = build_subworld_map_specs_from_genesis(
            project_id=project.id,
            genesis_revision_id=str(getattr(revision, "id", "") or ""),
            map_atlas=map_atlas,
        )
        if not specs:
            raise ValueError("Genesis map_atlas 未能生成 BookMap spec。")

        updater.save_decision_event(
            DecisionEventInfo(
                project_id=project.id,
                scope="project",
                event_family="runtime_observation",
                event_type=DecisionEventType.MAP_GENERATION_STARTED,
                actor_type="system",
                summary="开始从 Genesis map_atlas 生成 Scheme C BookMap。",
                payload=audit_payload(
                    stage="map_generation",
                    status="started",
                    subworld_count=len(specs),
                    subworld_ids=[spec.subworld_id for spec in specs],
                ),
                related_object_type="book_genesis_revision",
                related_object_id=str(getattr(revision, "id", "") or ""),
                parent_event_id=decision_event_id,
            )
        )
        interconnections, interconnection_source = build_interconnections_from_genesis_atlas(
            project_id=project.id,
            specs=specs,
            map_atlas=map_atlas,
            genesis_revision_id=str(getattr(revision, "id", "") or ""),
        )
        result = create_or_update_book_map(
            session,
            specs,
            interconnections=interconnections if interconnections else None,
            interconnection_source=interconnection_source,
            commit=False,
        )
        if not result.validation_report.valid:
            message = "；".join(result.validation_report.errors) or "BookMap validation failed."
            raise ValueError(message)

        summary = {
            "skipped": False,
            "subworld_count": len(result.subworld_results),
            "region_count": sum(len(item.regions) for item in result.subworld_results),
            "map_node_count": sum(len(item.map_nodes) for item in result.subworld_results),
            "map_edge_count": sum(len(item.map_edges) for item in result.subworld_results)
            + len(result.inter_subworld_edges),
            "inter_subworld_edge_count": len(result.inter_subworld_edges),
            "interconnection_source": result.summary.get("interconnection_source", interconnection_source),
            "generation_run_count": len(result.subworld_results),
            "subworld_ids": [item.subworld_id for item in result.subworld_results],
        }
        updater.save_decision_event(
            DecisionEventInfo(
                project_id=project.id,
                scope="project",
                event_family="runtime_observation",
                event_type=DecisionEventType.MAP_GENERATION_SUCCEEDED,
                actor_type="system",
                summary="Genesis map_atlas 已生成 Scheme C BookMap。",
                payload=audit_payload(stage="map_generation", status="succeeded", **summary),
                related_object_type="book_genesis_revision",
                related_object_id=str(getattr(revision, "id", "") or ""),
                parent_event_id=decision_event_id,
            )
        )
        return summary

