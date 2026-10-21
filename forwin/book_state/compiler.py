from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from forwin.book_state.cognition import CognitionView
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.repository import BookStateRepository, _as_jsonable, _world_edge_as_of
from forwin.book_state.runtime import INACTIVE_SPANS_KEY, BookStateRuntime
from forwin.book_state.schema import validate_graph_delta
from forwin.protocol.book_state import (
    ApprovedGraphDeltaSet,
    BookStateCompileResult,
    CognitionPatch,
    EdgePatch,
    FactPatch,
    GraphDelta,
    GraphDeltaType,
    MapPatch,
    NodePatch,
    WorldEdge,
    WorldNode,
)


class BookStateCompiler:
    """Append GraphDelta rows and materialize BookState snapshots."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = BookStateRepository(session)
        self.projection = BookStateProjection(session)

    def compile(
        self,
        approved_changes: ApprovedGraphDeltaSet,
        *,
        compiler_run_id: str = "",
        base_chapter: int | None = None,
    ) -> BookStateCompileResult:
        """Compile approved deltas into their chapter.

        ``base_chapter`` defaults to ``BookStateRepository.compile_base_chapter``:
        a chapter that already holds deltas is extended from its own canon.
        """
        requested_delta_ids = [delta.id for delta in approved_changes.graph_deltas]
        existing_delta_projects = self.repo.graph_delta_projects(requested_delta_ids)
        cross_project_delta_ids = sorted(
            delta_id
            for delta_id, project_id in existing_delta_projects.items()
            if project_id != approved_changes.project_id
        )
        if cross_project_delta_ids:
            return BookStateCompileResult(
                project_id=approved_changes.project_id,
                chapter_number=approved_changes.chapter_number,
                compiler_run_id=compiler_run_id
                or f"book_state_compile_blocked_{approved_changes.project_id}_{approved_changes.chapter_number}",
                committed=False,
                graph_delta_ids=cross_project_delta_ids,
                blocked_reasons=[
                    "graph_delta ids belong to another project: "
                    f"{cross_project_delta_ids}"
                ],
                forced_accept_reason=approved_changes.forced_accept_reason,
            )
        existing_delta_ids = set(existing_delta_projects)
        if existing_delta_ids:
            if existing_delta_ids == set(requested_delta_ids):
                world_snapshot = self.repo.latest_world_snapshot(
                    approved_changes.project_id,
                    approved_changes.chapter_number,
                )
                map_snapshot = self.repo.latest_map_snapshot(
                    approved_changes.project_id,
                    approved_changes.chapter_number,
                )
                return BookStateCompileResult(
                    project_id=approved_changes.project_id,
                    chapter_number=approved_changes.chapter_number,
                    compiler_run_id=compiler_run_id
                    or f"book_state_compile_idempotent_{approved_changes.project_id}_{approved_changes.chapter_number}",
                    committed=True,
                    graph_delta_ids=requested_delta_ids,
                    world_snapshot_id=world_snapshot.id if world_snapshot else "",
                    map_snapshot_id=map_snapshot.id if map_snapshot else "",
                    forced_accept_reason=approved_changes.forced_accept_reason,
                    metadata={"idempotent": True},
                )
            return BookStateCompileResult(
                project_id=approved_changes.project_id,
                chapter_number=approved_changes.chapter_number,
                compiler_run_id=compiler_run_id
                or f"book_state_compile_blocked_{approved_changes.project_id}_{approved_changes.chapter_number}",
                committed=False,
                graph_delta_ids=sorted(existing_delta_ids),
                blocked_reasons=[f"partial duplicate graph_delta ids: {sorted(existing_delta_ids)}"],
                forced_accept_reason=approved_changes.forced_accept_reason,
            )
        if base_chapter is None:
            base_chapter = self.repo.compile_base_chapter(
                approved_changes.project_id, approved_changes.chapter_number
            )
        runtime = self.projection.load_runtime_as_of(
            approved_changes.project_id,
            as_of_chapter=base_chapter,
        )
        extended = (
            self.repo.latest_world_snapshot(approved_changes.project_id, base_chapter)
            if base_chapter == approved_changes.chapter_number
            else None
        )
        if extended is not None and extended.as_of_chapter != base_chapter:
            extended = None
        deltas = [
            delta.model_copy(
                update={
                    "project_id": approved_changes.project_id,
                    "chapter_number": approved_changes.chapter_number,
                }
            )
            for delta in approved_changes.graph_deltas
        ]

        blocked_reasons: list[str] = []
        for delta in deltas:
            blocked_reasons.extend(
                self._validate_delta(
                    runtime,
                    delta,
                    forced_accept_reason=approved_changes.forced_accept_reason,
                )
            )
            if blocked_reasons:
                return BookStateCompileResult(
                    project_id=approved_changes.project_id,
                    chapter_number=approved_changes.chapter_number,
                    compiler_run_id=compiler_run_id
                    or f"book_state_compile_blocked_{approved_changes.project_id}_{approved_changes.chapter_number}",
                    committed=False,
                    blocked_reasons=blocked_reasons,
                    forced_accept_reason=approved_changes.forced_accept_reason,
                )
            self.projection.apply_delta_to_runtime(runtime, delta)

        for delta in deltas:
            self.repo.append_graph_delta(delta)
            self._persist_delta_side_effects(runtime, delta)

        # Extending a chapter supersedes its snapshot, so carry that snapshot's
        # provenance forward as historical rewrite does for retained edits.
        story_time = next(
            (delta.story_time for delta in reversed(deltas) if delta.story_time),
            extended.as_of_story_time if extended else "",
        )
        world_snapshot, map_snapshot, cognition_snapshots = self.projection.persist_snapshots(
            runtime,
            as_of_chapter=approved_changes.chapter_number,
            as_of_story_time=story_time,
            source_delta_ids=[
                *(extended.source_delta_ids if extended else []),
                *(delta.id for delta in deltas),
            ],
            active_world_line_ids=_unique(
                [
                    *(extended.active_world_line_ids if extended else []),
                    *(delta.world_line_id for delta in deltas if delta.world_line_id),
                ]
            ),
        )
        self.session.flush()
        return BookStateCompileResult(
            project_id=approved_changes.project_id,
            chapter_number=approved_changes.chapter_number,
            compiler_run_id=compiler_run_id
            or f"book_state_compile_{approved_changes.project_id}_{approved_changes.chapter_number}",
            committed=True,
            graph_delta_ids=[delta.id for delta in deltas],
            world_snapshot_id=world_snapshot.id,
            map_snapshot_id=map_snapshot.id,
            cognition_snapshot_ids=[snapshot.id for snapshot in cognition_snapshots],
            forced_accept_reason=approved_changes.forced_accept_reason,
        )

    def _validate_delta(
        self,
        runtime: BookStateRuntime,
        delta: GraphDelta,
        *,
        forced_accept_reason: str = "",
    ) -> list[str]:
        blocked: list[str] = []
        schema_report = validate_graph_delta(delta)
        blocked.extend(
            f"{delta.id}:{issue.code} {issue.target}: {issue.message}"
            for issue in schema_report.errors
        )
        is_repair = str(delta.delta_type) in {
            GraphDeltaType.REPAIR.value,
            GraphDeltaType.RETCON_BLOCK.value,
            "repair",
            "retcon_block",
        }
        for label, patch, current in self._iter_patch_current_values(runtime, delta):
            old_value = getattr(patch, "old_value", None)
            if old_value is None:
                continue
            if _json_equal(old_value, current):
                continue
            if is_repair and forced_accept_reason:
                continue
            blocked.append(
                f"{delta.id}:{label} old_value mismatch; expected {old_value!r}, current {current!r}"
            )
        return blocked

    def _iter_patch_current_values(
        self,
        runtime: BookStateRuntime,
        delta: GraphDelta,
    ) -> list[tuple[str, object, Any]]:
        values: list[tuple[str, object, Any]] = []
        for patch in delta.node_patches:
            values.append((f"node:{patch.node_id}:{patch.field_path}", patch, _node_current(runtime, patch)))
        for patch in delta.edge_patches:
            values.append((f"edge:{patch.edge_id}:{patch.field_path}", patch, _edge_current(runtime, patch)))
        for patch in delta.fact_patches:
            values.append((f"fact:{patch.fact_id}", patch, _fact_current(runtime, patch)))
        for patch in delta.map_patches:
            values.append((f"{patch.target_type}:{patch.target_id}:{patch.field_path}", patch, _map_current(runtime, patch)))
        for patch in delta.cognition_patches:
            key = (str(patch.observer_type), patch.observer_id)
            view = runtime.cognition_by_observer.get(key)
            if patch.field_path == "knowledge_by_ref":
                current = _knowledge_current(view, patch)
            elif view is None:
                current = None
            elif patch.field_path in {"visible_refs", "hidden_refs", "suspected_refs", "confirmed_refs"}:
                current = sorted(getattr(view, patch.field_path))
            else:
                current = getattr(view, patch.field_path, None)
            values.append((f"cognition:{key[0]}:{key[1]}:{patch.field_path}", patch, current))
        for patch in delta.narrative_patches:
            current = _narrative_current(runtime, patch)
            values.append((f"narrative:{patch.target_ref}:{patch.field_path}", patch, current))
        return values

    def _persist_delta_side_effects(self, runtime: BookStateRuntime, delta: GraphDelta) -> None:
        versioned_node_ids: dict[str, None] = {}
        for patch in delta.node_patches:
            node = runtime.world.nodes_by_id.get(patch.node_id)
            if node is None:
                continue
            versioned_node_ids[patch.node_id] = None
            if str(patch.op) == "create" and str(node.node_type) == "character":
                profile = dict(node.profile) if isinstance(node.profile, dict) else {}
                if not profile.get("personality_loadout"):
                    from forwin.characters.creation import CharacterCreationHelper
                    from forwin.characters.models import CharacterCreationRequest

                    genesis_ref = str(node.metadata.get("genesis_ref") or "")
                    result = CharacterCreationHelper(self.session).apply_book_state_character_patch(
                        CharacterCreationRequest(
                            project_id=delta.project_id,
                            source="book_state_graph_delta",
                            source_ref=delta.id,
                            character_id=patch.node_id,
                            name=node.name or patch.node_id,
                            aliases=list(node.aliases),
                            summary=node.summary,
                            description=node.description,
                            importance=node.importance,
                            created_at_chapter=delta.chapter_number,
                            profile=profile,
                            state=runtime.world.get_state(patch.node_id),
                            creation_context={"genesis_ref_id": genesis_ref} if genesis_ref else {},
                            existing_resolution="create_new",
                            audit_reason=patch.reason or "BookState GraphDelta create character",
                        )
                    )
                    if result.world_node:
                        created = WorldNode.model_validate(result.world_node)
                        # The helper writes its own metadata; keep the patch's metadata under it.
                        runtime.world.nodes_by_id[patch.node_id] = created.model_copy(
                            update={"metadata": {**node.metadata, **created.metadata}}
                        )
        for node_id in versioned_node_ids:
            self.repo.create_world_node(
                runtime.world.nodes_by_id[node_id],
                version_chapter=delta.chapter_number,
                version_state=runtime.world.get_state(node_id),
                as_of_story_time=delta.story_time,
                source_delta_id=delta.id,
            )
        for patch in delta.edge_patches:
            edge = runtime.world.edges_by_id.get(patch.edge_id)
            if edge is None:
                continue
            stored = self.repo.get_world_edge(patch.edge_id)
            stored_then = (
                _world_edge_as_of(stored, delta.chapter_number) if stored is not None else None
            )
            if stored_then is not None and (edge.is_active, edge.ended_at_chapter) == (
                stored_then.is_active,
                stored_then.ended_at_chapter,
            ):
                # This delta left the edge's activity alone; keep the row's later
                # end or reactivation instead of writing back the as-of view.
                edge = edge.model_copy(
                    update={
                        "is_active": stored.is_active,
                        "ended_at_chapter": stored.ended_at_chapter,
                        "metadata": _with_inactive_spans(edge.metadata, stored),
                    }
                )
            self.repo.create_world_edge(edge)
        for patch in delta.fact_patches:
            if patch.fact_id in runtime.world.facts_by_id:
                self.repo.create_fact_node(runtime.world.facts_by_id[patch.fact_id])
        for patch in delta.map_patches:
            if patch.target_type == "map_node" and patch.target_id in runtime.map.nodes_by_id:
                node = runtime.map.nodes_by_id[patch.target_id]
                if "created_at_chapter" not in node.metadata:
                    node = node.model_copy(
                        update={"metadata": {**node.metadata, "created_at_chapter": delta.chapter_number}}
                    )
                self.repo.create_map_node(node)
            elif patch.target_type == "map_edge" and patch.target_id in runtime.map.edges_by_id:
                edge = runtime.map.edges_by_id[patch.target_id]
                if "created_at_chapter" not in edge.metadata:
                    edge = edge.model_copy(
                        update={"metadata": {**edge.metadata, "created_at_chapter": delta.chapter_number}}
                    )
                self.repo.create_map_edge(edge)
        for patch in delta.narrative_patches:
            if str(patch.op) != "create":
                continue
            target_kind, target_id = _split_target_ref(patch.target_ref)
            if target_kind == "narrative_edge" and target_id in runtime.narrative.edges_by_id:
                edge = runtime.narrative.edges_by_id[target_id]
                if "created_at_chapter" not in edge.metadata:
                    edge = edge.model_copy(
                        update={"metadata": {**edge.metadata, "created_at_chapter": delta.chapter_number}}
                    )
                self.repo.create_narrative_edge(edge)
            elif target_id in runtime.narrative.nodes_by_id:
                node = runtime.narrative.nodes_by_id[target_id]
                if "created_at_chapter" not in node.metadata:
                    node = node.model_copy(
                        update={"metadata": {**node.metadata, "created_at_chapter": delta.chapter_number}}
                )
                self.repo.create_narrative_node(node)
        persisted_cognition_keys: set[tuple[str, str]] = set()
        for patch in delta.cognition_patches:
            key = (str(patch.observer_type), patch.observer_id)
            if key in persisted_cognition_keys:
                continue
            view = runtime.cognition_by_observer.get(key)
            if view is None:
                continue
            self.repo.upsert_cognition_overlay(
                self.repo.overlay_from_view(
                    view,
                    project_id=delta.project_id,
                    as_of_chapter=delta.chapter_number,
                    as_of_story_time=delta.story_time,
                )
            )
            persisted_cognition_keys.add(key)


def _node_current(runtime: BookStateRuntime, patch: NodePatch) -> Any:
    if str(patch.op) == "create":
        return None
    node = runtime.world.nodes_by_id.get(patch.node_id)
    if node is None:
        return None
    if patch.field_path.startswith("state."):
        return _get_path(runtime.world.get_state(patch.node_id), patch.field_path.removeprefix("state."))
    return _get_path(node.model_dump(mode="json"), patch.field_path)


def _edge_current(runtime: BookStateRuntime, patch: EdgePatch) -> Any:
    if str(patch.op) == "create":
        return None
    edge = runtime.world.edges_by_id.get(patch.edge_id)
    if edge is None:
        return None
    return _get_path(edge.model_dump(mode="json"), patch.field_path)


def _knowledge_current(view: CognitionView | None, patch: CognitionPatch) -> dict[str, str | None]:
    """Current state of each ref a merge's ``old_value`` names; the whole map otherwise."""
    known = view.knowledge_by_ref if view is not None else {}
    if str(patch.op) == "merge" and isinstance(patch.old_value, dict):
        return {ref: known.get(ref) for ref in patch.old_value}
    return dict(known)


def _fact_current(runtime: BookStateRuntime, patch: FactPatch) -> Any:
    if str(patch.op) == "create":
        return None
    fact = runtime.world.facts_by_id.get(patch.fact_id)
    if fact is None:
        return None
    payload = fact.model_dump(mode="json")
    if patch.field_path:
        return _get_path(payload, patch.field_path)
    return payload


def _map_current(runtime: BookStateRuntime, patch: MapPatch) -> Any:
    if str(patch.op) == "create":
        return None
    if patch.target_type == "map_node":
        target = runtime.map.nodes_by_id.get(patch.target_id)
    elif patch.target_type == "map_edge":
        target = runtime.map.edges_by_id.get(patch.target_id)
    else:
        target = None
    if target is None:
        return None
    return _get_path(target.model_dump(mode="json"), patch.field_path)


def _narrative_current(runtime: BookStateRuntime, patch: Any) -> Any:
    if str(patch.op) == "create":
        return None
    target_kind, target_id = _split_target_ref(str(patch.target_ref))
    if target_kind == "narrative_edge":
        target = runtime.narrative.edges_by_id.get(target_id)
    else:
        target = runtime.narrative.nodes_by_id.get(target_id)
    if target is None:
        return None
    return _get_path(target.model_dump(mode="json"), patch.field_path)


def _split_target_ref(target_ref: str) -> tuple[str, str]:
    if ":" not in target_ref:
        return "narrative_node", target_ref
    return tuple(target_ref.split(":", 1))  # type: ignore[return-value]


def _get_path(payload: dict[str, Any], field_path: str) -> Any:
    if not field_path:
        return payload
    cursor: Any = payload
    for part in [part for part in field_path.split(".") if part]:
        if not isinstance(cursor, dict) or part not in cursor:
            return None
        cursor = cursor[part]
    return cursor


def _with_inactive_spans(metadata: dict[str, Any], stored: WorldEdge) -> dict[str, Any]:
    result = {key: value for key, value in metadata.items() if key != INACTIVE_SPANS_KEY}
    if INACTIVE_SPANS_KEY in stored.metadata:
        result[INACTIVE_SPANS_KEY] = stored.metadata[INACTIVE_SPANS_KEY]
    return result


def _json_equal(left: Any, right: Any) -> bool:
    return _as_jsonable(left) == _as_jsonable(right)


def _unique(values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value)
        if text and text not in result:
            result.append(text)
    return result


__all__ = ["BookStateCompiler"]
