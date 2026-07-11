from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.book_state.repository import BookStateRepository
from forwin.audit.events import DecisionEventType
from forwin.models.base import new_id
from forwin.models.entity import Entity, EntityAlias
from forwin.models.audit import DecisionEvent
from forwin.naming import EntityAdmissionDecision, EntityAdmissionPlan


class EntityAdmissionCommitter:
    """Apply a verified EntityAdmissionPlan inside the Canon transaction."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def apply(
        self,
        *,
        project_id: str,
        plan: EntityAdmissionPlan,
    ) -> None:
        if plan.project_id != project_id:
            raise ValueError("Entity admission project mismatch")
        if plan.blocked:
            raise ValueError(
                "Blocked entity admission plan reached Canon: "
                + ", ".join(plan.plan_conflicts)
            )
        for decision in plan.decisions:
            if decision.action == "background_generic":
                self._record_event(
                    project_id=project_id,
                    chapter_number=plan.chapter_number,
                    event_type=DecisionEventType.ENTITY_BACKGROUND_GENERIC,
                    summary=f"实体注册器判定「{decision.mention_name}」为背景泛指。",
                    payload=decision.model_dump(mode="json"),
                )
                continue
            if decision.action == "register_character":
                entity = self._register_character(
                    project_id=project_id,
                    chapter_number=plan.chapter_number,
                    decision=decision,
                )
            elif decision.action == "register_alias":
                entity = self._require_or_create_registry_character(
                    project_id=project_id,
                    entity_id=decision.entity_id,
                )
            else:
                raise ValueError(
                    f"Blocked entity admission decision reached Canon: {decision.mention_name}"
                )
            for alias in decision.aliases:
                if alias and alias != entity.name:
                    self._register_alias(
                        project_id=project_id,
                        entity=entity,
                        alias=alias,
                        chapter_number=plan.chapter_number,
                    )

    def _register_character(
        self,
        *,
        project_id: str,
        chapter_number: int,
        decision: EntityAdmissionDecision,
    ) -> Entity:
        existing = self.session.get(Entity, decision.entity_id)
        entity = self._require_or_create_registry_character(
            project_id=project_id,
            entity_id=decision.entity_id,
        )
        if entity.name != decision.canonical_name:
            raise ValueError(f"Entity admission id conflict: {decision.mention_name}")
        if existing is None:
            self._record_event(
                project_id=project_id,
                chapter_number=chapter_number,
                event_type=DecisionEventType.ENTITY_REGISTERED,
                summary=f"实体注册器注册新角色「{entity.name}」。",
                payload=decision.model_dump(mode="json"),
                related_object_id=entity.id,
            )
        return entity

    def _register_alias(
        self,
        *,
        project_id: str,
        entity: Entity,
        alias: str,
        chapter_number: int,
    ) -> None:
        exact_owner = (
            self.session.execute(
                select(Entity).where(
                    Entity.project_id == project_id,
                    Entity.name == alias,
                )
            )
            .scalars()
            .first()
        )
        if exact_owner is not None and exact_owner.id != entity.id:
            raise ValueError(f'Entity alias "{alias}" conflicts with an entity name')
        existing = self.session.execute(
            select(EntityAlias).where(
                EntityAlias.project_id == project_id,
                EntityAlias.alias == alias,
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.entity_id != entity.id:
                raise ValueError(f'Entity alias "{alias}" belongs to another entity')
            self._sync_alias_json(entity, alias)
            return
        self.session.add(
            EntityAlias(
                id=new_id(),
                entity_id=entity.id,
                project_id=project_id,
                alias=alias,
            )
        )
        self._sync_alias_json(entity, alias)
        self.session.flush()
        self._record_event(
            project_id=project_id,
            chapter_number=chapter_number,
            event_type=DecisionEventType.ENTITY_ALIAS_REGISTERED,
            summary=f"实体注册器注册「{entity.name}」的别名「{alias}」。",
            payload={
                "entity_id": entity.id,
                "entity_name": entity.name,
                "alias": alias,
            },
            related_object_id=entity.id,
        )

    def _require_or_create_registry_character(
        self,
        *,
        project_id: str,
        entity_id: str,
    ) -> Entity:
        node = BookStateRepository(self.session).get_world_node(entity_id)
        if (
            node is None
            or node.project_id != project_id
            or str(node.node_type) != "character"
            or not node.is_active
        ):
            raise ValueError(f"Entity admission BookState target missing: {entity_id}")
        entity = self.session.get(Entity, entity_id)
        if entity is None:
            name_owner = (
                self.session.execute(
                    select(Entity).where(
                        Entity.project_id == project_id,
                        Entity.name == node.name,
                    )
                )
                .scalars()
                .first()
            )
            if name_owner is not None:
                raise ValueError(f'Entity name "{node.name}" already exists')
            entity = Entity(
                id=node.id,
                project_id=project_id,
                kind="character",
                name=node.name,
                aliases_json=json.dumps(node.aliases, ensure_ascii=False),
                description=node.description or node.summary,
                importance=node.importance,
                created_at_chapter=node.created_at_chapter,
                is_active=True,
            )
            self.session.add(entity)
            self.session.flush()
        if (
            entity.project_id != project_id
            or entity.kind != "character"
            or entity.name != node.name
            or not entity.is_active
        ):
            raise ValueError(f"Entity admission registry conflict: {entity_id}")
        return entity

    def _sync_alias_json(self, entity: Entity, alias: str) -> None:
        entity.aliases_json = json.dumps(
            _dedupe([*_json_list(entity.aliases_json), alias]),
            ensure_ascii=False,
        )
        self.session.add(entity)

    def _record_event(
        self,
        *,
        project_id: str,
        chapter_number: int,
        event_type: str,
        summary: str,
        payload: dict[str, Any],
        related_object_id: str = "",
    ) -> None:
        self.session.add(
            DecisionEvent(
                id=new_id(),
                project_id=project_id,
                chapter_number=int(chapter_number),
                scope="chapter",
                event_family="runtime_observation",
                event_type=event_type,
                actor_type="system",
                summary=summary,
                payload_json=json.dumps(payload, ensure_ascii=False),
                related_object_type="entity" if related_object_id else "",
                related_object_id=related_object_id,
            )
        )


def _json_list(raw: str) -> list[str]:
    try:
        payload = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [str(item or "").strip() for item in payload if str(item or "").strip()]


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = str(item or "").strip()
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


__all__ = ["EntityAdmissionCommitter"]
