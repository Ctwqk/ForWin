from __future__ import annotations

import json
from typing import Any, Callable

from fastapi import HTTPException

from forwin.map.service import (
    compute_distance,
    ensure_book_map_from_genesis_atlas,
    get_book_map_runtime,
)
from forwin.models.genesis import BookGenesisRevision
from forwin.models.project import Project


def _require_project(session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def _active_revision(session, project: Project) -> BookGenesisRevision | None:
    revision_id = str(getattr(project, "active_genesis_revision_id", "") or "").strip()
    return session.get(BookGenesisRevision, revision_id) if revision_id else None


def _map_atlas_from_revision(revision: BookGenesisRevision) -> dict[str, Any]:
    try:
        pack = json.loads(revision.pack_json or "{}") or {}
    except (TypeError, ValueError, json.JSONDecodeError):
        pack = {}
    if not isinstance(pack, dict):
        return {}
    world = pack.get("world") if isinstance(pack.get("world"), dict) else {}
    if world and isinstance(world.get("map_atlas"), dict):
        return world["map_atlas"]
    return pack.get("map_atlas") if isinstance(pack.get("map_atlas"), dict) else {}


def build_handlers(*, get_session: Callable[[], Any]) -> dict[str, Callable[..., Any]]:
    def get_project_map_runtime(project_id: str) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            runtime = get_book_map_runtime(session, project_id)
            return {
                "schema_version": "map.runtime.v1",
                "project_id": project_id,
                "subworld_count": len(runtime.subworlds_by_id),
                "region_count": len(runtime.regions_by_id),
                "map_node_count": len(runtime.map_nodes_by_id),
                "map_edge_count": len(runtime.map_edges_by_id),
                "inter_subworld_edge_count": len(runtime.inter_subworld_edges_by_id),
                "subworld_ids": sorted(runtime.subworlds_by_id),
            }

    def get_project_map_path(
        project_id: str,
        from_node_id: str,
        to_node_id: str,
        metric: str = "travel_time",
        allow_hidden: bool = False,
        allow_blocked: bool = False,
    ) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            result = compute_distance(
                session,
                project_id,
                from_node_id,
                to_node_id,
                metric=metric,
                allow_hidden=allow_hidden,
                allow_blocked=allow_blocked,
            )
            return {
                "schema_version": "map.path.v1",
                "project_id": project_id,
                **result.model_dump(mode="json"),
            }

    def ensure_project_map_from_genesis(project_id: str) -> dict[str, Any]:
        with get_session() as session:
            project = _require_project(session, project_id)
            revision = _active_revision(session, project)
            if revision is None:
                raise HTTPException(status_code=409, detail="active genesis revision not found")
            result = ensure_book_map_from_genesis_atlas(
                session,
                project_id=project_id,
                genesis_revision_id=revision.id,
                map_atlas=_map_atlas_from_revision(revision),
                commit=False,
            )
            session.commit()
            return {
                "schema_version": "map.ensure.v1",
                "project_id": project_id,
                "summary": dict(result.summary),
                "validation_report": result.validation_report.model_dump(mode="json"),
            }

    return {
        "get_project_map_runtime": get_project_map_runtime,
        "get_project_map_path": get_project_map_path,
        "ensure_project_map_from_genesis": ensure_project_map_from_genesis,
    }
