from __future__ import annotations

from typing import Any, Callable

from fastapi import HTTPException

from forwin.book_state import BookStateProjection, LegacyBookStateImporter
from forwin.models.project import Project


def _require_project(session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def build_handlers(*, get_session: Callable[[], Any]) -> dict[str, Callable[..., Any]]:
    def get_book_state_runtime(project_id: str, as_of_chapter: int = 0) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=as_of_chapter)
            return {
                "project_id": project_id,
                "as_of_chapter": as_of_chapter,
                "world_node_count": len(runtime.world.nodes_by_id),
                "world_edge_count": len(runtime.world.edges_by_id),
                "fact_count": len(runtime.world.facts_by_id),
                "map_node_count": len(runtime.map.nodes_by_id),
                "map_edge_count": len([edge_id for edge_id in runtime.map.edges_by_id if "__reverse" not in edge_id]),
                "observer_count": len(runtime.cognition_by_observer),
                "narrative_node_count": len(runtime.narrative.nodes_by_id),
                "narrative_edge_count": len(runtime.narrative.edges_by_id),
                "active_world_line_ids": runtime.narrative.active_world_line_ids(),
                "open_gap_ids": runtime.narrative.open_gap_ids(),
            }

    def get_book_state_path(
        project_id: str,
        from_node_id: str,
        to_node_id: str,
        metric: str = "travel_time",
        as_of_chapter: int = 0,
        observer_type: str = "",
        observer_id: str = "",
        allow_hidden: bool = False,
        allow_blocked: bool = False,
    ) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            observer = (observer_type, observer_id) if observer_type and observer_id else None
            runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=as_of_chapter, observer_keys=[observer] if observer else None)
            result = runtime.map.shortest_path(from_node_id, to_node_id, metric=metric, observer=observer, allow_hidden=allow_hidden, allow_blocked=allow_blocked)
            return result.model_dump(mode="json")

    def import_book_state_legacy(project_id: str) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            with session.begin_nested():
                counts = LegacyBookStateImporter(session).import_project(project_id)
            session.commit()
            return {"project_id": project_id, "imported": counts}

    return {
        "get_book_state_runtime": get_book_state_runtime,
        "get_book_state_path": get_book_state_path,
        "import_book_state_legacy": import_book_state_legacy,
    }
