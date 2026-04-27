from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.orm import Session

from forwin.book_state.cognition import CognitionView
from forwin.book_state.repository import BookStateRepository
from forwin.book_state.runtime import BookStateRuntime
from forwin.models.base import new_id
from forwin.protocol.book_state import (
    CognitionOverlay,
    CognitionSnapshot,
    GraphDelta,
    MapSnapshot,
    WorldSnapshot,
)


_BLOCKED_EDGE_STATUSES = {"blocked", "destroyed", "sealed"}
_HIDDEN_EDGE_STATUSES = {"hidden"}


class BookStateProjection:
    """Materialize BookState runtime graphs from base rows, snapshots, and deltas."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = BookStateRepository(session)

    def load_runtime_as_of(
        self,
        project_id: str,
        *,
        as_of_chapter: int,
        observer_keys: list[tuple[str, str]] | None = None,
    ) -> BookStateRuntime:
        world_snapshot = self.repo.latest_world_snapshot(project_id, as_of_chapter)
        map_snapshot = self.repo.latest_map_snapshot(project_id, as_of_chapter)

        cognition_views = self.repo.load_cognition_views(
            project_id,
            as_of_chapter=as_of_chapter,
            observer_keys=observer_keys,
        )
        world = self.repo.load_base_world_graph(
            project_id,
            as_of_chapter=as_of_chapter,
            state_index=(
                world_snapshot.world_node_state_index
                if world_snapshot is not None and world_snapshot.world_node_state_index
                else None
            ),
        )
        map_graph = self.repo.load_base_map_graph(
            project_id,
            as_of_chapter=as_of_chapter,
            map_node_index=(
                map_snapshot.map_node_index
                if map_snapshot is not None and map_snapshot.map_node_index
                else None
            ),
            map_edge_index=(
                map_snapshot.map_edge_index
                if map_snapshot is not None and map_snapshot.map_edge_index
                else None
            ),
            cognition_by_observer=cognition_views,
        )
        narrative = self.repo.load_narrative_graph(project_id, as_of_chapter=as_of_chapter)
        runtime = BookStateRuntime(
            project_id=project_id,
            as_of_chapter=as_of_chapter,
            world=world,
            map_graph=map_graph,
            cognition_by_observer=cognition_views,
            narrative=narrative,
        )

        world_after = world_snapshot.as_of_chapter if world_snapshot else -1
        map_after = map_snapshot.as_of_chapter if map_snapshot else -1
        cognition_after = max((int(view.as_of_chapter or 0) for view in cognition_views.values()), default=-1)
        # Narrative rows are persisted as side effects without full snapshots; replay
        # narrative deltas from the beginning and rely on graph upserts to be idempotent.
        narrative_after = -1
        after_chapter = min(world_after, map_after, cognition_after, narrative_after)
        for delta in self.repo.list_graph_deltas(
            project_id,
            after_chapter=after_chapter,
            through_chapter=as_of_chapter,
        ):
            self.apply_delta_to_runtime(
                runtime,
                delta,
                apply_world=delta.chapter_number > world_after,
                apply_map=delta.chapter_number > map_after,
                apply_cognition=delta.chapter_number > cognition_after,
                apply_narrative=delta.chapter_number > narrative_after,
            )
        return runtime

    def apply_delta_to_runtime(
        self,
        runtime: BookStateRuntime,
        delta: GraphDelta,
        *,
        apply_world: bool = True,
        apply_map: bool = True,
        apply_cognition: bool = True,
        apply_narrative: bool = True,
    ) -> None:
        if apply_world:
            runtime.world.apply_delta(delta)
        if apply_map:
            for patch in delta.map_patches:
                runtime.map.apply_map_patch(patch)
        if apply_cognition:
            for patch in delta.cognition_patches:
                key = (str(patch.observer_type), patch.observer_id)
                view = runtime.cognition_by_observer.get(key)
                if view is None:
                    view = CognitionView(
                        CognitionOverlay(
                            id=f"cog_{runtime.project_id}_{key[0]}_{key[1]}_{runtime.as_of_chapter}",
                            project_id=runtime.project_id,
                            observer_type=key[0],
                            observer_id=key[1],
                            as_of_chapter=runtime.as_of_chapter,
                        )
                    )
                    runtime.cognition_by_observer[key] = view
                    runtime.map.cognition_by_observer[key] = view
                view.apply_cognition_patch(patch.field_path, str(patch.op), patch.new_value)
                for ref in patch.evidence_refs:
                    view.evidence_by_ref.setdefault(str(patch.new_value), []).append(ref)
        if apply_narrative:
            for patch in delta.narrative_patches:
                runtime.narrative.apply_narrative_patch(patch, project_id=delta.project_id)

    def persist_snapshots(
        self,
        runtime: BookStateRuntime,
        *,
        as_of_chapter: int,
        as_of_story_time: str = "",
        base_snapshot_id: str = "",
        source_delta_ids: list[str] | None = None,
        active_world_line_ids: list[str] | None = None,
        open_gap_ids: list[str] | None = None,
    ) -> tuple[WorldSnapshot, MapSnapshot, list[CognitionSnapshot]]:
        source_delta_ids = source_delta_ids or []
        world_snapshot = WorldSnapshot(
            id=f"world_snapshot_{runtime.project_id}_{as_of_chapter}_{new_id()}",
            project_id=runtime.project_id,
            as_of_chapter=as_of_chapter,
            as_of_story_time=as_of_story_time,
            base_snapshot_id=base_snapshot_id,
            objective_graph_digest=_digest(
                {
                    "nodes": runtime.world.nodes_by_id,
                    "edges": runtime.world.edges_by_id,
                    "facts": runtime.world.facts_by_id,
                    "states": runtime.world.states_by_node_id,
                }
            ),
            map_graph_digest=_digest(
                {
                    "nodes": runtime.map.nodes_by_id,
                    "edges": {
                        edge_id: edge
                        for edge_id, edge in runtime.map.edges_by_id.items()
                        if "__reverse" not in edge_id
                    },
                }
            ),
            reader_overlay_digest=_digest(runtime.cognition_by_observer.get(("reader", "reader"), {})),
            character_overlay_digests={
                observer_id: _digest(view.overlay)
                for (observer_type, observer_id), view in runtime.cognition_by_observer.items()
                if observer_type == "character"
            },
            world_node_state_index={
                node_id: dict(state)
                for node_id, state in runtime.world.states_by_node_id.items()
            },
            active_edge_ids=[
                edge_id
                for edge_id, edge in runtime.world.edges_by_id.items()
                if edge.is_active
            ],
            active_fact_ids=list(runtime.world.facts_by_id),
            active_world_line_ids=active_world_line_ids or runtime.narrative.active_world_line_ids(),
            open_gap_ids=open_gap_ids or runtime.narrative.open_gap_ids(),
            active_promise_ids=[
                node_id
                for node_id, node in runtime.world.nodes_by_id.items()
                if node.node_type == "reader_promise" and node.is_active
            ],
            objective_state_summary=_summary_from_runtime(runtime),
            reader_state_summary=_reader_summary_from_runtime(runtime),
            source_delta_ids=source_delta_ids,
        )
        map_snapshot = MapSnapshot(
            id=f"map_snapshot_{runtime.project_id}_{as_of_chapter}_{new_id()}",
            project_id=runtime.project_id,
            as_of_chapter=as_of_chapter,
            map_node_index=dict(runtime.map.nodes_by_id),
            map_edge_index={
                edge_id: edge
                for edge_id, edge in runtime.map.edges_by_id.items()
                if "__reverse" not in edge_id
            },
            blocked_edge_ids=[
                edge_id
                for edge_id, edge in runtime.map.edges_by_id.items()
                if "__reverse" not in edge_id and edge.status in _BLOCKED_EDGE_STATUSES
            ],
            hidden_edge_ids=[
                edge_id
                for edge_id, edge in runtime.map.edges_by_id.items()
                if "__reverse" not in edge_id
                and (
                    edge.status in _HIDDEN_EDGE_STATUSES
                    or edge.edge_type == "hidden_route"
                    or not edge.discovered_by_default
                )
            ],
        )
        cognition_snapshots = [
            CognitionSnapshot(
                id=f"cognition_snapshot_{runtime.project_id}_{view.observer_type}_{view.observer_id}_{as_of_chapter}_{new_id()}",
                project_id=runtime.project_id,
                observer_type=view.observer_type,
                observer_id=view.observer_id,
                as_of_chapter=as_of_chapter,
                overlay_id=f"cog_{runtime.project_id}_{view.observer_type}_{view.observer_id}_{as_of_chapter}",
                visible_refs=sorted(view.visible_refs),
                suspected_refs=sorted(view.suspected_refs),
                confirmed_refs=sorted(view.confirmed_refs),
            )
            for view in runtime.cognition_by_observer.values()
        ]

        self.repo.persist_world_snapshot(world_snapshot)
        self.repo.persist_map_snapshot(map_snapshot)
        for view in runtime.cognition_by_observer.values():
            self.repo.upsert_cognition_overlay(
                self.repo.overlay_from_view(
                    view,
                    project_id=runtime.project_id,
                    as_of_chapter=as_of_chapter,
                    as_of_story_time=as_of_story_time,
                )
            )
        for snapshot in cognition_snapshots:
            self.repo.persist_cognition_snapshot(snapshot)
        self.session.flush()
        return world_snapshot, map_snapshot, cognition_snapshots


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "diff_against_objective"):
        return value.diff_against_objective()
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _digest(value: Any) -> str:
    raw = json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _summary_from_runtime(runtime: BookStateRuntime) -> str:
    return (
        f"nodes={len(runtime.world.nodes_by_id)} "
        f"edges={len(runtime.world.edges_by_id)} "
        f"facts={len(runtime.world.facts_by_id)}"
    )


def _reader_summary_from_runtime(runtime: BookStateRuntime) -> str:
    reader = runtime.cognition_by_observer.get(("reader", "reader"))
    if reader is None:
        return "reader_overlay=missing"
    return (
        f"visible={len(reader.visible_refs)} "
        f"suspected={len(reader.suspected_refs)} "
        f"confirmed={len(reader.confirmed_refs)}"
    )


def _apply_payload_patch(payload: dict[str, Any], field_path: str, op: str, value: Any) -> None:
    if not field_path:
        if isinstance(value, dict) and op in {"set", "replace", "merge"}:
            payload.update(value)
        return
    parts = [part for part in field_path.split(".") if part]
    cursor = payload
    for part in parts[:-1]:
        nested = cursor.get(part)
        if not isinstance(nested, dict):
            nested = {}
            cursor[part] = nested
        cursor = nested
    key = parts[-1]
    if op == "append":
        current = cursor.setdefault(key, [])
        if isinstance(current, list):
            current.append(value)
        else:
            cursor[key] = [current, value]
    elif op == "remove":
        current = cursor.get(key)
        if isinstance(current, list):
            cursor[key] = [item for item in current if item != value]
        else:
            cursor.pop(key, None)
    elif op == "merge" and isinstance(value, dict):
        current = cursor.get(key)
        if not isinstance(current, dict):
            current = {}
        current.update(value)
        cursor[key] = current
    else:
        cursor[key] = value


__all__ = ["BookStateProjection"]
