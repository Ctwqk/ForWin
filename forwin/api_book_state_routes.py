from __future__ import annotations

from typing import Any, Callable

from fastapi import HTTPException
from sqlalchemy import select

from forwin.book_state import BookStateProjection, LegacyBookStateImporter
from forwin.book_state.repository import BookStateRepository
from forwin.models.book_state import GraphDeltaRow, WorldSnapshotRow
from forwin.models.project import Project


def _require_project(session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def build_handlers(*, get_session: Callable[[], Any]) -> dict[str, Callable[..., Any]]:
    def _resolve_as_of_chapter(session, project_id: str, as_of_chapter: int) -> int:
        if as_of_chapter and as_of_chapter > 0:
            return int(as_of_chapter)
        latest_snapshot = session.execute(
            select(WorldSnapshotRow.as_of_chapter)
            .where(WorldSnapshotRow.project_id == project_id)
            .order_by(WorldSnapshotRow.as_of_chapter.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest_snapshot is not None:
            return int(latest_snapshot or 0)
        latest_delta = session.execute(
            select(GraphDeltaRow.chapter_number)
            .where(GraphDeltaRow.project_id == project_id)
            .order_by(GraphDeltaRow.chapter_number.desc())
            .limit(1)
        ).scalar_one_or_none()
        return int(latest_delta or 0)

    def get_book_state_runtime(project_id: str, as_of_chapter: int = 0) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            as_of = _resolve_as_of_chapter(session, project_id, as_of_chapter)
            runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=as_of)
            return {
                "project_id": project_id,
                "as_of_chapter": as_of,
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

    def get_book_state_snapshot(project_id: str, as_of_chapter: int = 0) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            as_of = _resolve_as_of_chapter(session, project_id, as_of_chapter)
            repo = BookStateRepository(session)
            snapshot = repo.latest_world_snapshot(project_id, as_of)
            if snapshot is None:
                runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=as_of)
                return {
                    "project_id": project_id,
                    "as_of_chapter": as_of,
                    "snapshot": None,
                    "materialized": False,
                    "runtime": {
                        "world_node_count": len(runtime.world.nodes_by_id),
                        "world_edge_count": len(runtime.world.edges_by_id),
                        "fact_count": len(runtime.world.facts_by_id),
                        "map_node_count": len(runtime.map.nodes_by_id),
                        "map_edge_count": len([edge_id for edge_id in runtime.map.edges_by_id if "__reverse" not in edge_id]),
                    },
                }
            return {
                "project_id": project_id,
                "as_of_chapter": as_of,
                "materialized": True,
                "snapshot": snapshot.model_dump(mode="json"),
            }

    def list_book_state_nodes(project_id: str, as_of_chapter: int = 0) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            as_of = _resolve_as_of_chapter(session, project_id, as_of_chapter)
            repo = BookStateRepository(session)
            nodes = repo.list_world_nodes(project_id, as_of_chapter=as_of)
            facts = repo.list_fact_nodes(project_id, as_of_chapter=as_of)
            return {
                "project_id": project_id,
                "as_of_chapter": as_of,
                "nodes": [node.model_dump(mode="json") for node in nodes],
                "facts": [fact.model_dump(mode="json") for fact in facts],
            }

    def list_book_state_edges(project_id: str, as_of_chapter: int = 0) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            as_of = _resolve_as_of_chapter(session, project_id, as_of_chapter)
            repo = BookStateRepository(session)
            return {
                "project_id": project_id,
                "as_of_chapter": as_of,
                "edges": [
                    edge.model_dump(mode="json")
                    for edge in repo.list_world_edges(project_id, as_of_chapter=as_of)
                ],
            }

    def list_book_state_deltas(project_id: str, through_chapter: int = 0, after_chapter: int = -1) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            through = _resolve_as_of_chapter(session, project_id, through_chapter)
            repo = BookStateRepository(session)
            deltas = repo.list_graph_deltas(project_id, after_chapter=after_chapter, through_chapter=through)
            return {
                "project_id": project_id,
                "through_chapter": through,
                "after_chapter": after_chapter,
                "deltas": [delta.model_dump(mode="json") for delta in deltas],
            }

    def list_book_state_cognition(project_id: str, as_of_chapter: int = 0) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            as_of = _resolve_as_of_chapter(session, project_id, as_of_chapter)
            repo = BookStateRepository(session)
            return {
                "project_id": project_id,
                "as_of_chapter": as_of,
                "overlays": [
                    overlay.model_dump(mode="json")
                    for overlay in repo.list_cognition_overlays(project_id, as_of_chapter=as_of)
                ],
            }

    def list_book_state_reader_promises(project_id: str, as_of_chapter: int = 0) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            as_of = _resolve_as_of_chapter(session, project_id, as_of_chapter)
            repo = BookStateRepository(session)
            return {
                "project_id": project_id,
                "as_of_chapter": as_of,
                "reader_promises": [
                    promise.model_dump(mode="json")
                    for promise in repo.list_reader_promises_native(project_id, as_of_chapter=as_of)
                ],
                "reader_promise_nodes": [
                    node.model_dump(mode="json")
                    for node in repo.list_reader_promises(project_id, as_of_chapter=as_of)
                ],
                "reader_experience_deltas": [
                    item.model_dump(mode="json")
                    for item in repo.list_reader_experience_deltas(project_id, through_chapter=as_of)
                ],
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
            as_of = _resolve_as_of_chapter(session, project_id, as_of_chapter)
            observer = (observer_type, observer_id) if observer_type and observer_id else None
            runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=as_of, observer_keys=[observer] if observer else None)
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
        "get_book_state_snapshot": get_book_state_snapshot,
        "list_book_state_nodes": list_book_state_nodes,
        "list_book_state_edges": list_book_state_edges,
        "list_book_state_deltas": list_book_state_deltas,
        "list_book_state_cognition": list_book_state_cognition,
        "list_book_state_reader_promises": list_book_state_reader_promises,
        "get_book_state_path": get_book_state_path,
        "import_book_state_legacy": import_book_state_legacy,
    }
