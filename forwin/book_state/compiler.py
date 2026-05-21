from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from forwin.book_state.projection import BookStateProjection
from forwin.book_state.repository import BookStateRepository, _as_jsonable
from forwin.book_state.runtime import BookStateRuntime
from forwin.book_state.schema import validate_graph_delta
from forwin.protocol.book_state import (
    ApprovedGraphDeltaSet,
    BookStateCompileResult,
    EdgePatch,
    FactPatch,
    GraphDelta,
    GraphDeltaType,
    MapPatch,
    NodePatch,
    ReaderExperienceDeltaRecord,
    ReaderPromise,
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
    ) -> BookStateCompileResult:
        requested_delta_ids = [delta.id for delta in approved_changes.graph_deltas]
        existing_delta_ids = self.repo.graph_delta_ids_exist(requested_delta_ids)
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
        base_chapter = max(int(approved_changes.chapter_number or 0) - 1, 0)
        runtime = self.projection.load_runtime_as_of(
            approved_changes.project_id,
            as_of_chapter=base_chapter,
        )
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

        story_time = next((delta.story_time for delta in reversed(deltas) if delta.story_time), "")
        world_snapshot, map_snapshot, cognition_snapshots = self.projection.persist_snapshots(
            runtime,
            as_of_chapter=approved_changes.chapter_number,
            as_of_story_time=story_time,
            source_delta_ids=[delta.id for delta in deltas],
            active_world_line_ids=_unique(delta.world_line_id for delta in deltas if delta.world_line_id),
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
        for label, old_value, current in self._iter_reader_promise_patch_current_values(runtime, delta):
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
            if view is None:
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

    def _iter_reader_promise_patch_current_values(
        self,
        runtime: BookStateRuntime,
        delta: GraphDelta,
    ) -> list[tuple[str, Any, Any]]:
        values: list[tuple[str, Any, Any]] = []
        promises = {
            promise.promise_id: promise.model_dump(mode="json")
            for promise in self.repo.list_reader_promises_native(
                delta.project_id,
                as_of_chapter=runtime.as_of_chapter,
            )
        }
        for patch in _reader_promise_patches(delta):
            promise_id = str(patch.get("promise_id") or "")
            op = str(patch.get("op") or "")
            field_path = str(patch.get("field_path") or "")
            payload = promises.get(promise_id)
            current = None if op == "create" else _get_path(payload or {}, field_path)
            values.append((f"reader_promise:{promise_id}:{field_path or op}", patch.get("old_value"), current))
            if op == "create" and isinstance(patch.get("new_value"), dict):
                promises[promise_id] = ReaderPromise.model_validate(
                    {
                        **patch["new_value"],
                        "project_id": delta.project_id,
                        "promise_id": promise_id,
                    }
                ).model_dump(mode="json")
            elif payload is not None:
                _set_path(payload, field_path, patch.get("new_value"))
        return values

    def _persist_delta_side_effects(self, runtime: BookStateRuntime, delta: GraphDelta) -> None:
        for patch in delta.node_patches:
            node = runtime.world.nodes_by_id.get(patch.node_id)
            if node is None:
                continue
            if str(patch.op) == "create" and str(node.node_type) == "character":
                profile = dict(node.profile) if isinstance(node.profile, dict) else {}
                if not profile.get("personality_loadout"):
                    from forwin.characters.creation import CharacterCreationHelper
                    from forwin.characters.models import CharacterCreationRequest

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
                            audit_reason=patch.reason or "BookState GraphDelta create character",
                        )
                    )
                    if result.world_node:
                        node = WorldNode.model_validate(result.world_node)
                        runtime.world.nodes_by_id[patch.node_id] = node
            self.repo.create_world_node(node)
            if str(patch.op) == "create" or patch.field_path.startswith("state."):
                self.repo.append_world_node_state(
                    project_id=delta.project_id,
                    node_id=patch.node_id,
                    node_type=str(node.node_type),
                    as_of_chapter=delta.chapter_number,
                    as_of_story_time=delta.story_time,
                    state=runtime.world.get_state(patch.node_id),
                    source_delta_id=delta.id,
                )
        for patch in delta.edge_patches:
            if patch.edge_id in runtime.world.edges_by_id:
                self.repo.create_world_edge(runtime.world.edges_by_id[patch.edge_id])
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
        self._persist_reader_promise_patch_side_effects(delta)
        self._persist_reader_experience_side_effects(delta)

    def _persist_reader_promise_patch_side_effects(self, delta: GraphDelta) -> None:
        patches = _reader_promise_patches(delta)
        if not patches:
            return
        promises = {
            promise.promise_id: promise.model_dump(mode="json")
            for promise in self.repo.list_reader_promises_native(delta.project_id)
        }
        for patch in patches:
            promise_id = str(patch.get("promise_id") or "")
            op = str(patch.get("op") or "")
            if not promise_id:
                continue
            if op == "create":
                new_value = patch.get("new_value")
                if not isinstance(new_value, dict):
                    continue
                payload = {
                    **new_value,
                    "project_id": delta.project_id,
                    "promise_id": promise_id,
                }
            else:
                payload = dict(promises.get(promise_id, {}))
                if not payload:
                    continue
                _set_path(payload, str(patch.get("field_path") or ""), patch.get("new_value"))
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            evidence_refs = list(payload.get("source_refs", []))
            for ref in patch.get("evidence_refs") or []:
                if ref not in evidence_refs:
                    evidence_refs.append(ref)
            payload["source_refs"] = evidence_refs
            payload["metadata"] = {
                **metadata,
                "source_delta_id": delta.id,
                "reader_promise_patch_reason": str(patch.get("reason") or ""),
            }
            promise = ReaderPromise.model_validate(payload)
            self.repo.upsert_reader_promise(promise)
            promises[promise_id] = promise.model_dump(mode="json")

    def _persist_reader_experience_side_effects(self, delta: GraphDelta) -> None:
        payload = delta.metadata.get("reader_experience_delta") if isinstance(delta.metadata, dict) else None
        if not isinstance(payload, dict):
            return
        reader_delta = ReaderExperienceDeltaRecord.model_validate(
            {
                **payload,
                "project_id": delta.project_id,
                "chapter_number": int(payload.get("chapter_number") or delta.chapter_number or 0),
            }
        )
        self.repo.upsert_reader_experience_delta(reader_delta)
        promise_id = str(payload.get("promise_id") or f"promise:{reader_delta.reader_experience_delta_id}")
        self.repo.upsert_reader_promise(
            ReaderPromise(
                promise_id=promise_id,
                project_id=delta.project_id,
                promise_type=reader_delta.payoff_type or "reader_experience",
                summary=reader_delta.reader_state_after or reader_delta.next_desire,
                created_at_chapter=reader_delta.chapter_number,
                current_debt_level=max(int(reader_delta.promise_debt_change or 0), 0),
                reward_tags=list(reader_delta.reward_tags),
                linked_threads=list(reader_delta.metadata.get("linked_threads", []))
                if isinstance(reader_delta.metadata, dict)
                else [],
                linked_knowledge_gaps=list(reader_delta.metadata.get("linked_knowledge_gaps", []))
                if isinstance(reader_delta.metadata, dict)
                else [],
                status="open" if int(reader_delta.promise_debt_change or 0) > 0 else "resolved",
                source_refs=list(reader_delta.source_refs),
                metadata={
                    "source_delta_id": delta.id,
                    "reader_experience_delta_id": reader_delta.reader_experience_delta_id,
                    **(reader_delta.metadata if isinstance(reader_delta.metadata, dict) else {}),
                },
            )
        )


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


def _reader_promise_patches(delta: GraphDelta) -> list[dict[str, Any]]:
    payload = delta.metadata.get("reader_promise_patches") if isinstance(delta.metadata, dict) else None
    if not isinstance(payload, list):
        return []
    return [dict(item) for item in payload if isinstance(item, dict)]


def _get_path(payload: dict[str, Any], field_path: str) -> Any:
    if not field_path:
        return payload
    cursor: Any = payload
    for part in [part for part in field_path.split(".") if part]:
        if not isinstance(cursor, dict) or part not in cursor:
            return None
        cursor = cursor[part]
    return cursor


def _set_path(payload: dict[str, Any], field_path: str, value: Any) -> None:
    if not field_path:
        return
    cursor: Any = payload
    parts = [part for part in field_path.split(".") if part]
    for part in parts[:-1]:
        nested = cursor.get(part) if isinstance(cursor, dict) else None
        if not isinstance(nested, dict):
            nested = {}
            cursor[part] = nested
        cursor = nested
    if parts and isinstance(cursor, dict):
        cursor[parts[-1]] = value


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
