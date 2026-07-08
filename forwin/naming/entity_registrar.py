from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from forwin.governance import DecisionEventType
from forwin.models.base import new_id
from forwin.models.entity import Entity, EntityAlias
from forwin.models.governance import DecisionEvent
from forwin.protocol.subworld import EntityMention
from forwin.protocol.writer import WriterOutput
from forwin.utils import parse_llm_json

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EntityRegistrationResult:
    writer_output: WriterOutput
    registered_names: list[str] = field(default_factory=list)
    alias_names: list[str] = field(default_factory=list)
    background_generic_names: list[str] = field(default_factory=list)
    plan_conflicts: list[str] = field(default_factory=list)


class LLMEntityRegistrationClassifier:
    def __init__(self, llm_client: Any) -> None:
        self.llm_client = llm_client

    def classify(
        self,
        *,
        project_id: str,
        chapter_number: int,
        names: list[str],
        writer_output: WriterOutput,
        existing_entities: list[Entity],
    ) -> list[dict[str, Any]]:
        if self.llm_client is None:
            raise RuntimeError("entity registrar has no LLM client")
        entity_rows = [
            {
                "entity_id": entity.id,
                "name": entity.name,
                "kind": entity.kind,
                "aliases": _json_list(entity.aliases_json),
                "description": entity.description,
            }
            for entity in existing_entities
            if str(entity.kind or "") == "character"
        ]
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 ForWin 命名实体注册器。只输出 JSON。"
                    "对每个 unknown_names 项给出 decision: register_character, "
                    "register_alias, background_generic, plan_conflict。"
                    "register_character 需要 canonical_name, aliases, role_hint, gender。"
                    "register_alias 需要 entity_id 和 aliases。"
                    "background_generic 不入实体表。plan_conflict 表示与计划或 canon 冲突。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "project_id": project_id,
                        "chapter_number": chapter_number,
                        "unknown_names": names,
                        "title": writer_output.title,
                        "body_excerpt": str(writer_output.body or "")[:2400],
                        "summary": writer_output.end_of_chapter_summary,
                        "existing_characters": entity_rows[:80],
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        raw = self.llm_client.chat(
            messages,
            temperature=0.2,
            max_tokens=600,
            response_format={"type": "json_object"},
            task_family="entity_registration",
            stage_key="entity_registrar",
        )
        payload = parse_llm_json(raw, error_prefix="EntityRegistrar JSON parser")
        decisions = payload.get("decisions") if isinstance(payload, dict) else payload
        if not isinstance(decisions, list):
            raise ValueError("EntityRegistrar JSON missing decisions list")
        return [item for item in decisions if isinstance(item, dict)]


class EntityRegistrar:
    def __init__(self, *, session: Session, classifier: Any | None = None) -> None:
        self.session = session
        self.classifier = classifier

    def register_writer_output(
        self,
        *,
        project_id: str,
        chapter_number: int,
        writer_output: WriterOutput,
    ) -> EntityRegistrationResult:
        names = self._unknown_named_references(
            project_id=project_id,
            writer_output=writer_output,
        )
        if not names:
            return EntityRegistrationResult(writer_output=writer_output)
        decisions = self._classify(
            project_id=project_id,
            chapter_number=chapter_number,
            names=names,
            writer_output=writer_output,
        )
        registered: list[str] = []
        aliases: list[str] = []
        background: list[str] = []
        conflicts: list[str] = []
        background_set: set[str] = set()
        for decision in decisions:
            action = str(decision.get("decision") or decision.get("action") or "").strip()
            name = str(decision.get("name") or decision.get("entity_name") or "").strip()
            if not name:
                continue
            if action == "register_character":
                entity = self._register_character(
                    project_id=project_id,
                    chapter_number=chapter_number,
                    decision=decision,
                    fallback_name=name,
                )
                registered.append(entity.name)
            elif action == "register_alias":
                alias_name = self._register_alias(
                    project_id=project_id,
                    decision=decision,
                    fallback_name=name,
                )
                if alias_name:
                    aliases.append(alias_name)
            elif action == "background_generic":
                background.append(name)
                background_set.add(name)
                self._record_event(
                    project_id=project_id,
                    chapter_number=chapter_number,
                    event_type=DecisionEventType.ENTITY_BACKGROUND_GENERIC,
                    summary=f"实体注册器判定「{name}」为背景泛指。",
                    payload={"name": name, "reason": str(decision.get("reason") or "")},
                )
            else:
                conflicts.append(name)
                self._record_event(
                    project_id=project_id,
                    chapter_number=chapter_number,
                    event_type=DecisionEventType.ENTITY_PLAN_CONFLICT,
                    summary=f"实体注册器判定「{name}」与计划或 canon 冲突。",
                    payload={"name": name, "reason": str(decision.get("reason") or ""), "decision": action},
                )
        updated_output = self._drop_background_generic_mentions(writer_output, background_set)
        return EntityRegistrationResult(
            writer_output=updated_output,
            registered_names=registered,
            alias_names=aliases,
            background_generic_names=background,
            plan_conflicts=conflicts,
        )

    def _unknown_named_references(self, *, project_id: str, writer_output: WriterOutput) -> list[str]:
        mentioned: list[str] = []
        seen: set[str] = set()
        for mention in getattr(writer_output, "entity_mentions", []) or []:
            if not bool(getattr(mention, "is_named", False)):
                continue
            if not bool(getattr(mention, "is_on_stage", True)):
                continue
            kind = str(getattr(mention, "entity_kind", "") or "").strip()
            if kind not in {"character", "person", "human"}:
                continue
            name = str(getattr(mention, "entity_name", "") or "").strip()
            if name and name not in seen:
                mentioned.append(name)
                seen.add(name)
        if not mentioned:
            return []
        known = self._entities_by_names(project_id, mentioned)
        return [name for name in mentioned if name not in known]

    def _classify(
        self,
        *,
        project_id: str,
        chapter_number: int,
        names: list[str],
        writer_output: WriterOutput,
    ) -> list[dict[str, Any]]:
        if self.classifier is None:
            return [
                {
                    "decision": "plan_conflict",
                    "name": name,
                    "reason": "entity registrar classifier unavailable",
                }
                for name in names
            ]
        existing_entities = list(
            self.session.execute(
                select(Entity).where(Entity.project_id == project_id, Entity.is_active == True)  # noqa: E712
            ).scalars().all()
        )
        try:
            raw = self.classifier.classify(
                project_id=project_id,
                chapter_number=chapter_number,
                names=names,
                writer_output=writer_output,
                existing_entities=existing_entities,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Entity registrar classifier failed; routing names to plan_conflict.", exc_info=True)
            return [
                {
                    "decision": "plan_conflict",
                    "name": name,
                    "reason": f"classifier_error:{type(exc).__name__}",
                }
                for name in names
            ]
        decisions = [item for item in raw if isinstance(item, dict)]
        decided_names = {str(item.get("name") or item.get("entity_name") or "").strip() for item in decisions}
        for name in names:
            if name not in decided_names:
                decisions.append(
                    {
                        "decision": "plan_conflict",
                        "name": name,
                        "reason": "classifier omitted this name",
                    }
                )
        return decisions

    def _register_character(
        self,
        *,
        project_id: str,
        chapter_number: int,
        decision: dict[str, Any],
        fallback_name: str,
    ) -> Entity:
        canonical = str(decision.get("canonical_name") or fallback_name).strip()
        entity = self._entities_by_names(project_id, [canonical]).get(canonical)
        aliases = _dedupe(
            [
                *[str(item or "").strip() for item in decision.get("aliases", []) if str(item or "").strip()],
                fallback_name,
            ]
        )
        if entity is None:
            entity = Entity(
                id=new_id(),
                project_id=project_id,
                kind="character",
                name=canonical,
                aliases_json=json.dumps(
                    [alias for alias in aliases if alias and alias != canonical],
                    ensure_ascii=False,
                ),
                description=str(decision.get("role_hint") or ""),
                importance=int(decision.get("importance") or 5),
                created_at_chapter=int(chapter_number or 0),
                is_active=True,
            )
            self.session.add(entity)
            self.session.flush()
            self._record_event(
                project_id=project_id,
                chapter_number=chapter_number,
                event_type=DecisionEventType.ENTITY_REGISTERED,
                summary=f"实体注册器注册新角色「{canonical}」。",
                payload={"name": canonical, "aliases": aliases, "decision": decision},
                related_object_id=entity.id,
            )
        for alias in aliases:
            if alias and alias != canonical:
                self._add_alias(project_id=project_id, entity=entity, alias=alias, chapter_number=chapter_number)
        return entity

    def _register_alias(self, *, project_id: str, decision: dict[str, Any], fallback_name: str) -> str:
        entity_id = str(decision.get("entity_id") or "").strip()
        entity = self.session.get(Entity, entity_id) if entity_id else None
        if entity is None or entity.project_id != project_id:
            canonical = str(decision.get("canonical_name") or "").strip()
            entity = self._entities_by_names(project_id, [canonical]).get(canonical)
        if entity is None:
            self._record_event(
                project_id=project_id,
                chapter_number=0,
                event_type=DecisionEventType.ENTITY_PLAN_CONFLICT,
                summary=f"实体注册器无法为「{fallback_name}」找到别名目标。",
                payload={"name": fallback_name, "decision": decision},
            )
            return ""
        aliases = _dedupe(
            [
                *[str(item or "").strip() for item in decision.get("aliases", []) if str(item or "").strip()],
                fallback_name,
            ]
        )
        for alias in aliases:
            if alias and alias != entity.name:
                self._add_alias(project_id=project_id, entity=entity, alias=alias, chapter_number=0)
        return aliases[0] if aliases else fallback_name

    def _add_alias(self, *, project_id: str, entity: Entity, alias: str, chapter_number: int) -> None:
        existing = self.session.execute(
            select(EntityAlias).where(EntityAlias.project_id == project_id, EntityAlias.alias == alias)
        ).scalar_one_or_none()
        if existing is not None:
            if existing.entity_id != entity.id:
                self._record_event(
                    project_id=project_id,
                    chapter_number=chapter_number,
                    event_type=DecisionEventType.ENTITY_ALIAS_CONFLICT,
                    summary=f"实体别名「{alias}」已绑定到其他实体。",
                    payload={"alias": alias, "existing_entity_id": existing.entity_id, "target_entity_id": entity.id},
                )
            return
        self.session.add(
            EntityAlias(
                id=new_id(),
                entity_id=entity.id,
                project_id=project_id,
                alias=alias,
            )
        )
        entity.aliases_json = json.dumps(_dedupe([*_json_list(entity.aliases_json), alias]), ensure_ascii=False)
        self.session.add(entity)
        try:
            self.session.flush()
        except IntegrityError:
            self.session.rollback()
            raise
        self._record_event(
            project_id=project_id,
            chapter_number=chapter_number,
            event_type=DecisionEventType.ENTITY_ALIAS_REGISTERED,
            summary=f"实体注册器注册「{entity.name}」的别名「{alias}」。",
            payload={"entity_id": entity.id, "entity_name": entity.name, "alias": alias},
            related_object_id=entity.id,
        )

    def _entities_by_names(self, project_id: str, names: list[str]) -> dict[str, Entity]:
        normalized = [str(name or "").strip() for name in names if str(name or "").strip()]
        if not normalized:
            return {}
        mapping: dict[str, Entity] = {}
        exact_rows = self.session.execute(
            select(Entity).where(Entity.project_id == project_id, Entity.name.in_(normalized))
        ).scalars().all()
        for entity in exact_rows:
            mapping[entity.name] = entity
        unresolved = [name for name in normalized if name not in mapping]
        if not unresolved:
            return mapping
        alias_rows = self.session.execute(
            select(EntityAlias.alias, Entity)
            .join(Entity, EntityAlias.entity_id == Entity.id)
            .where(
                Entity.project_id == project_id,
                EntityAlias.project_id == project_id,
                EntityAlias.alias.in_(unresolved),
            )
        ).all()
        for alias, entity in alias_rows:
            mapping[str(alias)] = entity
        return mapping

    def _drop_background_generic_mentions(
        self,
        writer_output: WriterOutput,
        names: set[str],
    ) -> WriterOutput:
        if not names:
            return writer_output
        mentions = [
            mention
            for mention in writer_output.entity_mentions
            if str(getattr(mention, "entity_name", "") or "").strip() not in names
        ]
        return writer_output.model_copy(update={"entity_mentions": mentions})

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
                chapter_number=int(chapter_number or 0),
                scope="chapter" if int(chapter_number or 0) else "project",
                event_family="runtime_observation",
                event_type=event_type,
                actor_type="system",
                summary=summary,
                payload_json=json.dumps(payload, ensure_ascii=False),
                related_object_type="entity" if related_object_id else "",
                related_object_id=related_object_id,
            )
        )
        self.session.flush()


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
