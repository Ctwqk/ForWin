from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from forwin.book_state import BookStateRepository
from forwin.governance import DecisionEventType
from forwin.models import DecisionEvent
from forwin.models.base import new_id
from forwin.models.entity import RelationEdge
from forwin.personality.library import CharacterPersonalityLibrary
from forwin.personality.models import PersonalityLoadout, PersonalitySkillRef
from forwin.personality.policy import CharacterPersonalityPolicyResolver
from forwin.protocol.book_state import WorldNode


class RelationshipPersonalityEnricher:
    def __init__(
        self,
        session: Session,
        *,
        personality_library: CharacterPersonalityLibrary | None = None,
    ) -> None:
        self.session = session
        self.repo = BookStateRepository(session)
        self.library = personality_library or CharacterPersonalityLibrary()

    def enrich_relation(self, relation: RelationEdge, *, reason: str = "relationship changed") -> dict[str, Any]:
        policy = CharacterPersonalityPolicyResolver(self.session).resolve_for_project(relation.project_id)
        if not policy.relationship_enrichment_enabled:
            return {"enriched": 0, "skipped": "policy_disabled", "diffs": []}
        skill_id = self._skill_for_relation(relation)
        if not skill_id:
            return {"enriched": 0, "skipped": "no_matching_rule", "diffs": []}
        skill = self.library.get(skill_id)
        if skill is None or skill.incomplete:
            return {"enriched": 0, "skipped": "skill_unavailable", "diffs": []}

        source = self._node_for_legacy_entity_id(relation.project_id, relation.source_entity_id)
        target = self._node_for_legacy_entity_id(relation.project_id, relation.target_entity_id)
        if source is None or target is None:
            return {"enriched": 0, "skipped": "character_mapping_missing", "diffs": []}

        diffs: list[dict[str, Any]] = []
        for node, target_node in ((source, target), (target, source)):
            diff = self._add_pattern(node, target_node.id, skill_id)
            if diff:
                diffs.append(diff)
        if diffs:
            self._save_event(relation, skill_id=skill_id, diffs=diffs, reason=reason)
        return {"enriched": len(diffs), "skill": skill_id, "diffs": diffs}

    def enrich_project(self, project_id: str, *, reason: str = "manual relationship enrichment") -> dict[str, Any]:
        relations = (
            self.session.query(RelationEdge)
            .filter(RelationEdge.project_id == project_id, RelationEdge.is_active.is_(True))
            .all()
        )
        items = [self.enrich_relation(relation, reason=reason) for relation in relations]
        return {
            "project_id": project_id,
            "scanned": len(relations),
            "enriched": sum(int(item.get("enriched") or 0) for item in items),
            "items": items,
        }

    def _node_for_legacy_entity_id(self, project_id: str, entity_id: str) -> WorldNode | None:
        normalized = str(entity_id or "").strip()
        if not normalized:
            return None
        for node in self.repo.list_world_nodes(project_id):
            if str(node.node_type) != "character":
                continue
            metadata = node.metadata if isinstance(node.metadata, dict) else {}
            if str(metadata.get("legacy_entity_id") or "") == normalized:
                return node
        return None

    def _add_pattern(self, node: WorldNode, target_character_id: str, skill_id: str) -> dict[str, Any]:
        metadata = dict(node.metadata) if isinstance(node.metadata, dict) else {}
        assignment = metadata.get("personality_assignment") if isinstance(metadata, dict) else {}
        if isinstance(assignment, dict) and assignment.get("manual_override"):
            return {}
        profile = dict(node.profile) if isinstance(node.profile, dict) else {}
        loadout = PersonalityLoadout.model_validate(profile.get("personality_loadout") or {})
        if loadout.dominant is None:
            return {}
        for ref in loadout.relationship_patterns:
            if ref.skill == skill_id and ref.target == target_character_id:
                return {}
        old_loadout = _compact_loadout(loadout)
        loadout.relationship_patterns.append(
            PersonalitySkillRef(skill=skill_id, weight=0.48, target=target_character_id)
        )
        new_loadout = _compact_loadout(loadout)
        profile["personality_loadout"] = new_loadout
        updated = node.model_copy(update={"profile": profile})
        self.repo.create_world_node(updated)
        return {
            "character_id": node.id,
            "target_character_id": target_character_id,
            "added_skill_id": skill_id,
            "old_loadout": old_loadout,
            "new_loadout": new_loadout,
        }

    def _skill_for_relation(self, relation: RelationEdge) -> str:
        text = f"{relation.relation_type}\n{relation.description}".lower()
        if any(keyword in text for keyword in ("rival", "对手", "竞争")):
            return "rel-rival-respect"
        if any(keyword in text for keyword in ("mentor", "导师", "师父", "扶持", "保护")):
            return "rel-mentor-protector"
        return ""

    def _save_event(
        self,
        relation: RelationEdge,
        *,
        skill_id: str,
        diffs: list[dict[str, Any]],
        reason: str,
    ) -> None:
        row = DecisionEvent(
            id=new_id(),
            project_id=relation.project_id,
            scope="character_creation",
            event_family="business_event",
            event_type=DecisionEventType.PERSONALITY_RELATIONSHIP_ENRICHED,
            actor_type="system",
            summary="根据关系边补充人物 relationship personality pattern。",
            reason=reason,
            payload_json=json.dumps(
                {
                    "stage": "relationship_personality_enrichment",
                    "status": "updated",
                    "relation_edge_id": relation.id,
                    "relation_type": relation.relation_type,
                    "selected_skill_id": skill_id,
                    "diffs": diffs,
                },
                ensure_ascii=False,
            ),
            related_object_type="relation_edge",
            related_object_id=relation.id,
        )
        self.session.add(row)
        self.session.flush()
        row.causal_root_id = row.id
        self.session.add(row)
        self.session.flush()


def _compact_loadout(loadout: PersonalityLoadout) -> dict[str, Any]:
    payload = loadout.model_dump(mode="json", exclude_none=True)
    refs: list[dict[str, Any]] = []
    dominant = payload.get("dominant")
    if isinstance(dominant, dict):
        refs.append(dominant)
    for key in ("secondary", "social_mask", "stress_modes", "relationship_patterns"):
        values = payload.get(key)
        if isinstance(values, list):
            refs.extend(item for item in values if isinstance(item, dict))
    for ref in refs:
        for key in ("active_when", "trigger", "target"):
            if ref.get(key) in (None, "", []):
                ref.pop(key, None)
    return payload
