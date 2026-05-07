from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.world_v4 import (
    BeliefRow,
    KnowledgeGapRow,
    KnowledgeUpdateEventRow,
    ReaderExperienceDeltaRow,
    RevealEventRow,
    WorldDeltaRow,
    WorldLineRow,
    WorldModelSnapshotV4Row,
)
from forwin.protocol.world_v4 import (
    Belief,
    DeltaSource,
    KnowledgeGap,
    KnowledgeUpdateEvent,
    ReaderExperienceDelta,
    RevealEvent,
    WorldDelta,
    WorldLine,
    WorldModelSnapshot,
)


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _model_json(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    return dict(model or {})


class WorldModelRepository:
    """Persistence API for v4 world-model ledgers and snapshots."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create_world_line(self, world_line: WorldLine) -> WorldLineRow:
        row = WorldLineRow(
            project_id=world_line.project_id,
            world_line_id=world_line.world_line_id,
            line_type=world_line.line_type,
            title=world_line.title,
            participants_json=_json(world_line.participants),
            objective_state_summary=world_line.objective_state_summary,
            is_visible_onstage=world_line.is_visible_onstage,
            planned_reveal_chapter=world_line.planned_reveal_chapter,
            long_term_promise=world_line.long_term_promise,
            source_refs_json=_json(world_line.source_refs),
            metadata_json=_json(world_line.metadata),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def append_world_delta(self, world_delta: WorldDelta) -> WorldDeltaRow:
        source: DeltaSource = world_delta.source
        row = WorldDeltaRow(
            project_id=world_delta.project_id,
            delta_id=world_delta.delta_id,
            world_line_id=world_delta.world_line_id,
            delta_kind=_enum_value(world_delta.delta_kind),
            summary=world_delta.summary,
            objective_story_time=world_delta.objective_story_time,
            narrative_chapter=int(world_delta.narrative_chapter or 0),
            source_type=_enum_value(source.source_type),
            source_actor_id=source.actor_id,
            source_mechanism=source.mechanism,
            source_evidence_refs_json=_json(source.evidence_refs),
            affected_entities_json=_json(world_delta.affected_entities),
            affected_factions_json=_json(world_delta.affected_factions),
            affected_locations_json=_json(world_delta.affected_locations),
            affected_resources_json=_json(world_delta.affected_resources),
            affected_rules_json=_json(world_delta.affected_rules),
            observer_states_json=_json(
                {
                    key: _model_json(value)
                    for key, value in world_delta.observer_states.items()
                }
            ),
            allowed_for_canon=world_delta.allowed_for_canon,
            source_refs_json=_json(world_delta.source_refs),
            metadata_json=_json(world_delta.metadata),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def append_belief(self, belief: Belief, *, project_id: str | None = None) -> BeliefRow:
        row = BeliefRow(
            project_id=project_id or str(belief.metadata.get("project_id", "")),
            belief_id=belief.belief_id,
            holder_type=_enum_value(belief.holder_type),
            holder_id=belief.holder_id,
            proposition=belief.proposition,
            truth_relation=_enum_value(belief.truth_relation),
            confidence=belief.confidence,
            belief_status=_enum_value(belief.belief_status),
            evidence_sources_json=_json(belief.evidence_sources),
            created_at_chapter=int(belief.created_at_chapter or 0),
            created_at_story_time=belief.created_at_story_time,
            contradicted_by_json=_json(belief.contradicted_by),
            last_updated_at_chapter=int(belief.last_updated_at_chapter or 0),
            metadata_json=_json(belief.metadata),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def create_or_update_gap(
        self,
        gap: KnowledgeGap | None = None,
        **kwargs: Any,
    ) -> KnowledgeGapRow:
        if gap is not None:
            project_id = gap.project_id
            gap_id = gap.gap_id
            payload = gap.model_dump(mode="json")
        else:
            project_id = str(kwargs["project_id"])
            gap_id = str(kwargs["gap_id"])
            payload = dict(kwargs)

        row = self.get_gap(project_id, gap_id)
        if row is None:
            row = KnowledgeGapRow(
                project_id=project_id,
                gap_id=gap_id,
                objective_truth=str(payload.get("objective_truth", "")),
            )
            self.session.add(row)

        row.objective_truth = str(payload.get("objective_truth", row.objective_truth))
        row.happened_at_story_time = str(payload.get("happened_at_story_time", ""))
        row.related_world_line_id = str(payload.get("related_world_line_id", ""))
        row.observer_states_json = _json(payload.get("observer_states", {}))
        row.narrative_function = str(payload.get("narrative_function", ""))
        row.planned_closure = str(payload.get("planned_closure", ""))
        row.maximum_safe_delay = int(payload.get("maximum_safe_delay") or 0)
        row.fairness_requirements_json = _json(payload.get("fairness_requirements", []))
        row.status = str(payload.get("status", row.status))
        row.source_refs_json = _json(payload.get("source_refs", []))
        row.metadata_json = _json(payload.get("metadata", {}))
        self.session.flush()
        return row

    def get_gap(self, project_id: str, gap_id: str) -> KnowledgeGapRow | None:
        stmt = (
            select(KnowledgeGapRow)
            .where(
                KnowledgeGapRow.project_id == project_id,
                KnowledgeGapRow.gap_id == gap_id,
            )
            .limit(1)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def append_reveal_event(self, reveal_event: RevealEvent) -> RevealEventRow:
        row = RevealEventRow(
            project_id=reveal_event.project_id,
            reveal_event_id=reveal_event.reveal_event_id,
            reveals_fact_id=reveal_event.reveals_fact_id,
            reveals_delta_id=reveal_event.reveals_delta_id,
            related_gap_id=reveal_event.related_gap_id,
            reveal_to_reader=reveal_event.reveal_to_reader,
            reveal_to_characters_json=_json(reveal_event.reveal_to_characters),
            reveal_method=reveal_event.reveal_method,
            from_state=_enum_value(reveal_event.from_state),
            to_state=_enum_value(reveal_event.to_state),
            emotional_effect=reveal_event.emotional_effect,
            narrative_function=reveal_event.narrative_function,
            fairness_evidence_json=_json(reveal_event.fairness_evidence),
            source_refs_json=_json(reveal_event.source_refs),
            metadata_json=_json(reveal_event.metadata),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def append_knowledge_update(
        self,
        knowledge_update: KnowledgeUpdateEvent,
    ) -> KnowledgeUpdateEventRow:
        row = KnowledgeUpdateEventRow(
            project_id=knowledge_update.project_id,
            update_event_id=knowledge_update.update_event_id,
            update_type=_enum_value(knowledge_update.update_type),
            observer_type=_enum_value(knowledge_update.observer_type),
            observer_id=knowledge_update.observer_id,
            related_gap_id=knowledge_update.related_gap_id,
            related_delta_id=knowledge_update.related_delta_id,
            from_state=_enum_value(knowledge_update.from_state),
            to_state=_enum_value(knowledge_update.to_state),
            evidence_refs_json=_json(knowledge_update.evidence_refs),
            chapter_number=int(knowledge_update.chapter_number or 0),
            story_time=knowledge_update.story_time,
            metadata_json=_json(knowledge_update.metadata),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def append_reader_experience_delta(
        self,
        reader_experience_delta: ReaderExperienceDelta,
    ) -> ReaderExperienceDeltaRow:
        row = ReaderExperienceDeltaRow(
            project_id=reader_experience_delta.project_id,
            reader_experience_delta_id=reader_experience_delta.reader_experience_delta_id,
            chapter_number=reader_experience_delta.chapter_number,
            reader_state_before=reader_experience_delta.reader_state_before,
            reader_state_after=reader_experience_delta.reader_state_after,
            cognition_transition=reader_experience_delta.cognition_transition,
            payoff_type=reader_experience_delta.payoff_type,
            reward_tags_json=_json(reader_experience_delta.reward_tags),
            emotional_effect=reader_experience_delta.emotional_effect,
            promise_debt_change=reader_experience_delta.promise_debt_change,
            next_desire=reader_experience_delta.next_desire,
            fairness_evidence_json=_json(reader_experience_delta.fairness_evidence),
            source_refs_json=_json(reader_experience_delta.source_refs),
            metadata_json=_json(reader_experience_delta.metadata),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get_snapshot_as_of_chapter(
        self,
        project_id: str,
        chapter_number: int,
    ) -> WorldModelSnapshot:
        stmt = (
            select(WorldModelSnapshotV4Row)
            .where(
                WorldModelSnapshotV4Row.project_id == project_id,
                WorldModelSnapshotV4Row.as_of_chapter <= chapter_number,
            )
            .order_by(
                WorldModelSnapshotV4Row.as_of_chapter.desc(),
                WorldModelSnapshotV4Row.rebuilt_at.desc(),
                WorldModelSnapshotV4Row.id.desc(),
            )
            .limit(1)
        )
        row = self.session.execute(stmt).scalar_one()
        return WorldModelSnapshot(
            snapshot_id=row.snapshot_id,
            project_id=row.project_id,
            as_of_chapter=row.as_of_chapter,
            as_of_story_time=row.as_of_story_time,
            active_world_line_ids=json.loads(row.active_world_line_ids_json or "[]"),
            open_gap_ids=json.loads(row.open_gap_ids_json or "[]"),
            objective_state_summary=row.objective_state_summary,
            source_delta_ids=json.loads(row.source_delta_ids_json or "[]"),
            metadata=json.loads(row.metadata_json or "{}"),
        )
