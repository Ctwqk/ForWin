from __future__ import annotations

from collections import defaultdict
import json
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from forwin.models.entity import Entity, EntityState
from forwin.models.world_v4 import (
    BeliefRow,
    CognitionSnapshotRow,
    KnowledgeGapRow,
    KnowledgeUpdateEventRow,
    WorldDeltaRow,
    WorldLineRow,
    WorldModelSnapshotV4Row,
)
from forwin.protocol.world_v4 import CognitionState, ObserverType, WorldModelSnapshot


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _observer_key(observer_type: str, observer_id: str) -> tuple[str, str]:
    return (observer_type or "", observer_id or "")


def _chapter_in_scope(*chapter_numbers: int, as_of_chapter: int) -> bool:
    scoped = [chapter for chapter in chapter_numbers if chapter > 0]
    return not scoped or min(scoped) <= as_of_chapter


def _is_known_visibility(visibility: str) -> bool:
    return visibility in {"partially_revealed", "partially_known", "known", "confirmed"}


def _visible_to_reader_for_delta(delta: WorldDeltaRow) -> str:
    if delta.delta_kind in {"visible", "reveal"}:
        return "revealed"
    if delta.delta_kind == "hint":
        return "hinted"
    return "hidden_or_unconfirmed"


def _derive_objective_layer(delta: WorldDeltaRow) -> dict[str, Any]:
    summary = delta.summary or ""
    objective_layer: dict[str, Any] = {
        "last_delta_id": delta.delta_id,
        "last_world_line_id": delta.world_line_id,
        "last_delta_kind": delta.delta_kind,
        "last_summary": summary,
    }
    if (
        "围困" in summary
        or "被围" in summary
        or "siege" in summary.lower()
        or "siege" in delta.world_line_id.lower()
    ):
        objective_layer["siege_status"] = "under_siege"
    return objective_layer


class WorldModelProjection:
    """Rebuild v4 snapshots from append-only ledgers."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def rebuild_snapshot(self, project_id: str, *, as_of_chapter: int) -> WorldModelSnapshot:
        lines = list(
            self.session.execute(
                select(WorldLineRow)
                .where(WorldLineRow.project_id == project_id)
                .order_by(WorldLineRow.created_at.asc(), WorldLineRow.id.asc())
            )
            .scalars()
            .all()
        )
        deltas = list(
            self.session.execute(
                select(WorldDeltaRow)
                .where(
                    WorldDeltaRow.project_id == project_id,
                    WorldDeltaRow.narrative_chapter <= as_of_chapter,
                )
                .order_by(
                    WorldDeltaRow.narrative_chapter.asc(),
                    WorldDeltaRow.created_at.asc(),
                    WorldDeltaRow.id.asc(),
                )
            )
            .scalars()
            .all()
        )
        open_gaps = list(
            self.session.execute(
                select(KnowledgeGapRow)
                .where(
                    KnowledgeGapRow.project_id == project_id,
                    KnowledgeGapRow.status.in_(("open", "hinted", "partially_closed")),
                )
                .order_by(KnowledgeGapRow.created_at.asc(), KnowledgeGapRow.id.asc())
            )
            .scalars()
            .all()
        )
        knowledge_updates = list(
            self.session.execute(
                select(KnowledgeUpdateEventRow)
                .where(
                    KnowledgeUpdateEventRow.project_id == project_id,
                    KnowledgeUpdateEventRow.chapter_number <= as_of_chapter,
                )
                .order_by(
                    KnowledgeUpdateEventRow.chapter_number.asc(),
                    KnowledgeUpdateEventRow.created_at.asc(),
                    KnowledgeUpdateEventRow.id.asc(),
                )
            )
            .scalars()
            .all()
        )
        belief_rows = [
            row
            for row in self.session.execute(
                select(BeliefRow)
                .where(BeliefRow.project_id == project_id)
                .order_by(
                    BeliefRow.last_updated_at_chapter.asc(),
                    BeliefRow.created_at_chapter.asc(),
                    BeliefRow.created_at.asc(),
                    BeliefRow.id.asc(),
                )
            )
            .scalars()
            .all()
            if _chapter_in_scope(
                int(row.last_updated_at_chapter or 0),
                int(row.created_at_chapter or 0),
                as_of_chapter=as_of_chapter,
            )
        ]

        active_world_line_ids = [
            line.world_line_id
            for line in lines
            if any(delta.world_line_id == line.world_line_id for delta in deltas)
        ]
        if not active_world_line_ids:
            active_world_line_ids = [line.world_line_id for line in lines]

        source_delta_ids = [delta.delta_id for delta in deltas]
        objective_state_summary = "；".join(delta.summary for delta in deltas)
        cognition_states = self._rebuild_cognition_snapshots(
            project_id,
            as_of_chapter=as_of_chapter,
            open_gaps=open_gaps,
            knowledge_updates=knowledge_updates,
            belief_rows=belief_rows,
        )
        derived_entity_state_ids = self._materialize_entity_states(
            project_id,
            as_of_chapter=as_of_chapter,
            deltas=deltas,
        )
        reader_cognition_state = cognition_states.get(("reader", "reader"))
        character_cognition_states = {
            observer_id: state
            for (observer_type, observer_id), state in cognition_states.items()
            if observer_type == "character"
        }
        snapshot = WorldModelSnapshot(
            snapshot_id=f"snapshot_{project_id}_{as_of_chapter}",
            project_id=project_id,
            as_of_chapter=as_of_chapter,
            active_world_line_ids=active_world_line_ids,
            open_gap_ids=[gap.gap_id for gap in open_gaps],
            reader_cognition_state=reader_cognition_state,
            character_cognition_states=character_cognition_states,
            objective_state_summary=objective_state_summary,
            source_delta_ids=source_delta_ids,
            metadata={"derived_entity_state_ids": derived_entity_state_ids},
        )
        row = WorldModelSnapshotV4Row(
            project_id=project_id,
            snapshot_id=snapshot.snapshot_id,
            as_of_chapter=as_of_chapter,
            active_world_line_ids_json=json.dumps(
                snapshot.active_world_line_ids,
                ensure_ascii=False,
            ),
            open_gap_ids_json=json.dumps(snapshot.open_gap_ids, ensure_ascii=False),
            reader_cognition_state_json=_dumps(
                reader_cognition_state.model_dump(mode="json")
                if reader_cognition_state
                else {}
            ),
            character_cognition_states_json=_dumps(
                {
                    observer_id: state.model_dump(mode="json")
                    for observer_id, state in character_cognition_states.items()
                }
            ),
            objective_state_summary=snapshot.objective_state_summary,
            source_delta_ids_json=json.dumps(snapshot.source_delta_ids, ensure_ascii=False),
            metadata_json=json.dumps(snapshot.metadata, ensure_ascii=False),
        )
        self.session.add(row)
        self.session.flush()
        return snapshot

    def _rebuild_cognition_snapshots(
        self,
        project_id: str,
        *,
        as_of_chapter: int,
        open_gaps: list[KnowledgeGapRow],
        knowledge_updates: list[KnowledgeUpdateEventRow],
        belief_rows: list[BeliefRow],
    ) -> dict[tuple[str, str], CognitionState]:
        existing_rows = list(
            self.session.execute(
                select(CognitionSnapshotRow).where(
                    CognitionSnapshotRow.project_id == project_id,
                    CognitionSnapshotRow.as_of_chapter == as_of_chapter,
                )
            )
            .scalars()
            .all()
        )
        existing_by_observer = {
            _observer_key(row.observer_type, row.observer_id): row
            for row in existing_rows
        }
        self.session.execute(
            delete(CognitionSnapshotRow).where(
                CognitionSnapshotRow.project_id == project_id,
                CognitionSnapshotRow.as_of_chapter == as_of_chapter,
            )
        )

        observers: set[tuple[str, str]] = set()
        updates_by_observer: dict[tuple[str, str], list[KnowledgeUpdateEventRow]] = defaultdict(list)
        beliefs_by_observer: dict[tuple[str, str], list[BeliefRow]] = defaultdict(list)
        gap_states_by_observer: dict[tuple[str, str], list[tuple[str, dict[str, Any]]]] = defaultdict(list)

        observers.update(existing_by_observer)

        for update in knowledge_updates:
            key = _observer_key(update.observer_type, update.observer_id)
            observers.add(key)
            updates_by_observer[key].append(update)

        for belief in belief_rows:
            key = _observer_key(belief.holder_type, belief.holder_id)
            observers.add(key)
            beliefs_by_observer[key].append(belief)

        for gap in open_gaps:
            for observer_state in _loads(gap.observer_states_json, {}).values():
                if not isinstance(observer_state, dict):
                    continue
                key = _observer_key(
                    str(observer_state.get("observer_type", "")),
                    str(observer_state.get("observer_id", "")),
                )
                observers.add(key)
                gap_states_by_observer[key].append((gap.gap_id, observer_state))

        cognition_states: dict[tuple[str, str], CognitionState] = {}
        for observer_type, observer_id in sorted(observers):
            if not observer_type or not observer_id:
                continue

            existing = existing_by_observer.get((observer_type, observer_id))
            visibility_by_delta: dict[str, str] = dict(
                _loads(existing.visibility_by_delta_json, {}) if existing else {}
            )
            known_delta_ids: list[str] = list(
                _loads(existing.known_delta_ids_json, []) if existing else []
            )
            suspected_gap_ids: list[str] = list(
                _loads(existing.suspected_gap_ids_json, []) if existing else []
            )

            for update in updates_by_observer[(observer_type, observer_id)]:
                to_state = update.to_state or "unknown"
                if update.related_delta_id:
                    visibility_by_delta[update.related_delta_id] = to_state
                    if _is_known_visibility(to_state) and update.related_delta_id not in known_delta_ids:
                        known_delta_ids.append(update.related_delta_id)
                if to_state == "suspected" and update.related_gap_id not in suspected_gap_ids:
                    suspected_gap_ids.append(update.related_gap_id)

            for gap_id, observer_state in gap_states_by_observer[(observer_type, observer_id)]:
                visibility = str(observer_state.get("visibility", "unknown"))
                if visibility == "suspected" and gap_id not in suspected_gap_ids:
                    suspected_gap_ids.append(gap_id)

            belief_payloads = [
                {
                    "belief_id": belief.belief_id,
                    "holder_type": belief.holder_type,
                    "holder_id": belief.holder_id,
                    "proposition": belief.proposition,
                    "truth_relation": belief.truth_relation,
                    "confidence": belief.confidence,
                    "belief_status": belief.belief_status,
                    "evidence_sources": _loads(belief.evidence_sources_json, []),
                    "created_at_chapter": belief.created_at_chapter,
                    "created_at_story_time": belief.created_at_story_time,
                    "contradicted_by": _loads(belief.contradicted_by_json, []),
                    "last_updated_at_chapter": belief.last_updated_at_chapter,
                    "metadata": _loads(belief.metadata_json, {}),
                }
                for belief in beliefs_by_observer[(observer_type, observer_id)]
            ] or (_loads(existing.beliefs_json, []) if existing else [])
            state = CognitionState(
                cognition_state_id=f"cognition_{observer_type}_{observer_id}_{as_of_chapter}",
                project_id=project_id,
                observer_type=ObserverType(observer_type),
                observer_id=observer_id,
                as_of_chapter=as_of_chapter,
                as_of_story_time=existing.as_of_story_time if existing else "",
                beliefs=belief_payloads,
                known_delta_ids=known_delta_ids,
                suspected_gap_ids=suspected_gap_ids,
                visibility_by_delta=visibility_by_delta,
                metadata=_loads(existing.metadata_json, {}) if existing else {},
            )
            cognition_states[(observer_type, observer_id)] = state
            self.session.add(
                CognitionSnapshotRow(
                    project_id=project_id,
                    cognition_state_id=state.cognition_state_id,
                    observer_type=observer_type,
                    observer_id=observer_id,
                    as_of_chapter=as_of_chapter,
                    beliefs_json=_dumps(belief_payloads),
                    known_delta_ids_json=_dumps(known_delta_ids),
                    suspected_gap_ids_json=_dumps(suspected_gap_ids),
                    visibility_by_delta_json=_dumps(visibility_by_delta),
                    metadata_json=existing.metadata_json if existing else "{}",
                )
            )

        self.session.flush()
        return cognition_states

    def _materialize_entity_states(
        self,
        project_id: str,
        *,
        as_of_chapter: int,
        deltas: list[WorldDeltaRow],
    ) -> list[str]:
        entities = list(
            self.session.execute(
                select(Entity)
                .where(Entity.project_id == project_id)
                .order_by(Entity.name.asc(), Entity.id.asc())
            )
            .scalars()
            .all()
        )
        if not entities:
            return []

        entity_ids = [entity.id for entity in entities]
        self.session.execute(
            delete(EntityState).where(
                EntityState.entity_id.in_(entity_ids),
                EntityState.as_of_chapter == as_of_chapter,
            )
        )

        entities_by_ref: dict[str, Entity] = {}
        for entity in entities:
            refs = {entity.id, entity.name}
            refs.update(str(alias) for alias in _loads(entity.aliases_json, []) if alias)
            for ref in refs:
                entities_by_ref[ref] = entity

        deltas_by_entity: dict[str, list[WorldDeltaRow]] = defaultdict(list)
        for delta in deltas:
            refs = set(_loads(delta.affected_entities_json, []))
            refs.update(_loads(delta.affected_locations_json, []))
            refs.update(_loads(delta.affected_factions_json, []))
            refs.update(_loads(delta.affected_resources_json, []))
            for ref in refs:
                entity = entities_by_ref.get(str(ref))
                if entity is not None:
                    deltas_by_entity[entity.id].append(delta)

        entity_state_ids: list[str] = []
        for entity in entities:
            affected_deltas = deltas_by_entity.get(entity.id, [])
            if not affected_deltas:
                continue

            objective_layer: dict[str, Any] = {}
            visible_to_reader = "hidden_or_unconfirmed"
            source_delta_ids: list[str] = []
            for delta in affected_deltas:
                objective_layer.update(_derive_objective_layer(delta))
                visible_to_reader = _visible_to_reader_for_delta(delta)
                source_delta_ids.append(delta.delta_id)

            row = EntityState(
                entity_id=entity.id,
                as_of_chapter=as_of_chapter,
                state_json=_dumps(
                    {
                        "state_layer": "derived_entity_state",
                        "objective_layer": objective_layer,
                        "visible_to_reader": visible_to_reader,
                        "reader_layer": {"visibility": visible_to_reader},
                        "source_delta_ids": source_delta_ids,
                    }
                ),
            )
            self.session.add(row)
            self.session.flush()
            entity_state_ids.append(row.id)

        self.session.flush()
        return entity_state_ids
