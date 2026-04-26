from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.book_state.repository import BookStateRepository, _loads
from forwin.models.entity import Entity, EntityState, RelationEdge
from forwin.models.world_v4 import (
    KnowledgeGapRow,
    ReaderExperienceDeltaRow,
    RevealEventRow,
    WorldLineRow,
)
from forwin.protocol.book_state import (
    NarrativeNode,
    WORLD_EDGE_TYPES_BY_FAMILY,
    WorldEdge,
    WorldNode,
)


_NODE_TYPE_MAP = {
    "character": "character",
    "person": "character",
    "actor": "character",
    "faction": "faction",
    "organization": "faction",
    "org": "faction",
    "group": "group",
    "team": "group",
    "item": "item",
    "artifact": "item",
    "resource": "resource",
    "ability": "ability",
    "skill": "ability",
    "rule": "rule",
    "law": "rule",
    "activity": "activity",
    "site": "site_state",
    "location": "site_state",
    "place": "site_state",
    "event": "event",
    "fact": "fact",
    "objective": "objective",
}

_RELATION_TYPE_MAP = {
    "ally": "ally_of",
    "allied_with": "ally_of",
    "enemy": "enemy_of",
    "hostile_to": "enemy_of",
    "mentor": "mentor_of",
    "member": "member_of",
    "member_of": "member_of",
    "leader": "leader_of",
    "leader_of": "leader_of",
    "owns": "owns",
    "owner_of": "owns",
    "possesses": "possesses",
    "equipped_with": "equipped_with",
    "controls": "controls",
    "protects": "protects",
    "trusts": "trusts",
    "distrusts": "distrusts",
    "requires": "requires",
    "causes": "causes",
    "supports": "supports",
    "contradicts": "contradicts",
    "targets": "targets",
}


class LegacyBookStateImporter:
    """Best-effort importer from existing entity/V4 rows into BookState tables."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = BookStateRepository(session)

    def import_project(self, project_id: str) -> dict[str, int]:
        counts = {
            "world_nodes": 0,
            "world_node_states": 0,
            "world_edges": 0,
            "skipped_relation_edges": 0,
            "narrative_nodes": 0,
        }
        counts["world_nodes"] += self._import_entities(project_id)
        counts["world_node_states"] += self._import_entity_states(project_id)
        edge_counts = self._import_relation_edges(project_id)
        counts["world_edges"] += edge_counts["imported"]
        counts["skipped_relation_edges"] += edge_counts["skipped"]
        counts["narrative_nodes"] += self._import_v4_narrative_nodes(project_id)
        self.session.flush()
        return counts

    def _import_entities(self, project_id: str) -> int:
        count = 0
        rows = list(
            self.session.execute(
                select(Entity)
                .where(Entity.project_id == project_id)
                .order_by(Entity.created_at_chapter.asc(), Entity.id.asc())
            )
            .scalars()
            .all()
        )
        for row in rows:
            node = WorldNode(
                id=row.id,
                project_id=row.project_id,
                node_type=_node_type(row.kind),
                name=row.name,
                aliases=_loads(row.aliases_json, []),
                description=row.description,
                importance=row.importance,
                created_at_chapter=row.created_at_chapter,
                is_active=row.is_active,
                metadata={"legacy_entity_kind": row.kind},
            )
            self.repo.create_world_node(node)
            count += 1
        return count

    def _import_entity_states(self, project_id: str) -> int:
        entities = {
            row.id: row
            for row in self.session.execute(
                select(Entity).where(Entity.project_id == project_id)
            )
            .scalars()
            .all()
        }
        if not entities:
            return 0
        count = 0
        rows = list(
            self.session.execute(
                select(EntityState)
                .where(EntityState.entity_id.in_(list(entities)))
                .order_by(EntityState.as_of_chapter.asc(), EntityState.id.asc())
            )
            .scalars()
            .all()
        )
        for row in rows:
            entity = entities.get(row.entity_id)
            if entity is None:
                continue
            self.repo.append_world_node_state(
                project_id=project_id,
                node_id=row.entity_id,
                node_type=_node_type(entity.kind),
                as_of_chapter=row.as_of_chapter,
                state=_loads(row.state_json, {}),
                source_delta_id="legacy_entity_state",
            )
            count += 1
        return count

    def _import_relation_edges(self, project_id: str) -> dict[str, int]:
        imported = 0
        skipped = 0
        rows = list(
            self.session.execute(
                select(RelationEdge)
                .where(RelationEdge.project_id == project_id)
                .order_by(RelationEdge.established_at_chapter.asc(), RelationEdge.id.asc())
            )
            .scalars()
            .all()
        )
        for row in rows:
            edge_type = _relation_type(row.relation_type)
            family = _edge_family(edge_type)
            if family is None:
                skipped += 1
                continue
            edge = WorldEdge(
                id=row.id,
                project_id=row.project_id,
                source_id=row.source_entity_id,
                target_id=row.target_entity_id,
                edge_type=edge_type,
                edge_family=family,
                established_at_chapter=row.established_at_chapter,
                ended_at_chapter=row.ended_at_chapter,
                is_active=row.is_active,
                state={"description": row.description} if row.description else {},
                metadata={"legacy_relation_type": row.relation_type},
            )
            self.repo.create_world_edge(edge)
            imported += 1
        return {"imported": imported, "skipped": skipped}

    def _import_v4_narrative_nodes(self, project_id: str) -> int:
        count = 0
        for row in self.session.execute(
            select(WorldLineRow)
            .where(WorldLineRow.project_id == project_id)
            .order_by(WorldLineRow.created_at.asc(), WorldLineRow.id.asc())
        ).scalars():
            self.repo.create_narrative_node(
                NarrativeNode(
                    id=row.world_line_id,
                    project_id=project_id,
                    node_type="world_line",
                    title=row.title,
                    status="active",
                    payload={
                        "line_type": row.line_type,
                        "participants": _loads(row.participants_json, []),
                        "objective_state_summary": row.objective_state_summary,
                        "planned_reveal_chapter": row.planned_reveal_chapter,
                        "long_term_promise": row.long_term_promise,
                        "source_refs": _loads(row.source_refs_json, []),
                    },
                    metadata={"legacy_table": "world_lines", **_loads(row.metadata_json, {})},
                )
            )
            count += 1
        for row in self.session.execute(
            select(KnowledgeGapRow)
            .where(KnowledgeGapRow.project_id == project_id)
            .order_by(KnowledgeGapRow.created_at.asc(), KnowledgeGapRow.id.asc())
        ).scalars():
            self.repo.create_narrative_node(
                NarrativeNode(
                    id=row.gap_id,
                    project_id=project_id,
                    node_type="knowledge_gap",
                    title=row.objective_truth,
                    status=row.status,
                    payload={
                        "objective_truth": row.objective_truth,
                        "related_world_line_id": row.related_world_line_id,
                        "observer_states": _loads(row.observer_states_json, {}),
                        "narrative_function": row.narrative_function,
                        "planned_closure": row.planned_closure,
                    },
                    metadata={"legacy_table": "knowledge_gaps", **_loads(row.metadata_json, {})},
                )
            )
            count += 1
        for row in self.session.execute(
            select(RevealEventRow)
            .where(RevealEventRow.project_id == project_id)
            .order_by(RevealEventRow.created_at.asc(), RevealEventRow.id.asc())
        ).scalars():
            self.repo.create_narrative_node(
                NarrativeNode(
                    id=row.reveal_event_id,
                    project_id=project_id,
                    node_type="reveal_plan",
                    title=row.narrative_function or row.reveal_method,
                    status="active",
                    payload={
                        "reveals_fact_id": row.reveals_fact_id,
                        "reveals_delta_id": row.reveals_delta_id,
                        "related_gap_id": row.related_gap_id,
                        "reveal_to_reader": row.reveal_to_reader,
                        "reveal_to_characters": _loads(row.reveal_to_characters_json, []),
                        "from_state": row.from_state,
                        "to_state": row.to_state,
                        "fairness_evidence": _loads(row.fairness_evidence_json, []),
                    },
                    metadata={"legacy_table": "reveal_events", **_loads(row.metadata_json, {})},
                )
            )
            count += 1
        for row in self.session.execute(
            select(ReaderExperienceDeltaRow)
            .where(ReaderExperienceDeltaRow.project_id == project_id)
            .order_by(ReaderExperienceDeltaRow.chapter_number.asc(), ReaderExperienceDeltaRow.id.asc())
        ).scalars():
            self.repo.create_narrative_node(
                NarrativeNode(
                    id=row.reader_experience_delta_id,
                    project_id=project_id,
                    node_type="promise",
                    title=row.next_desire or row.payoff_type,
                    status="active",
                    payload={
                        "chapter_number": row.chapter_number,
                        "reader_state_before": row.reader_state_before,
                        "reader_state_after": row.reader_state_after,
                        "cognition_transition": row.cognition_transition,
                        "promise_debt_change": row.promise_debt_change,
                        "reward_tags": _loads(row.reward_tags_json, []),
                    },
                    metadata={"legacy_table": "reader_experience_deltas", **_loads(row.metadata_json, {})},
                )
            )
            count += 1
        return count


def _node_type(kind: str) -> str:
    return _NODE_TYPE_MAP.get(str(kind or "").lower(), "group")


def _relation_type(relation_type: str) -> str:
    text = str(relation_type or "").lower()
    return _RELATION_TYPE_MAP.get(text, text)


def _edge_family(edge_type: str) -> str | None:
    for family, edge_types in WORLD_EDGE_TYPES_BY_FAMILY.items():
        if edge_type in edge_types:
            return family
    return None


__all__ = ["LegacyBookStateImporter"]
