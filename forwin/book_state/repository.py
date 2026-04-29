from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forwin.book_state.cognition import CognitionView
from forwin.book_state.map_graph import MapGraph
from forwin.book_state.narrative import NarrativeControlGraph
from forwin.book_state.runtime import ObjectiveWorldGraph
from forwin.models.book_state import (
    BookCognitionSnapshotRow,
    BookReaderExperienceDeltaRow,
    BookReaderPromiseRow,
    CognitionOverlayPatchRow,
    CognitionOverlayRow,
    FactNodeRow,
    GraphDeltaPatchRow,
    GraphDeltaRow,
    MapEdgeRow,
    MapNodeRow,
    MapSnapshotRow,
    NarrativeEdgeRow,
    NarrativeNodeRow,
    WorldEdgeRow,
    WorldNodeRow,
    WorldNodeStateRow,
    WorldSnapshotRow,
)
from forwin.protocol.book_state import (
    CognitionOverlay,
    CognitionPatch,
    CognitionSnapshot,
    EdgePatch,
    FactNode,
    FactPatch,
    GraphDelta,
    MapEdge,
    MapNode,
    MapPatch,
    MapSnapshot,
    NarrativeEdge,
    NarrativeNode,
    NarrativePatch,
    NodePatch,
    ReaderExperienceDeltaRecord,
    ReaderPromise,
    WorldEdge,
    WorldNode,
    WorldSnapshot,
)


def _loads(value: str | None, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _dump(value: Any) -> str:
    return json.dumps(_as_jsonable(value), ensure_ascii=False)


def _as_jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _as_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_as_jsonable(item) for item in value]
    return value


class BookStateRepository:
    """Persistence boundary for the final BookState graph tables."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Core graph rows
    # ------------------------------------------------------------------

    def create_world_node(self, node: WorldNode) -> WorldNodeRow:
        row = self.session.get(WorldNodeRow, node.id)
        if row is None:
            row = WorldNodeRow(id=node.id, project_id=node.project_id, node_type=str(node.node_type))
            self.session.add(row)
        row.name = node.name
        row.aliases_json = _dump(node.aliases)
        row.summary = node.summary
        row.description = node.description
        row.status = node.status
        row.importance = int(node.importance)
        row.scope = node.scope
        row.tags_json = _dump(node.tags)
        row.created_at_chapter = int(node.created_at_chapter or 0)
        row.retired_at_chapter = node.retired_at_chapter
        row.valid_from_chapter = int(node.valid_from_chapter or node.created_at_chapter or 0)
        row.valid_until_chapter = node.valid_until_chapter
        row.source_refs_json = _dump(node.source_refs)
        row.is_active = bool(node.is_active)
        row.profile_json = _dump(node.profile)
        metadata = dict(node.metadata)
        row.created_at_chapter = _created_at_chapter(metadata, default=int(node.created_at_chapter or 0))
        metadata.setdefault("created_at_chapter", row.created_at_chapter)
        row.metadata_json = _dump(metadata)
        self.session.flush()
        return row

    def get_world_node(self, node_id: str) -> WorldNode | None:
        row = self.session.get(WorldNodeRow, node_id)
        return _world_node_from_row(row) if row is not None else None

    def append_world_node_state(
        self,
        *,
        project_id: str,
        node_id: str,
        node_type: str,
        as_of_chapter: int,
        state: dict[str, Any],
        as_of_story_time: str = "",
        source_delta_id: str = "",
    ) -> WorldNodeStateRow:
        row = WorldNodeStateRow(
            project_id=project_id,
            node_id=node_id,
            node_type=node_type,
            as_of_chapter=int(as_of_chapter or 0),
            as_of_story_time=as_of_story_time,
            state_json=_dump(state),
            source_delta_id=source_delta_id,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def create_world_edge(self, edge: WorldEdge) -> WorldEdgeRow:
        row = self.session.get(WorldEdgeRow, edge.id)
        if row is None:
            row = WorldEdgeRow(id=edge.id, project_id=edge.project_id)
            self.session.add(row)
        row.source_id = edge.source_id
        row.target_id = edge.target_id
        row.edge_type = edge.edge_type
        row.edge_family = str(edge.edge_family)
        row.directionality = str(edge.directionality)
        row.weight = float(edge.weight)
        row.confidence = float(edge.confidence)
        row.established_at_chapter = int(edge.established_at_chapter or 0)
        row.ended_at_chapter = edge.ended_at_chapter
        row.valid_from_chapter = int(edge.valid_from_chapter or edge.established_at_chapter or 0)
        row.valid_until_chapter = edge.valid_until_chapter
        row.is_active = bool(edge.is_active)
        row.status = edge.status
        row.visibility = edge.visibility
        row.truth_relation = edge.truth_relation
        row.visibility_default = edge.visibility_default
        row.source_refs_json = _dump(edge.source_refs)
        row.state_json = _dump(edge.state)
        row.evidence_refs_json = _dump(edge.evidence_refs)
        metadata = dict(edge.metadata)
        metadata.setdefault("created_at_chapter", int(edge.established_at_chapter or 0))
        row.created_at_chapter = _created_at_chapter(metadata)
        metadata.setdefault("created_at_chapter", row.created_at_chapter)
        row.metadata_json = _dump(metadata)
        self.session.flush()
        return row

    def create_fact_node(self, fact: FactNode) -> FactNodeRow:
        row = self.session.get(FactNodeRow, fact.id)
        if row is None:
            row = FactNodeRow(id=fact.id, project_id=fact.project_id, proposition=fact.proposition)
            self.session.add(row)
        row.proposition = fact.proposition
        row.fact_type = fact.fact_type
        row.truth_value = fact.truth_value
        row.confidence = float(fact.confidence)
        row.related_node_refs_json = _dump(fact.related_node_refs)
        row.related_edge_refs_json = _dump(fact.related_edge_refs)
        row.source_refs_json = _dump(fact.source_refs)
        row.created_at_chapter = int(fact.created_at_chapter or 0)
        row.happened_at_story_time = fact.happened_at_story_time
        row.contradiction_refs_json = _dump(fact.contradiction_refs)
        row.sensitivity_level = fact.sensitivity_level
        row.narrative_function = fact.narrative_function
        row.state_json = _dump(fact.state)
        row.metadata_json = _dump(fact.metadata)
        self.session.flush()
        return row

    def list_world_nodes(self, project_id: str, *, as_of_chapter: int | None = None) -> list[WorldNode]:
        stmt = select(WorldNodeRow).where(WorldNodeRow.project_id == project_id)
        if as_of_chapter is not None:
            stmt = stmt.where(WorldNodeRow.created_at_chapter <= int(as_of_chapter))
        rows = list(
            self.session.execute(
                stmt.order_by(WorldNodeRow.created_at_chapter.asc(), WorldNodeRow.id.asc())
            )
            .scalars()
            .all()
        )
        if as_of_chapter is None:
            return [_world_node_from_row(row) for row in rows]
        return [
            _world_node_from_row(row)
            for row in rows
            if row.retired_at_chapter is None or row.retired_at_chapter > int(as_of_chapter)
        ]

    def list_world_edges(self, project_id: str, *, as_of_chapter: int | None = None) -> list[WorldEdge]:
        stmt = select(WorldEdgeRow).where(WorldEdgeRow.project_id == project_id)
        if as_of_chapter is not None:
            stmt = stmt.where(WorldEdgeRow.established_at_chapter <= int(as_of_chapter))
        rows = list(
            self.session.execute(
                stmt.order_by(WorldEdgeRow.established_at_chapter.asc(), WorldEdgeRow.id.asc())
            )
            .scalars()
            .all()
        )
        if as_of_chapter is None:
            return [_world_edge_from_row(row) for row in rows]
        return [
            _world_edge_from_row(row)
            for row in rows
            if row.ended_at_chapter is None or row.ended_at_chapter > int(as_of_chapter)
        ]

    def list_fact_nodes(self, project_id: str, *, as_of_chapter: int | None = None) -> list[FactNode]:
        stmt = select(FactNodeRow).where(FactNodeRow.project_id == project_id)
        if as_of_chapter is not None:
            stmt = stmt.where(FactNodeRow.created_at_chapter <= int(as_of_chapter))
        return [
            _fact_node_from_row(row)
            for row in self.session.execute(
                stmt.order_by(FactNodeRow.created_at_chapter.asc(), FactNodeRow.id.asc())
            )
            .scalars()
            .all()
        ]

    def list_map_nodes(self, project_id: str) -> list[MapNode]:
        return [
            _map_node_from_row(row)
            for row in self.session.execute(
                select(MapNodeRow).where(MapNodeRow.project_id == project_id).order_by(MapNodeRow.id.asc())
            )
            .scalars()
            .all()
        ]

    def list_map_edges(self, project_id: str) -> list[MapEdge]:
        return [
            _map_edge_from_row(row)
            for row in self.session.execute(
                select(MapEdgeRow).where(MapEdgeRow.project_id == project_id).order_by(MapEdgeRow.id.asc())
            )
            .scalars()
            .all()
        ]

    def list_cognition_overlays(self, project_id: str, *, as_of_chapter: int | None = None) -> list[CognitionOverlay]:
        stmt = select(CognitionOverlayRow).where(CognitionOverlayRow.project_id == project_id)
        if as_of_chapter is not None:
            stmt = stmt.where(CognitionOverlayRow.as_of_chapter <= int(as_of_chapter))
        rows = list(
            self.session.execute(
                stmt.order_by(
                    CognitionOverlayRow.observer_type.asc(),
                    CognitionOverlayRow.observer_id.asc(),
                    CognitionOverlayRow.as_of_chapter.desc(),
                    CognitionOverlayRow.id.asc(),
                )
            )
            .scalars()
            .all()
        )
        latest: dict[tuple[str, str], CognitionOverlay] = {}
        for row in rows:
            key = (row.observer_type, row.observer_id)
            if key not in latest:
                latest[key] = _cognition_overlay_from_row(row)
        return list(latest.values())

    def list_reader_promises(self, project_id: str, *, as_of_chapter: int | None = None) -> list[WorldNode]:
        return [
            node
            for node in self.list_world_nodes(project_id, as_of_chapter=as_of_chapter)
            if node.node_type == "reader_promise"
        ]

    def upsert_reader_promise(self, promise: ReaderPromise) -> BookReaderPromiseRow:
        row = self.session.execute(
            select(BookReaderPromiseRow).where(
                BookReaderPromiseRow.project_id == promise.project_id,
                BookReaderPromiseRow.promise_id == promise.promise_id,
            )
        ).scalar_one_or_none()
        if row is None:
            row = BookReaderPromiseRow(
                promise_id=promise.promise_id,
                project_id=promise.project_id,
            )
            self.session.add(row)
        row.promise_type = promise.promise_type
        row.summary = promise.summary
        row.created_at_chapter = int(promise.created_at_chapter or 0)
        row.expected_payoff_window = promise.expected_payoff_window
        row.maximum_safe_delay = int(promise.maximum_safe_delay or 0)
        row.current_debt_level = int(promise.current_debt_level or 0)
        row.reward_tags_json = _dump(promise.reward_tags)
        row.linked_threads_json = _dump(promise.linked_threads)
        row.linked_knowledge_gaps_json = _dump(promise.linked_knowledge_gaps)
        row.status = promise.status
        row.source_refs_json = _dump(promise.source_refs)
        row.metadata_json = _dump(promise.metadata)
        self.session.flush()
        return row

    def upsert_reader_experience_delta(
        self,
        reader_delta: ReaderExperienceDeltaRecord,
    ) -> BookReaderExperienceDeltaRow:
        row = self.session.execute(
            select(BookReaderExperienceDeltaRow).where(
                BookReaderExperienceDeltaRow.project_id == reader_delta.project_id,
                BookReaderExperienceDeltaRow.reader_experience_delta_id
                == reader_delta.reader_experience_delta_id
            )
        ).scalar_one_or_none()
        if row is None:
            row = BookReaderExperienceDeltaRow(
                reader_experience_delta_id=reader_delta.reader_experience_delta_id,
                project_id=reader_delta.project_id,
            )
            self.session.add(row)
        row.chapter_number = int(reader_delta.chapter_number or 0)
        row.reader_state_before = reader_delta.reader_state_before
        row.reader_state_after = reader_delta.reader_state_after
        row.cognition_transition = reader_delta.cognition_transition
        row.payoff_type = reader_delta.payoff_type
        row.reward_tags_json = _dump(reader_delta.reward_tags)
        row.emotional_effect = reader_delta.emotional_effect
        row.promise_debt_change = int(reader_delta.promise_debt_change or 0)
        row.next_desire = reader_delta.next_desire
        row.fairness_evidence_json = _dump(reader_delta.fairness_evidence)
        row.source_refs_json = _dump(reader_delta.source_refs)
        row.metadata_json = _dump(reader_delta.metadata)
        self.session.flush()
        return row

    def list_reader_promises_native(
        self,
        project_id: str,
        *,
        as_of_chapter: int | None = None,
    ) -> list[ReaderPromise]:
        stmt = select(BookReaderPromiseRow).where(BookReaderPromiseRow.project_id == project_id)
        if as_of_chapter is not None:
            stmt = stmt.where(BookReaderPromiseRow.created_at_chapter <= int(as_of_chapter))
        return [
            _reader_promise_from_row(row)
            for row in self.session.execute(
                stmt.order_by(BookReaderPromiseRow.created_at_chapter.asc(), BookReaderPromiseRow.promise_id.asc())
            )
            .scalars()
            .all()
        ]

    def list_reader_experience_deltas(
        self,
        project_id: str,
        *,
        through_chapter: int | None = None,
    ) -> list[ReaderExperienceDeltaRecord]:
        stmt = select(BookReaderExperienceDeltaRow).where(BookReaderExperienceDeltaRow.project_id == project_id)
        if through_chapter is not None:
            stmt = stmt.where(BookReaderExperienceDeltaRow.chapter_number <= int(through_chapter))
        return [
            _reader_experience_delta_from_row(row)
            for row in self.session.execute(
                stmt.order_by(
                    BookReaderExperienceDeltaRow.chapter_number.asc(),
                    BookReaderExperienceDeltaRow.reader_experience_delta_id.asc(),
                )
            )
            .scalars()
            .all()
        ]

    def graph_delta_ids_exist(self, delta_ids: list[str]) -> set[str]:
        if not delta_ids:
            return set()
        return {
            row
            for row in self.session.execute(
                select(GraphDeltaRow.id).where(GraphDeltaRow.id.in_(delta_ids))
            ).scalars()
        }

    def create_map_node(self, node: MapNode) -> MapNodeRow:
        row = self.session.get(MapNodeRow, node.id)
        if row is None:
            row = MapNodeRow(id=node.id, project_id=node.project_id, node_type=str(node.node_type))
            self.session.add(row)
        row.subworld_id = node.subworld_id
        row.region_id = node.region_id
        row.name = node.name
        row.aliases_json = _dump(node.aliases)
        row.description = node.description
        row.parent_id = node.parent_id
        row.hierarchy_path = node.hierarchy_path
        row.scale_level = node.scale_level
        row.coordinates_json = _dump(node.coordinates or {})
        row.shape_ref = node.shape_ref
        row.terrain = node.terrain
        row.climate = node.climate
        row.culture_tag = node.culture_tag
        row.default_danger_level = float(node.default_danger_level)
        row.access_level = node.access_level
        row.status = node.status
        metadata = dict(node.metadata)
        row.created_at_chapter = _created_at_chapter(metadata, default=int(getattr(row, "created_at_chapter", 0) or 0))
        metadata.setdefault("created_at_chapter", row.created_at_chapter)
        row.metadata_json = _dump(metadata)
        self.session.flush()
        return row

    def create_map_edge(self, edge: MapEdge) -> MapEdgeRow:
        row = self.session.get(MapEdgeRow, edge.id)
        if row is None:
            row = MapEdgeRow(id=edge.id, project_id=edge.project_id)
            self.session.add(row)
        row.subworld_id = edge.subworld_id
        row.from_node_id = edge.from_node_id
        row.to_node_id = edge.to_node_id
        row.edge_type = str(edge.edge_type)
        row.bidirectional = bool(edge.bidirectional)
        row.distance = float(edge.distance or 0.0)
        row.travel_time = float(edge.travel_time or 0.0)
        row.travel_cost = float(edge.travel_cost or 0.0)
        row.risk_level = float(edge.risk_level or 0.0)
        row.narrative_cost = float(edge.narrative_cost or 0.0)
        row.access_rule_id = edge.access_rule_id
        row.status = edge.status
        row.discovered_by_default = bool(edge.discovered_by_default)
        row.visibility_default = edge.visibility_default
        metadata = dict(edge.metadata)
        row.created_at_chapter = _created_at_chapter(metadata, default=int(getattr(row, "created_at_chapter", 0) or 0))
        metadata.setdefault("created_at_chapter", row.created_at_chapter)
        row.metadata_json = _dump(metadata)
        self.session.flush()
        return row

    # ------------------------------------------------------------------
    # Narrative and cognition rows
    # ------------------------------------------------------------------

    def create_narrative_node(self, node: NarrativeNode) -> NarrativeNodeRow:
        row = self.session.get(NarrativeNodeRow, node.id)
        if row is None:
            row = NarrativeNodeRow(id=node.id, project_id=node.project_id, node_type=node.node_type)
            self.session.add(row)
        row.title = node.title
        row.status = node.status
        row.payload_json = _dump(node.payload)
        row.metadata_json = _dump(node.metadata)
        self.session.flush()
        return row

    def create_narrative_edge(self, edge: NarrativeEdge) -> NarrativeEdgeRow:
        row = self.session.get(NarrativeEdgeRow, edge.id)
        if row is None:
            row = NarrativeEdgeRow(id=edge.id, project_id=edge.project_id)
            self.session.add(row)
        row.source_id = edge.source_id
        row.target_id = edge.target_id
        row.edge_type = edge.edge_type
        row.payload_json = _dump(edge.payload)
        row.metadata_json = _dump(edge.metadata)
        self.session.flush()
        return row

    def upsert_cognition_overlay(self, overlay: CognitionOverlay) -> CognitionOverlayRow:
        row = self.session.get(CognitionOverlayRow, overlay.id)
        if row is None:
            row = CognitionOverlayRow(
                id=overlay.id,
                project_id=overlay.project_id,
                observer_type=str(overlay.observer_type),
                observer_id=overlay.observer_id,
            )
            self.session.add(row)
        row.as_of_chapter = int(overlay.as_of_chapter or 0)
        row.as_of_story_time = overlay.as_of_story_time
        row.visible_refs_json = _dump(overlay.visible_refs)
        row.hidden_refs_json = _dump(overlay.hidden_refs)
        row.suspected_refs_json = _dump(overlay.suspected_refs)
        row.confirmed_refs_json = _dump(overlay.confirmed_refs)
        row.field_overrides_json = _dump(overlay.field_overrides)
        row.false_nodes_json = _dump(overlay.false_nodes)
        row.false_edges_json = _dump(overlay.false_edges)
        row.false_facts_json = _dump(overlay.false_facts)
        row.evidence_by_ref_json = _dump(overlay.evidence_by_ref)
        row.metadata_json = _dump(overlay.metadata)
        self.session.flush()
        return row

    def overlay_from_view(
        self,
        view: CognitionView,
        *,
        project_id: str,
        as_of_chapter: int,
        as_of_story_time: str = "",
    ) -> CognitionOverlay:
        return CognitionOverlay(
            id=f"cog_{project_id}_{view.observer_type}_{view.observer_id}_{as_of_chapter}",
            project_id=project_id,
            observer_type=view.observer_type,
            observer_id=view.observer_id,
            as_of_chapter=as_of_chapter,
            as_of_story_time=as_of_story_time,
            visible_refs=sorted(view.visible_refs),
            hidden_refs=sorted(view.hidden_refs),
            suspected_refs=sorted(view.suspected_refs),
            confirmed_refs=sorted(view.confirmed_refs),
            field_overrides=dict(view.field_overrides),
            false_nodes=dict(view.false_nodes),
            false_edges=dict(view.false_edges),
            false_facts=dict(view.false_facts),
            evidence_by_ref={key: list(value) for key, value in view.evidence_by_ref.items()},
        )

    # ------------------------------------------------------------------
    # Delta ledger
    # ------------------------------------------------------------------

    def append_graph_delta(self, delta: GraphDelta) -> GraphDeltaRow:
        row = GraphDeltaRow(
            id=delta.id,
            project_id=delta.project_id,
            chapter_number=int(delta.chapter_number or 0),
            story_time=delta.story_time,
            delta_type=str(delta.delta_type),
            operation=delta.operation,
            target_type=delta.target_type,
            target_id=delta.target_id,
            source_type=delta.source_type,
            source_id=delta.source_id,
            world_line_id=delta.world_line_id,
            summary=delta.summary,
            evidence_refs_json=_dump(delta.evidence_refs),
            review_verdict_id=delta.review_verdict_id,
            allowed_for_canon=bool(delta.allowed_for_canon),
            metadata_json=_dump(delta.metadata),
        )
        self.session.add(row)
        for patch in delta.node_patches:
            self.session.add(self._patch_row(delta, "node", patch))
        for patch in delta.edge_patches:
            self.session.add(self._patch_row(delta, "edge", patch))
        for patch in delta.fact_patches:
            self.session.add(self._patch_row(delta, "fact", patch))
        for patch in delta.map_patches:
            self.session.add(self._patch_row(delta, "map", patch))
        for patch in delta.cognition_patches:
            self.session.add(self._patch_row(delta, "cognition", patch))
            self.session.add(
                CognitionOverlayPatchRow(
                    project_id=delta.project_id,
                    observer_type=str(patch.observer_type),
                    observer_id=patch.observer_id,
                    delta_id=delta.id,
                    op=str(patch.op),
                    field_path=patch.field_path,
                    old_value_json=_dump(patch.old_value),
                    new_value_json=_dump(patch.new_value),
                    reason=patch.reason,
                    evidence_refs_json=_dump(patch.evidence_refs),
                )
            )
        for patch in delta.narrative_patches:
            self.session.add(self._patch_row(delta, "narrative", patch))
        self.session.flush()
        return row

    def _patch_row(
        self,
        delta: GraphDelta,
        patch_type: str,
        patch: NodePatch | EdgePatch | FactPatch | MapPatch | CognitionPatch | NarrativePatch,
    ) -> GraphDeltaPatchRow:
        target_ref, metadata = _patch_target_and_metadata(patch_type, patch)
        return GraphDeltaPatchRow(
            project_id=delta.project_id,
            delta_id=delta.id,
            chapter_number=int(delta.chapter_number or 0),
            patch_type=patch_type,
            target_ref=target_ref,
            op=str(patch.op),
            field_path=getattr(patch, "field_path", ""),
            old_value_json=_dump(getattr(patch, "old_value", None)),
            new_value_json=_dump(getattr(patch, "new_value", None)),
            reason=getattr(patch, "reason", ""),
            visibility_default=getattr(patch, "visibility_default", "visible"),
            metadata_json=_dump(metadata),
        )

    def list_graph_deltas(
        self,
        project_id: str,
        *,
        after_chapter: int = -1,
        through_chapter: int,
    ) -> list[GraphDelta]:
        rows = list(
            self.session.execute(
                select(GraphDeltaRow)
                .where(
                    GraphDeltaRow.project_id == project_id,
                    GraphDeltaRow.chapter_number > after_chapter,
                    GraphDeltaRow.chapter_number <= through_chapter,
                )
                .order_by(GraphDeltaRow.chapter_number.asc(), GraphDeltaRow.created_at.asc(), GraphDeltaRow.id.asc())
            )
            .scalars()
            .all()
        )
        patch_rows = list(
            self.session.execute(
                select(GraphDeltaPatchRow)
                .where(
                    GraphDeltaPatchRow.project_id == project_id,
                    GraphDeltaPatchRow.delta_id.in_([row.id for row in rows] or [""]),
                )
                .order_by(GraphDeltaPatchRow.created_at.asc(), GraphDeltaPatchRow.id.asc())
            )
            .scalars()
            .all()
        )
        patches_by_delta: dict[str, list[GraphDeltaPatchRow]] = {}
        for patch_row in patch_rows:
            patches_by_delta.setdefault(patch_row.delta_id, []).append(patch_row)

        return [
            self._graph_delta_from_row(row, patches_by_delta.get(row.id, []))
            for row in rows
        ]

    def _graph_delta_from_row(
        self,
        row: GraphDeltaRow,
        patch_rows: list[GraphDeltaPatchRow],
    ) -> GraphDelta:
        node_patches: list[NodePatch] = []
        edge_patches: list[EdgePatch] = []
        fact_patches: list[FactPatch] = []
        map_patches: list[MapPatch] = []
        cognition_patches: list[CognitionPatch] = []
        narrative_patches: list[NarrativePatch] = []

        for patch_row in patch_rows:
            patch_type = patch_row.patch_type
            metadata = _loads(patch_row.metadata_json, {})
            base = {
                "op": patch_row.op,
                "field_path": patch_row.field_path,
                "old_value": _loads(patch_row.old_value_json, None),
                "new_value": _loads(patch_row.new_value_json, None),
                "reason": patch_row.reason,
            }
            if patch_type == "node":
                node_patches.append(
                    NodePatch(
                        node_id=str(metadata.get("node_id", "")),
                        node_type=str(metadata.get("node_type", "")),
                        visibility_default=patch_row.visibility_default,
                        **base,
                    )
                )
            elif patch_type == "edge":
                edge_patches.append(
                    EdgePatch(
                        edge_id=str(metadata.get("edge_id", "")),
                        source_id=str(metadata.get("source_id", "")),
                        target_id=str(metadata.get("target_id", "")),
                        edge_type=str(metadata.get("edge_type", "")),
                        edge_family=str(metadata.get("edge_family", "")),
                        **base,
                    )
                )
            elif patch_type == "fact":
                fact_base = {key: value for key, value in base.items() if key != "field_path"}
                fact_patches.append(
                    FactPatch(
                        fact_id=str(metadata.get("fact_id", "")),
                        proposition=str(metadata.get("proposition", "")),
                        truth_value=str(metadata.get("truth_value", "")),
                        related_refs=list(metadata.get("related_refs", [])),
                        sensitivity_level=str(metadata.get("sensitivity_level", "")),
                        **fact_base,
                    )
                )
            elif patch_type == "map":
                map_patches.append(
                    MapPatch(
                        target_type=str(metadata.get("target_type", "")),
                        target_id=str(metadata.get("target_id", "")),
                        discovered_by_default=metadata.get("discovered_by_default"),
                        access_change=str(metadata.get("access_change", "")),
                        affected_path_cache_keys=list(metadata.get("affected_path_cache_keys", [])),
                        visibility_default=patch_row.visibility_default,
                        **base,
                    )
                )
            elif patch_type == "cognition":
                cognition_patches.append(
                    CognitionPatch(
                        observer_type=str(metadata.get("observer_type", "")),
                        observer_id=str(metadata.get("observer_id", "")),
                        evidence_refs=list(metadata.get("evidence_refs", [])),
                        **base,
                    )
                )
            elif patch_type == "narrative":
                narrative_patches.append(
                    NarrativePatch(
                        target_ref=patch_row.target_ref,
                        evidence_refs=list(metadata.get("evidence_refs", [])),
                        **base,
                    )
                )

        return GraphDelta(
            id=row.id,
            project_id=row.project_id,
            chapter_number=row.chapter_number,
            story_time=row.story_time,
            delta_type=row.delta_type,
            operation=getattr(row, "operation", "") or "",
            target_type=getattr(row, "target_type", "") or "",
            target_id=getattr(row, "target_id", "") or "",
            source_type=row.source_type,
            source_id=row.source_id,
            world_line_id=row.world_line_id,
            summary=row.summary,
            node_patches=node_patches,
            edge_patches=edge_patches,
            fact_patches=fact_patches,
            map_patches=map_patches,
            cognition_patches=cognition_patches,
            narrative_patches=narrative_patches,
            evidence_refs=_loads(row.evidence_refs_json, []),
            review_verdict_id=getattr(row, "review_verdict_id", "") or "",
            allowed_for_canon=bool(getattr(row, "allowed_for_canon", True)),
            metadata=_loads(row.metadata_json, {}),
        )

    # ------------------------------------------------------------------
    # Runtime loading
    # ------------------------------------------------------------------

    def load_base_world_graph(
        self,
        project_id: str,
        *,
        as_of_chapter: int,
        state_index: dict[str, dict[str, Any]] | None = None,
    ) -> ObjectiveWorldGraph:
        nodes = [
            _world_node_from_row(row)
            for row in self.session.execute(
                select(WorldNodeRow)
                .where(
                    WorldNodeRow.project_id == project_id,
                    WorldNodeRow.created_at_chapter <= as_of_chapter,
                )
                .order_by(WorldNodeRow.created_at_chapter.asc(), WorldNodeRow.id.asc())
            )
            .scalars()
            .all()
            if row.retired_at_chapter is None or row.retired_at_chapter > as_of_chapter
        ]
        states = self._latest_state_index(project_id, as_of_chapter)
        if state_index:
            states.update(
                {
                    node_id: dict(state)
                    for node_id, state in state_index.items()
                    if state
                }
            )
        edges = [
            _world_edge_from_row(row)
            for row in self.session.execute(
                select(WorldEdgeRow)
                .where(
                    WorldEdgeRow.project_id == project_id,
                    WorldEdgeRow.established_at_chapter <= as_of_chapter,
                )
                .order_by(WorldEdgeRow.established_at_chapter.asc(), WorldEdgeRow.id.asc())
            )
            .scalars()
            .all()
            if row.ended_at_chapter is None or row.ended_at_chapter > as_of_chapter
        ]
        facts = [
            _fact_node_from_row(row)
            for row in self.session.execute(
                select(FactNodeRow)
                .where(
                    FactNodeRow.project_id == project_id,
                    FactNodeRow.created_at_chapter <= as_of_chapter,
                )
                .order_by(FactNodeRow.created_at_chapter.asc(), FactNodeRow.id.asc())
            )
            .scalars()
            .all()
        ]
        return ObjectiveWorldGraph(nodes=nodes, states_by_node_id=states, edges=edges, facts=facts)

    def _latest_state_index(self, project_id: str, as_of_chapter: int) -> dict[str, dict[str, Any]]:
        rows = list(
            self.session.execute(
                select(WorldNodeStateRow)
                .where(
                    WorldNodeStateRow.project_id == project_id,
                    WorldNodeStateRow.as_of_chapter <= as_of_chapter,
                )
                .order_by(
                    WorldNodeStateRow.node_id.asc(),
                    WorldNodeStateRow.as_of_chapter.desc(),
                    WorldNodeStateRow.created_at.desc(),
                    WorldNodeStateRow.id.desc(),
                )
            )
            .scalars()
            .all()
        )
        states: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row.node_id in states:
                continue
            states[row.node_id] = _loads(row.state_json, {})
        return states

    def load_base_map_graph(
        self,
        project_id: str,
        *,
        as_of_chapter: int,
        map_node_index: dict[str, MapNode] | None = None,
        map_edge_index: dict[str, MapEdge] | None = None,
        cognition_by_observer: dict[tuple[str, str], CognitionView] | None = None,
    ) -> MapGraph:
        if map_node_index is None:
            nodes = [
                _map_node_from_row(row)
                for row in self.session.execute(
                    select(MapNodeRow)
                    .where(
                        MapNodeRow.project_id == project_id,
                        MapNodeRow.created_at_chapter <= as_of_chapter,
                    )
                    .order_by(MapNodeRow.id.asc())
                )
                .scalars()
                .all()
            ]
        else:
            nodes = list(map_node_index.values())

        if map_edge_index is None:
            edges = [
                _map_edge_from_row(row)
                for row in self.session.execute(
                    select(MapEdgeRow)
                    .where(
                        MapEdgeRow.project_id == project_id,
                        MapEdgeRow.created_at_chapter <= as_of_chapter,
                    )
                    .order_by(MapEdgeRow.id.asc())
                )
                .scalars()
                .all()
            ]
        else:
            edges = list(map_edge_index.values())

        return MapGraph(nodes=nodes, edges=edges, cognition_by_observer=cognition_by_observer)

    def load_cognition_views(
        self,
        project_id: str,
        *,
        as_of_chapter: int,
        observer_keys: list[tuple[str, str]] | None = None,
    ) -> dict[tuple[str, str], CognitionView]:
        rows = list(
            self.session.execute(
                select(CognitionOverlayRow)
                .where(
                    CognitionOverlayRow.project_id == project_id,
                    CognitionOverlayRow.as_of_chapter <= as_of_chapter,
                )
                .order_by(
                    CognitionOverlayRow.observer_type.asc(),
                    CognitionOverlayRow.observer_id.asc(),
                    CognitionOverlayRow.as_of_chapter.desc(),
                    CognitionOverlayRow.updated_at.desc(),
                    CognitionOverlayRow.id.desc(),
                )
            )
            .scalars()
            .all()
        )
        wanted = set(observer_keys or [])
        overlays: dict[tuple[str, str], CognitionOverlay] = {}
        for row in rows:
            key = (row.observer_type, row.observer_id)
            if wanted and key not in wanted:
                continue
            if key in overlays:
                continue
            overlays[key] = _cognition_overlay_from_row(row)
        return {key: CognitionView(overlay) for key, overlay in overlays.items()}

    def load_narrative_graph(self, project_id: str, *, as_of_chapter: int | None = None) -> NarrativeControlGraph:
        nodes = [
            _narrative_node_from_row(row)
            for row in self.session.execute(
                select(NarrativeNodeRow)
                .where(NarrativeNodeRow.project_id == project_id)
                .order_by(NarrativeNodeRow.created_at.asc(), NarrativeNodeRow.id.asc())
            )
            .scalars()
            .all()
            if as_of_chapter is None or _created_at_chapter(_loads(row.metadata_json, {})) <= as_of_chapter
        ]
        edges = [
            _narrative_edge_from_row(row)
            for row in self.session.execute(
                select(NarrativeEdgeRow)
                .where(NarrativeEdgeRow.project_id == project_id)
                .order_by(NarrativeEdgeRow.created_at.asc(), NarrativeEdgeRow.id.asc())
            )
            .scalars()
            .all()
            if as_of_chapter is None or _created_at_chapter(_loads(row.metadata_json, {})) <= as_of_chapter
        ]
        return NarrativeControlGraph(nodes=nodes, edges=edges)

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def latest_world_snapshot(self, project_id: str, as_of_chapter: int) -> WorldSnapshot | None:
        row = self.session.execute(
            select(WorldSnapshotRow)
            .where(
                WorldSnapshotRow.project_id == project_id,
                WorldSnapshotRow.as_of_chapter <= as_of_chapter,
            )
            .order_by(WorldSnapshotRow.as_of_chapter.desc(), WorldSnapshotRow.built_at.desc(), WorldSnapshotRow.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        return _world_snapshot_from_row(row) if row else None

    def latest_map_snapshot(self, project_id: str, as_of_chapter: int) -> MapSnapshot | None:
        row = self.session.execute(
            select(MapSnapshotRow)
            .where(
                MapSnapshotRow.project_id == project_id,
                MapSnapshotRow.as_of_chapter <= as_of_chapter,
            )
            .order_by(MapSnapshotRow.as_of_chapter.desc(), MapSnapshotRow.built_at.desc(), MapSnapshotRow.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        return _map_snapshot_from_row(row) if row else None

    def latest_available_chapter(self, project_id: str) -> int:
        values = [
            self.session.scalar(
                select(func.max(GraphDeltaRow.chapter_number)).where(GraphDeltaRow.project_id == project_id)
            ),
            self.session.scalar(
                select(func.max(WorldSnapshotRow.as_of_chapter)).where(WorldSnapshotRow.project_id == project_id)
            ),
            self.session.scalar(
                select(func.max(MapSnapshotRow.as_of_chapter)).where(MapSnapshotRow.project_id == project_id)
            ),
            self.session.scalar(
                select(func.max(CognitionOverlayRow.as_of_chapter)).where(CognitionOverlayRow.project_id == project_id)
            ),
            self.session.scalar(
                select(func.max(MapNodeRow.created_at_chapter)).where(MapNodeRow.project_id == project_id)
            ),
            self.session.scalar(
                select(func.max(MapEdgeRow.created_at_chapter)).where(MapEdgeRow.project_id == project_id)
            ),
        ]
        return max([int(value or 0) for value in values] or [0])

    def persist_world_snapshot(self, snapshot: WorldSnapshot) -> WorldSnapshotRow:
        row = WorldSnapshotRow(
            id=snapshot.id,
            project_id=snapshot.project_id,
            as_of_chapter=snapshot.as_of_chapter,
            as_of_story_time=snapshot.as_of_story_time,
            base_snapshot_id=snapshot.base_snapshot_id,
            objective_graph_digest=snapshot.objective_graph_digest,
            map_graph_digest=snapshot.map_graph_digest,
            reader_overlay_digest=snapshot.reader_overlay_digest,
            character_overlay_digests_json=_dump(snapshot.character_overlay_digests),
            world_node_state_index_json=_dump(snapshot.world_node_state_index),
            active_edge_ids_json=_dump(snapshot.active_edge_ids),
            active_fact_ids_json=_dump(snapshot.active_fact_ids),
            active_world_line_ids_json=_dump(snapshot.active_world_line_ids),
            open_gap_ids_json=_dump(snapshot.open_gap_ids),
            active_promise_ids_json=_dump(snapshot.active_promise_ids),
            objective_state_summary=snapshot.objective_state_summary,
            reader_state_summary=snapshot.reader_state_summary,
            source_delta_ids_json=_dump(snapshot.source_delta_ids),
            metadata_json=_dump(snapshot.metadata),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def persist_map_snapshot(self, snapshot: MapSnapshot) -> MapSnapshotRow:
        row = MapSnapshotRow(
            id=snapshot.id,
            project_id=snapshot.project_id,
            as_of_chapter=snapshot.as_of_chapter,
            map_node_index_json=_dump(snapshot.map_node_index),
            map_edge_index_json=_dump(snapshot.map_edge_index),
            blocked_edge_ids_json=_dump(snapshot.blocked_edge_ids),
            hidden_edge_ids_json=_dump(snapshot.hidden_edge_ids),
            metadata_json=_dump(snapshot.metadata),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def persist_cognition_snapshot(self, snapshot: CognitionSnapshot) -> BookCognitionSnapshotRow:
        row = BookCognitionSnapshotRow(
            id=snapshot.id,
            project_id=snapshot.project_id,
            observer_type=str(snapshot.observer_type),
            observer_id=snapshot.observer_id,
            as_of_chapter=snapshot.as_of_chapter,
            overlay_id=snapshot.overlay_id,
            visible_refs_json=_dump(snapshot.visible_refs),
            suspected_refs_json=_dump(snapshot.suspected_refs),
            confirmed_refs_json=_dump(snapshot.confirmed_refs),
            metadata_json=_dump(snapshot.metadata),
        )
        self.session.add(row)
        self.session.flush()
        return row


def _world_node_from_row(row: WorldNodeRow) -> WorldNode:
    return WorldNode(
        id=row.id,
        project_id=row.project_id,
        node_type=row.node_type,
        name=row.name,
        aliases=_loads(row.aliases_json, []),
        summary=getattr(row, "summary", "") or "",
        description=row.description,
        status=getattr(row, "status", "") or "active",
        importance=row.importance,
        scope=getattr(row, "scope", "") or "",
        tags=_loads(getattr(row, "tags_json", "[]"), []),
        created_at_chapter=row.created_at_chapter,
        retired_at_chapter=row.retired_at_chapter,
        valid_from_chapter=getattr(row, "valid_from_chapter", row.created_at_chapter) or 0,
        valid_until_chapter=getattr(row, "valid_until_chapter", None),
        source_refs=_loads(getattr(row, "source_refs_json", "[]"), []),
        is_active=row.is_active,
        profile=_loads(row.profile_json, {}),
        metadata=_loads(row.metadata_json, {}),
    )


def _world_edge_from_row(row: WorldEdgeRow) -> WorldEdge:
    return WorldEdge(
        id=row.id,
        project_id=row.project_id,
        source_id=row.source_id,
        target_id=row.target_id,
        edge_type=row.edge_type,
        edge_family=row.edge_family,
        directionality=row.directionality,
        weight=row.weight,
        confidence=row.confidence,
        established_at_chapter=row.established_at_chapter,
        ended_at_chapter=row.ended_at_chapter,
        valid_from_chapter=getattr(row, "valid_from_chapter", row.established_at_chapter) or 0,
        valid_until_chapter=getattr(row, "valid_until_chapter", None),
        is_active=row.is_active,
        status=getattr(row, "status", "") or "active",
        visibility=getattr(row, "visibility", "") or "",
        truth_relation=getattr(row, "truth_relation", "") or "true",
        visibility_default=row.visibility_default,
        source_refs=_loads(getattr(row, "source_refs_json", "[]"), []),
        state=_loads(row.state_json, {}),
        evidence_refs=_loads(row.evidence_refs_json, []),
        metadata=_loads(row.metadata_json, {}),
    )


def _fact_node_from_row(row: FactNodeRow) -> FactNode:
    return FactNode(
        id=row.id,
        project_id=row.project_id,
        proposition=row.proposition,
        fact_type=row.fact_type,
        truth_value=row.truth_value,
        confidence=row.confidence,
        related_node_refs=_loads(row.related_node_refs_json, []),
        related_edge_refs=_loads(row.related_edge_refs_json, []),
        source_refs=_loads(row.source_refs_json, []),
        created_at_chapter=row.created_at_chapter,
        happened_at_story_time=row.happened_at_story_time,
        contradiction_refs=_loads(row.contradiction_refs_json, []),
        sensitivity_level=row.sensitivity_level,
        narrative_function=row.narrative_function,
        state=_loads(row.state_json, {}),
        metadata=_loads(row.metadata_json, {}),
    )


def _reader_promise_from_row(row: BookReaderPromiseRow) -> ReaderPromise:
    return ReaderPromise(
        promise_id=row.promise_id,
        project_id=row.project_id,
        promise_type=row.promise_type,
        summary=row.summary,
        created_at_chapter=row.created_at_chapter,
        expected_payoff_window=row.expected_payoff_window,
        maximum_safe_delay=row.maximum_safe_delay,
        current_debt_level=row.current_debt_level,
        reward_tags=_loads(row.reward_tags_json, []),
        linked_threads=_loads(row.linked_threads_json, []),
        linked_knowledge_gaps=_loads(row.linked_knowledge_gaps_json, []),
        status=row.status,
        source_refs=_loads(row.source_refs_json, []),
        metadata=_loads(row.metadata_json, {}),
    )


def _reader_experience_delta_from_row(row: BookReaderExperienceDeltaRow) -> ReaderExperienceDeltaRecord:
    return ReaderExperienceDeltaRecord(
        reader_experience_delta_id=row.reader_experience_delta_id,
        project_id=row.project_id,
        chapter_number=row.chapter_number,
        reader_state_before=row.reader_state_before,
        reader_state_after=row.reader_state_after,
        cognition_transition=row.cognition_transition,
        payoff_type=row.payoff_type,
        reward_tags=_loads(row.reward_tags_json, []),
        emotional_effect=row.emotional_effect,
        promise_debt_change=row.promise_debt_change,
        next_desire=row.next_desire,
        fairness_evidence=_loads(row.fairness_evidence_json, []),
        source_refs=_loads(row.source_refs_json, []),
        metadata=_loads(row.metadata_json, {}),
    )


def _map_node_from_row(row: MapNodeRow) -> MapNode:
    coordinates = _loads(row.coordinates_json, {})
    metadata = _loads(row.metadata_json, {})
    if isinstance(metadata, dict):
        metadata.setdefault("created_at_chapter", int(getattr(row, "created_at_chapter", 0) or 0))
    else:
        metadata = {"created_at_chapter": int(getattr(row, "created_at_chapter", 0) or 0)}
    return MapNode(
        id=row.id,
        project_id=row.project_id,
        subworld_id=getattr(row, "subworld_id", "") or "",
        region_id=getattr(row, "region_id", "") or "",
        node_type=row.node_type,
        name=row.name,
        aliases=_loads(row.aliases_json, []),
        description=getattr(row, "description", "") or "",
        parent_id=row.parent_id,
        hierarchy_path=row.hierarchy_path,
        scale_level=row.scale_level,
        coordinates=coordinates or None,
        shape_ref=row.shape_ref,
        terrain=row.terrain,
        climate=row.climate,
        culture_tag=row.culture_tag,
        default_danger_level=row.default_danger_level,
        access_level=row.access_level,
        status=row.status,
        metadata=metadata,
    )


def _map_edge_from_row(row: MapEdgeRow) -> MapEdge:
    metadata = _loads(row.metadata_json, {})
    if isinstance(metadata, dict):
        metadata.setdefault("created_at_chapter", int(getattr(row, "created_at_chapter", 0) or 0))
    else:
        metadata = {"created_at_chapter": int(getattr(row, "created_at_chapter", 0) or 0)}
    return MapEdge(
        id=row.id,
        project_id=row.project_id,
        subworld_id=getattr(row, "subworld_id", "") or "",
        from_node_id=row.from_node_id,
        to_node_id=row.to_node_id,
        edge_type=row.edge_type,
        bidirectional=row.bidirectional,
        distance=row.distance,
        travel_time=row.travel_time,
        travel_cost=row.travel_cost,
        risk_level=row.risk_level,
        narrative_cost=row.narrative_cost,
        access_rule_id=row.access_rule_id,
        status=row.status,
        discovered_by_default=row.discovered_by_default,
        visibility_default=row.visibility_default,
        metadata=metadata,
    )


def _narrative_node_from_row(row: NarrativeNodeRow) -> NarrativeNode:
    return NarrativeNode(
        id=row.id,
        project_id=row.project_id,
        node_type=row.node_type,
        title=row.title,
        status=row.status,
        payload=_loads(row.payload_json, {}),
        metadata=_loads(row.metadata_json, {}),
    )


def _narrative_edge_from_row(row: NarrativeEdgeRow) -> NarrativeEdge:
    return NarrativeEdge(
        id=row.id,
        project_id=row.project_id,
        source_id=row.source_id,
        target_id=row.target_id,
        edge_type=row.edge_type,
        payload=_loads(row.payload_json, {}),
        metadata=_loads(row.metadata_json, {}),
    )


def _cognition_overlay_from_row(row: CognitionOverlayRow) -> CognitionOverlay:
    false_nodes = {
        key: WorldNode.model_validate(value)
        for key, value in _loads(row.false_nodes_json, {}).items()
    }
    false_edges = {
        key: _false_edge_from_payload(value)
        for key, value in _loads(row.false_edges_json, {}).items()
    }
    false_facts = {
        key: FactNode.model_validate(value)
        for key, value in _loads(row.false_facts_json, {}).items()
    }
    return CognitionOverlay(
        id=row.id,
        project_id=row.project_id,
        observer_type=row.observer_type,
        observer_id=row.observer_id,
        as_of_chapter=row.as_of_chapter,
        as_of_story_time=row.as_of_story_time,
        visible_refs=_loads(row.visible_refs_json, []),
        hidden_refs=_loads(row.hidden_refs_json, []),
        suspected_refs=_loads(row.suspected_refs_json, []),
        confirmed_refs=_loads(row.confirmed_refs_json, []),
        field_overrides=_loads(row.field_overrides_json, {}),
        false_nodes=false_nodes,
        false_edges=false_edges,
        false_facts=false_facts,
        evidence_by_ref=_loads(row.evidence_by_ref_json, {}),
        metadata=_loads(row.metadata_json, {}),
    )


def _false_edge_from_payload(value: Any) -> WorldEdge | MapEdge:
    if isinstance(value, dict) and "from_node_id" in value:
        return MapEdge.model_validate(value)
    return WorldEdge.model_validate(value)


def _world_snapshot_from_row(row: WorldSnapshotRow) -> WorldSnapshot:
    return WorldSnapshot(
        id=row.id,
        project_id=row.project_id,
        as_of_chapter=row.as_of_chapter,
        as_of_story_time=row.as_of_story_time,
        base_snapshot_id=row.base_snapshot_id,
        objective_graph_digest=getattr(row, "objective_graph_digest", "") or "",
        map_graph_digest=getattr(row, "map_graph_digest", "") or "",
        reader_overlay_digest=getattr(row, "reader_overlay_digest", "") or "",
        character_overlay_digests=_loads(getattr(row, "character_overlay_digests_json", "{}"), {}),
        world_node_state_index=_loads(row.world_node_state_index_json, {}),
        active_edge_ids=_loads(row.active_edge_ids_json, []),
        active_fact_ids=_loads(row.active_fact_ids_json, []),
        active_world_line_ids=_loads(row.active_world_line_ids_json, []),
        open_gap_ids=_loads(row.open_gap_ids_json, []),
        active_promise_ids=_loads(getattr(row, "active_promise_ids_json", "[]"), []),
        objective_state_summary=getattr(row, "objective_state_summary", "") or "",
        reader_state_summary=getattr(row, "reader_state_summary", "") or "",
        source_delta_ids=_loads(row.source_delta_ids_json, []),
        built_at=row.built_at.isoformat() if row.built_at else "",
        metadata=_loads(row.metadata_json, {}),
    )


def _map_snapshot_from_row(row: MapSnapshotRow) -> MapSnapshot:
    return MapSnapshot(
        id=row.id,
        project_id=row.project_id,
        as_of_chapter=row.as_of_chapter,
        map_node_index={
            key: MapNode.model_validate(value)
            for key, value in _loads(row.map_node_index_json, {}).items()
        },
        map_edge_index={
            key: MapEdge.model_validate(value)
            for key, value in _loads(row.map_edge_index_json, {}).items()
        },
        blocked_edge_ids=_loads(row.blocked_edge_ids_json, []),
        hidden_edge_ids=_loads(row.hidden_edge_ids_json, []),
        built_at=row.built_at.isoformat() if row.built_at else "",
        metadata=_loads(row.metadata_json, {}),
    )


def _patch_target_and_metadata(
    patch_type: str,
    patch: NodePatch | EdgePatch | FactPatch | MapPatch | CognitionPatch | NarrativePatch,
) -> tuple[str, dict[str, Any]]:
    if patch_type == "node":
        assert isinstance(patch, NodePatch)
        return f"node:{patch.node_id}", {"node_id": patch.node_id, "node_type": str(patch.node_type)}
    if patch_type == "edge":
        assert isinstance(patch, EdgePatch)
        return (
            f"edge:{patch.edge_id}",
            {
                "edge_id": patch.edge_id,
                "source_id": patch.source_id,
                "target_id": patch.target_id,
                "edge_type": patch.edge_type,
                "edge_family": str(patch.edge_family),
            },
        )
    if patch_type == "fact":
        assert isinstance(patch, FactPatch)
        return (
            f"fact:{patch.fact_id}",
            {
                "fact_id": patch.fact_id,
                "proposition": patch.proposition,
                "truth_value": patch.truth_value,
                "related_refs": list(patch.related_refs),
                "sensitivity_level": patch.sensitivity_level,
            },
        )
    if patch_type == "map":
        assert isinstance(patch, MapPatch)
        return (
            f"{patch.target_type}:{patch.target_id}",
            {
                "target_type": patch.target_type,
                "target_id": patch.target_id,
                "discovered_by_default": patch.discovered_by_default,
                "access_change": patch.access_change,
                "affected_path_cache_keys": list(patch.affected_path_cache_keys),
            },
        )
    if patch_type == "cognition":
        assert isinstance(patch, CognitionPatch)
        return (
            f"cognition:{patch.observer_type}:{patch.observer_id}",
            {
                "observer_type": str(patch.observer_type),
                "observer_id": patch.observer_id,
                "evidence_refs": list(patch.evidence_refs),
            },
        )
    assert isinstance(patch, NarrativePatch)
    return patch.target_ref, {"evidence_refs": list(patch.evidence_refs)}


def _created_at_chapter(metadata: dict[str, Any], *, default: int = 0) -> int:
    try:
        return int(metadata.get("created_at_chapter", default) or default)
    except (TypeError, ValueError):
        return int(default or 0)


__all__ = [
    "BookStateRepository",
    "_as_jsonable",
    "_dump",
    "_loads",
]
