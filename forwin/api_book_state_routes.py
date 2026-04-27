from __future__ import annotations

from typing import Any, Callable

from fastapi import HTTPException
from sqlalchemy import select

from forwin.book_state import BookStateProjection, BookStateRepository, LegacyBookStateImporter
from forwin.governance import DecisionEventInfo, DecisionEventType
from forwin.observability.payloads import audit_payload, event_error_payload
from forwin.models.project import Project
from forwin.state.updater import StateUpdater


def _require_project(session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def build_handlers(*, get_session: Callable[[], Any]) -> dict[str, Callable[..., Any]]:
    def _resolve_as_of_chapter(session, project_id: str, as_of_chapter: int | None = None) -> int:
        if as_of_chapter is not None and int(as_of_chapter or 0) > 0:
            return int(as_of_chapter)
        return BookStateRepository(session).latest_available_chapter(project_id)

    def get_book_state_runtime(project_id: str, as_of_chapter: int | None = None) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            resolved_chapter = (
                BookStateRepository(session).latest_available_chapter(project_id)
                if as_of_chapter is None
                else int(as_of_chapter)
            )
            runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=resolved_chapter)
            return {
                "schema_version": "book_state.runtime.v1",
                "project_id": project_id,
                "as_of_chapter": resolved_chapter,
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
        as_of_chapter: int | None = None,
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
            return {
                "schema_version": "book_state.path.v1",
                "project_id": project_id,
                "as_of_chapter": as_of,
                **result.model_dump(mode="json"),
            }

    def import_book_state_legacy(project_id: str) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            updater = StateUpdater(session)
            started = updater.save_decision_event(
                DecisionEventInfo(
                    project_id=project_id,
                    scope="project",
                    event_family="runtime_observation",
                    event_type=DecisionEventType.LEGACY_REGION_PROMOTION_STARTED,
                    actor_type="system",
                    summary="开始执行 legacy BookState import 与 region_drafts promotion。",
                    payload=audit_payload(
                        stage="legacy_region_promotion",
                        status="started",
                        project_id=project_id,
                    ),
                    related_object_type="project",
                    related_object_id=project_id,
                )
            )
            try:
                with session.begin_nested():
                    counts = LegacyBookStateImporter(session).import_project(project_id)
            except Exception as exc:
                updater.save_decision_event(
                    DecisionEventInfo(
                        project_id=project_id,
                        scope="project",
                        event_family="runtime_observation",
                        event_type=DecisionEventType.LEGACY_REGION_PROMOTION_FAILED,
                        actor_type="system",
                        summary="legacy BookState import 或 region_drafts promotion 失败。",
                        reason=str(exc),
                        payload=event_error_payload(
                            exc,
                            stage="legacy_region_promotion",
                            project_id=project_id,
                        ),
                        related_object_type="project",
                        related_object_id=project_id,
                        parent_event_id=started.id,
                    )
                )
                session.commit()
                raise
            updater.save_decision_event(
                DecisionEventInfo(
                    project_id=project_id,
                    scope="project",
                    event_family="runtime_observation",
                    event_type=DecisionEventType.LEGACY_REGION_PROMOTION_SUCCEEDED,
                    actor_type="system",
                    summary="legacy BookState import 与 region_drafts promotion 已完成。",
                    payload=audit_payload(
                        stage="legacy_region_promotion",
                        status="succeeded",
                        project_id=project_id,
                        migration_report=counts.get("migration_report", {}) if isinstance(counts, dict) else {},
                    ),
                    related_object_type="project",
                    related_object_id=project_id,
                    parent_event_id=started.id,
                )
            )
            session.commit()
            return {
                "schema_version": "book_state.legacy_import.v1",
                "project_id": project_id,
                "imported": counts,
                "migration_report": counts.get("migration_report", {}) if isinstance(counts, dict) else {},
            }

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
