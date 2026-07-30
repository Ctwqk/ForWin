from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from forwin.book_state.query import BookStateQuery
from forwin.checker.reference_classifier import (
    classify_reference,
    looks_like_generic_character_reference,
    looks_like_non_character_reference,
)
from forwin.llm.compat import call_chat_compat
from forwin.models.base import new_id
from forwin.models.entity import Entity
from forwin.observability.llm_trace import mark_latest_attempt_parse_failure
from forwin.protocol.context import EntitySnapshot
from forwin.protocol.writer import WriterOutput
from forwin.utils import parse_llm_json

from .types import EntityAdmissionDecision, EntityAdmissionPlan

logger = logging.getLogger(__name__)
_CHARACTER_KINDS = {"character", "person", "human"}
_ENTITY_ADMISSION_ACTIONS = {
    "register_character",
    "register_alias",
    "background_generic",
    "plan_conflict",
}
_ENTITY_ADMISSION_OUTPUT_SCHEMA: dict[str, Any] = {
    "title": "EntityAdmissionResponse",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "decision": {
                        "type": "string",
                        "enum": sorted(_ENTITY_ADMISSION_ACTIONS),
                    },
                    "entity_id": {"type": "string"},
                    "canonical_name": {"type": "string"},
                    "aliases": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "role_hint": {"type": "string"},
                    "importance": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                    },
                    "reason": {"type": "string"},
                },
                "required": ["name", "decision"],
            },
        }
    },
    "required": ["decisions"],
}


@dataclass(frozen=True)
class EntityAdmissionResult:
    writer_output: WriterOutput
    plan: EntityAdmissionPlan
    registered_names: list[str] = field(default_factory=list)
    alias_names: list[str] = field(default_factory=list)
    background_generic_names: list[str] = field(default_factory=list)
    plan_conflicts: list[str] = field(default_factory=list)


class LLMEntityAdmissionClassifier:
    def __init__(self, llm_client: Any, *, max_schema_retries: int = 1) -> None:
        self.llm_client = llm_client
        self.max_schema_retries = max(0, int(max_schema_retries))

    def classify(
        self,
        *,
        project_id: str,
        chapter_number: int,
        names: list[str],
        writer_output: WriterOutput,
        existing_entities: list[EntitySnapshot],
    ) -> list[dict[str, Any]]:
        if self.llm_client is None:
            raise RuntimeError("entity registrar has no LLM client")
        entity_rows = [
            {
                "entity_id": entity.entity_id,
                "name": entity.name,
                "kind": entity.kind,
                "aliases": list(entity.aliases),
                "description": entity.description,
            }
            for entity in existing_entities
            if str(entity.kind or "") == "character"
        ]
        base_messages = [
            {
                "role": "system",
                "content": (
                    "你是 ForWin 命名实体准入规划器。只输出 JSON。"
                    "顶层必须且只能是包含 decisions 数组的对象。"
                    "对每个 unknown_names 项给出 decision: register_character, "
                    "register_alias, background_generic, plan_conflict。"
                    "register_character 需要 canonical_name, aliases, role_hint。"
                    "register_alias 需要 aliases，以及 existing_characters 中的 "
                    "entity_id 或可唯一解析的 canonical_name；不得臆造别名目标。"
                    "background_generic 不入实体表。plan_conflict 表示与计划或 canon 冲突。"
                    "每条 decision 必须包含 name 字段，并完全复制对应 unknown_names 原值；"
                    "不得使用 unknown_name 或 entity_name 替代 name。"
                    "不得添加 unknown_names 之外的名字。"
                    "输出必须匹配以下 JSON Schema："
                    + json.dumps(
                        _ENTITY_ADMISSION_OUTPUT_SCHEMA,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "project_id": project_id,
                        "chapter_number": chapter_number,
                        "unknown_names": names,
                        "reference_candidates": [
                            {
                                "name": name,
                                "scope": classification.scope,
                                "features": list(classification.features),
                            }
                            for name in names
                            if (
                                classification := classify_reference(name)
                            ).scope
                            == "genre_candidate"
                        ],
                        "title": writer_output.title,
                        "body_excerpt": str(writer_output.body or "")[:2400],
                        "summary": writer_output.end_of_chapter_summary,
                        "mention_evidence": [
                            {
                                "name": name,
                                "quotes": _mention_quotes(
                                    str(writer_output.body or ""),
                                    name,
                                ),
                            }
                            for name in names
                        ],
                        "existing_characters": entity_rows[:80],
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        last_raw = ""
        last_error = ""
        for attempt_index in range(self.max_schema_retries + 1):
            stage_key = (
                "entity_registrar"
                if attempt_index == 0
                else "entity_registrar_json_repair"
            )
            messages = (
                base_messages
                if attempt_index == 0
                else _entity_admission_repair_messages(
                    base_messages=base_messages,
                    previous_raw=last_raw,
                    validation_error=last_error,
                )
            )
            raw = call_chat_compat(
                self.llm_client,
                messages,
                temperature=0.2 if attempt_index == 0 else 0.0,
                max_tokens=600,
                response_format={"type": "json_object"},
                output_schema=_ENTITY_ADMISSION_OUTPUT_SCHEMA,
                task_family="entity_admission",
                stage_key=stage_key,
            )
            try:
                payload = parse_llm_json(
                    raw,
                    error_prefix="EntityRegistrar JSON parser",
                )
                return _validate_entity_admission_decisions(
                    payload,
                    expected_names=names,
                    existing_entities=existing_entities,
                )
            except ValueError as exc:
                last_raw = str(raw or "")
                last_error = str(exc)
                mark_latest_attempt_parse_failure(
                    self.llm_client,
                    parser_name="EntityRegistrar",
                    stage_key=stage_key,
                    schema_name="entity_admission_response",
                    raw_output=last_raw,
                    error=exc,
                )
        raise ValueError(
            "EntityRegistrar schema invalid after "
            f"{self.max_schema_retries + 1} attempts: {last_error}"
        )


def _validate_entity_admission_decisions(
    payload: dict[str, Any],
    *,
    expected_names: list[str],
    existing_entities: list[EntitySnapshot],
) -> list[dict[str, Any]]:
    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("EntityRegistrar JSON missing decisions list")

    expected = set(expected_names)
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for index, item in enumerate(decisions):
        if not isinstance(item, dict):
            raise ValueError(
                f"EntityRegistrar decisions[{index}] must be an object"
            )
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError(
                f"EntityRegistrar decisions[{index}] missing name"
            )
        if name not in expected:
            raise ValueError(
                f"EntityRegistrar returned unexpected name: {name}"
            )
        if name in seen:
            raise ValueError(
                f"EntityRegistrar returned duplicate decision: {name}"
            )
        decision = str(item.get("decision") or "").strip()
        if decision not in _ENTITY_ADMISSION_ACTIONS:
            raise ValueError(
                f"EntityRegistrar returned invalid decision for {name}: {decision}"
            )
        aliases = item.get("aliases")
        if aliases is not None and (
            not isinstance(aliases, list)
            or any(not isinstance(alias, str) for alias in aliases)
        ):
            raise ValueError(
                f"EntityRegistrar returned invalid aliases for {name}"
            )
        if decision == "register_alias":
            _validate_alias_target(
                item,
                mention_name=name,
                existing_entities=existing_entities,
            )
        seen.add(name)
        validated.append(item)

    missing = [name for name in expected_names if name not in seen]
    if missing:
        raise ValueError(
            "EntityRegistrar omitted decisions for: " + ", ".join(missing)
        )
    return validated


def _validate_alias_target(
    item: dict[str, Any],
    *,
    mention_name: str,
    existing_entities: list[EntitySnapshot],
) -> None:
    entity_id = str(item.get("entity_id") or "").strip()
    if entity_id:
        if any(entity.entity_id == entity_id for entity in existing_entities):
            return
        raise ValueError(
            "EntityRegistrar returned unresolvable alias target for "
            f"{mention_name}: entity_id={entity_id}"
        )

    canonical_name = str(item.get("canonical_name") or "").strip()
    if not canonical_name:
        raise ValueError(
            "EntityRegistrar alias target is missing entity_id and "
            f"canonical_name for {mention_name}"
        )
    matching_ids = {
        entity.entity_id
        for entity in existing_entities
        if canonical_name == entity.name or canonical_name in entity.aliases
    }
    if len(matching_ids) != 1:
        resolution = "not found" if not matching_ids else "ambiguous"
        raise ValueError(
            "EntityRegistrar returned unresolvable alias target for "
            f"{mention_name}: canonical_name={canonical_name} ({resolution})"
        )


def _entity_admission_repair_messages(
    *,
    base_messages: list[dict[str, str]],
    previous_raw: str,
    validation_error: str,
) -> list[dict[str, str]]:
    return [
        *base_messages,
        {
            "role": "user",
            "content": (
                "上一次 JSON 不符合 EntityAdmissionResponse 契约。"
                "请只返回修正后的 JSON 对象，顶层只能包含 decisions 数组；"
                "必须为每个 unknown_names 项返回且仅返回一条 decision。\n\n"
                f"校验错误：\n{validation_error[:2000]}\n\n"
                f"上一次 JSON：\n{previous_raw[:6000]}"
            ),
        },
    ]


class EntityRegistrar:
    def __init__(self, *, session: Session, classifier: Any | None = None) -> None:
        self.session = session
        self.classifier = classifier
        self.book_state = BookStateQuery(session)

    def plan_writer_output(
        self,
        *,
        project_id: str,
        chapter_number: int,
        writer_output: WriterOutput,
    ) -> EntityAdmissionResult:
        unknown_names = self._unknown_named_references(
            project_id=project_id,
            writer_output=writer_output,
        )
        raw_decisions = self._classify(
            project_id=project_id,
            chapter_number=chapter_number,
            names=unknown_names,
            writer_output=writer_output,
        )
        as_of_chapter = max(int(chapter_number) - 1, 0)
        reserved_names: dict[str, str] = {}
        decisions: list[EntityAdmissionDecision] = []
        for raw_decision in raw_decisions:
            mention_name = str(
                raw_decision.get("name")
                or raw_decision.get("entity_name")
                or raw_decision.get("unknown_name")
                or ""
            ).strip()
            action = str(
                raw_decision.get("decision") or raw_decision.get("action") or ""
            ).strip()
            if action == "register_character":
                decision = self._plan_character_registration(
                    project_id=project_id,
                    mention_name=mention_name,
                    raw_decision=raw_decision,
                    reserved_names=reserved_names,
                    as_of_chapter=as_of_chapter,
                )
            elif action == "register_alias":
                decision = self._plan_alias_registration(
                    project_id=project_id,
                    mention_name=mention_name,
                    raw_decision=raw_decision,
                    reserved_names=reserved_names,
                    as_of_chapter=as_of_chapter,
                )
            elif action == "background_generic":
                decision = EntityAdmissionDecision(
                    mention_name=mention_name,
                    action="background_generic",
                    reason=str(raw_decision.get("reason") or ""),
                )
            else:
                decision = self._conflict_decision(
                    mention_name,
                    str(raw_decision.get("reason") or "plan or canon conflict"),
                )
            decisions.append(decision)

        background_names = [
            decision.mention_name
            for decision in decisions
            if decision.action == "background_generic"
        ]
        planned_output = self._drop_background_generic_references(
            writer_output,
            set(background_names),
        )
        conflicts = [
            decision.mention_name
            for decision in decisions
            if decision.action == "plan_conflict"
        ]
        plan = EntityAdmissionPlan(
            project_id=project_id,
            chapter_number=int(chapter_number),
            candidate_fingerprint=writer_output_admission_fingerprint(planned_output),
            decisions=decisions,
            plan_conflicts=conflicts,
        )
        planned_output = self._attach_admission_plan(planned_output, plan)
        return EntityAdmissionResult(
            writer_output=planned_output,
            plan=plan,
            registered_names=[
                decision.canonical_name
                for decision in decisions
                if decision.action == "register_character"
            ],
            alias_names=[
                decision.mention_name
                for decision in decisions
                if decision.action == "register_alias"
            ],
            background_generic_names=background_names,
            plan_conflicts=conflicts,
        )

    def verify_writer_output_admission(
        self,
        *,
        project_id: str,
        writer_output: WriterOutput,
    ) -> EntityAdmissionPlan:
        plan = _admission_plan_from_output(writer_output)
        if plan.project_id != project_id:
            raise ValueError("Entity admission project mismatch")
        if plan.chapter_number != int(writer_output.chapter_number):
            raise ValueError("Entity admission chapter mismatch")
        actual_fingerprint = writer_output_admission_fingerprint(writer_output)
        if plan.candidate_fingerprint != actual_fingerprint:
            raise ValueError("Entity admission plan is stale for this candidate")
        if plan.plan_conflicts:
            raise ValueError(
                "Entity admission rejected: " + ", ".join(plan.plan_conflicts)
            )
        decision_names = [decision.mention_name for decision in plan.decisions]
        if len(decision_names) != len(set(decision_names)):
            raise ValueError("Entity admission contains duplicate mention decisions")
        admitted_names = {
            decision.mention_name
            for decision in plan.decisions
            if decision.action != "plan_conflict"
        }
        unresolved = [
            name
            for name in self._unknown_named_references(
                project_id=project_id,
                writer_output=writer_output,
            )
            if name not in admitted_names
        ]
        if unresolved:
            raise ValueError(
                "Entity admission decision missing: " + ", ".join(unresolved)
            )
        self._revalidate_plan(project_id=project_id, plan=plan)
        return plan

    def _plan_character_registration(
        self,
        *,
        project_id: str,
        mention_name: str,
        raw_decision: dict[str, Any],
        reserved_names: dict[str, str],
        as_of_chapter: int,
    ) -> EntityAdmissionDecision:
        canonical_name = str(
            raw_decision.get("canonical_name") or mention_name
        ).strip()
        aliases = _dedupe(
            [
                *[
                    str(item or "").strip()
                    for item in raw_decision.get("aliases", [])
                    if str(item or "").strip()
                ],
                mention_name,
            ]
        )
        names_to_reserve = _dedupe([canonical_name, *aliases])
        conflict = self._name_conflict(
            project_id=project_id,
            names=names_to_reserve,
            target_entity_id="",
            reserved_names=reserved_names,
            mention_name=mention_name,
            as_of_chapter=as_of_chapter,
        )
        if not canonical_name:
            conflict = "missing canonical name"
        if conflict:
            return self._conflict_decision(mention_name, conflict)
        for name in names_to_reserve:
            reserved_names[name] = mention_name
        return EntityAdmissionDecision(
            mention_name=mention_name,
            action="register_character",
            entity_id=str(raw_decision.get("entity_id") or new_id()),
            canonical_name=canonical_name,
            aliases=[alias for alias in aliases if alias != canonical_name],
            role_hint=str(raw_decision.get("role_hint") or ""),
            importance=_importance(raw_decision.get("importance")),
            reason=str(raw_decision.get("reason") or ""),
        )

    def _plan_alias_registration(
        self,
        *,
        project_id: str,
        mention_name: str,
        raw_decision: dict[str, Any],
        reserved_names: dict[str, str],
        as_of_chapter: int,
    ) -> EntityAdmissionDecision:
        entity, error = self._resolve_character_target(
            project_id=project_id,
            entity_id=str(raw_decision.get("entity_id") or "").strip(),
            canonical_name=str(raw_decision.get("canonical_name") or "").strip(),
            as_of_chapter=as_of_chapter,
        )
        if entity is None:
            return self._conflict_decision(mention_name, error or "alias target not found")
        aliases = [
            alias
            for alias in _dedupe(
                [
                    *[
                        str(item or "").strip()
                        for item in raw_decision.get("aliases", [])
                        if str(item or "").strip()
                    ],
                    mention_name,
                ]
            )
            if alias != entity.name
        ]
        conflict = self._name_conflict(
            project_id=project_id,
            names=aliases,
            target_entity_id=entity.entity_id,
            reserved_names=reserved_names,
            mention_name=mention_name,
            as_of_chapter=as_of_chapter,
        )
        if conflict:
            return self._conflict_decision(mention_name, conflict)
        for name in aliases:
            reserved_names[name] = mention_name
        return EntityAdmissionDecision(
            mention_name=mention_name,
            action="register_alias",
            entity_id=entity.entity_id,
            canonical_name=entity.name,
            aliases=aliases,
            role_hint=str(raw_decision.get("role_hint") or ""),
            reason=str(raw_decision.get("reason") or ""),
        )

    @staticmethod
    def _conflict_decision(
        mention_name: str,
        reason: str,
    ) -> EntityAdmissionDecision:
        return EntityAdmissionDecision(
            mention_name=mention_name,
            action="plan_conflict",
            reason=reason,
        )

    def _name_conflict(
        self,
        *,
        project_id: str,
        names: list[str],
        target_entity_id: str,
        reserved_names: dict[str, str],
        mention_name: str,
        as_of_chapter: int,
    ) -> str:
        owners = self._name_owners(
            project_id,
            names,
            as_of_chapter=as_of_chapter,
        )
        for name in names:
            reserved_by = reserved_names.get(name)
            if reserved_by and reserved_by != mention_name:
                return f'name "{name}" is already reserved by {reserved_by}'
            owner_ids = owners.get(name, set())
            if target_entity_id:
                if owner_ids and owner_ids != {target_entity_id}:
                    return f'name "{name}" belongs to another entity'
            elif owner_ids:
                return f'name "{name}" already exists'
        return ""

    def _revalidate_plan(
        self,
        *,
        project_id: str,
        plan: EntityAdmissionPlan,
    ) -> None:
        as_of_chapter = max(int(plan.chapter_number) - 1, 0)
        reserved_names: dict[str, str] = {}
        for decision in plan.decisions:
            if decision.action in {"background_generic", "plan_conflict"}:
                continue
            target_entity_id = (
                decision.entity_id if decision.action == "register_alias" else ""
            )
            names = (
                list(decision.aliases)
                if decision.action == "register_alias"
                else _dedupe([decision.canonical_name, *decision.aliases])
            )
            existing = self.session.get(Entity, decision.entity_id)
            if decision.action == "register_character" and existing is not None:
                if (
                    existing.project_id == project_id
                    and existing.kind == "character"
                    and existing.name == decision.canonical_name
                ):
                    target_entity_id = existing.id
                else:
                    raise ValueError(
                        f"Entity admission id conflict: {decision.mention_name}"
                    )
            conflict = self._name_conflict(
                project_id=project_id,
                names=names,
                target_entity_id=target_entity_id,
                reserved_names=reserved_names,
                mention_name=decision.mention_name,
                as_of_chapter=as_of_chapter,
            )
            if conflict:
                raise ValueError(
                    f"Entity admission uniqueness conflict for {decision.mention_name}: {conflict}"
                )
            for name in names:
                reserved_names[name] = decision.mention_name
            if decision.action == "register_alias":
                self._require_character_target(
                    project_id=project_id,
                    entity_id=decision.entity_id,
                    as_of_chapter=as_of_chapter,
                )

    def _classify(
        self,
        *,
        project_id: str,
        chapter_number: int,
        names: list[str],
        writer_output: WriterOutput,
    ) -> list[dict[str, Any]]:
        prose_evidence = "\n".join(
            (
                str(writer_output.body or ""),
                str(writer_output.end_of_chapter_summary or ""),
            )
        )
        deterministic: list[dict[str, Any]] = []
        for name in names:
            reason = ""
            if looks_like_generic_character_reference(name) or looks_like_non_character_reference(name):
                reason = "deterministic reference classifier"
            elif not _has_prose_evidence(prose_evidence, name):
                reason = "named mention has no exact prose evidence"
            if reason:
                deterministic.append(
                    {
                        "decision": "background_generic",
                        "name": name,
                        "reason": reason,
                    }
                )
        deterministic_names = {str(item["name"]) for item in deterministic}
        unresolved_names = [name for name in names if name not in deterministic_names]
        if not unresolved_names:
            return deterministic
        if self.classifier is None:
            return [
                *deterministic,
                *[
                    {
                        "decision": "plan_conflict",
                        "name": name,
                        "reason": "entity registrar classifier unavailable",
                    }
                    for name in unresolved_names
                ],
            ]
        existing_entities = self.book_state.active_entities(
            project_id,
            as_of_chapter=max(int(chapter_number) - 1, 0),
            kinds={"character"},
        )
        try:
            raw = self.classifier.classify(
                project_id=project_id,
                chapter_number=chapter_number,
                names=unresolved_names,
                writer_output=writer_output,
                existing_entities=existing_entities,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Entity registrar classifier failed; routing names to plan_conflict.",
                exc_info=True,
            )
            return [
                *deterministic,
                *[
                    {
                        "decision": "plan_conflict",
                        "name": name,
                        "reason": f"classifier_error:{type(exc).__name__}",
                    }
                    for name in unresolved_names
                ],
            ]
        expected = set(unresolved_names)
        decision_by_name: dict[str, dict[str, Any]] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(
                item.get("name")
                or item.get("entity_name")
                or item.get("unknown_name")
                or ""
            ).strip()
            if name in expected and name not in decision_by_name:
                decision_by_name[name] = item
        classified = [
            decision_by_name.get(name)
            or {
                "decision": "plan_conflict",
                "name": name,
                "reason": "classifier omitted this name",
            }
            for name in unresolved_names
        ]
        return [*deterministic, *classified]

    def _unknown_named_references(
        self,
        *,
        project_id: str,
        writer_output: WriterOutput,
    ) -> list[str]:
        mentioned = _structured_character_reference_names(writer_output)
        if not mentioned:
            return []
        known = self.book_state.entities_by_names(
            project_id,
            mentioned,
            as_of_chapter=max(int(writer_output.chapter_number) - 1, 0),
        )
        return [name for name in mentioned if name not in known]

    def _name_owners(
        self,
        project_id: str,
        names: list[str],
        *,
        as_of_chapter: int,
    ) -> dict[str, set[str]]:
        normalized = _dedupe(names)
        owners = {name: set() for name in normalized}
        if not normalized:
            return owners
        for name, entity in self.book_state.entities_by_names(
            project_id,
            normalized,
            as_of_chapter=as_of_chapter,
        ).items():
            owners[name].add(entity.entity_id)
        return owners

    def _resolve_character_target(
        self,
        *,
        project_id: str,
        entity_id: str,
        canonical_name: str,
        as_of_chapter: int,
    ) -> tuple[EntitySnapshot | None, str]:
        characters = self.book_state.active_entities(
            project_id,
            as_of_chapter=as_of_chapter,
            kinds={"character"},
        )
        if entity_id:
            entity = next(
                (item for item in characters if item.entity_id == entity_id),
                None,
            )
            if entity is None:
                return None, "alias target not found"
            return entity, ""
        if not canonical_name:
            return None, "alias target is missing entity_id and canonical_name"
        unique = {
            entity.entity_id: entity
            for entity in characters
            if canonical_name == entity.name or canonical_name in entity.aliases
        }
        if len(unique) != 1:
            reason = "alias target not found" if not unique else "alias target is ambiguous"
            return None, reason
        return next(iter(unique.values())), ""

    def _require_character_target(
        self,
        *,
        project_id: str,
        entity_id: str,
        as_of_chapter: int,
    ) -> EntitySnapshot:
        entity = next(
            (
                item
                for item in self.book_state.active_entities(
                    project_id,
                    as_of_chapter=as_of_chapter,
                    kinds={"character"},
                )
                if item.entity_id == entity_id
            ),
            None,
        )
        if entity is None:
            raise ValueError(f"Entity admission alias target missing: {entity_id}")
        return entity

    @staticmethod
    def _drop_background_generic_references(
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
        state_changes = [
            change
            for change in writer_output.state_changes
            if str(getattr(change, "entity_name", "") or "").strip() not in names
        ]
        events = []
        for event in writer_output.new_events:
            involved_entity_names: list[str] = []
            roles: list[str] = []
            for index, raw_name in enumerate(event.involved_entity_names):
                name = str(raw_name or "").strip()
                if name in names:
                    continue
                involved_entity_names.append(raw_name)
                if index < len(event.roles):
                    roles.append(event.roles[index])
            events.append(
                event.model_copy(
                    update={
                        "involved_entity_names": involved_entity_names,
                        "roles": roles,
                    }
                )
            )
        return writer_output.model_copy(
            update={
                "entity_mentions": mentions,
                "state_changes": state_changes,
                "new_events": events,
            }
        )

    @staticmethod
    def _attach_admission_plan(
        writer_output: WriterOutput,
        plan: EntityAdmissionPlan,
    ) -> WriterOutput:
        generation_meta = dict(writer_output.generation_meta or {})
        generation_meta["entity_admission_plan"] = plan.model_dump(mode="json")
        return writer_output.model_copy(update={"generation_meta": generation_meta})


def writer_output_admission_fingerprint(writer_output: WriterOutput) -> str:
    payload = writer_output.model_dump(
        mode="json",
        exclude={
            "generation_meta": True,
            "draft_blob_path": True,
            "scene_outputs": {"__all__": {"text_blob_path"}},
        },
    )
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _admission_plan_from_output(writer_output: WriterOutput) -> EntityAdmissionPlan:
    generation_meta = (
        writer_output.generation_meta
        if isinstance(writer_output.generation_meta, dict)
        else {}
    )
    payload = generation_meta.get("entity_admission_plan")
    if not isinstance(payload, dict):
        raise ValueError("Entity admission plan is missing")
    try:
        return EntityAdmissionPlan.model_validate(payload)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Entity admission plan is invalid") from exc


def _importance(raw: object) -> int:
    try:
        value = int(raw or 5)
    except (TypeError, ValueError):
        value = 5
    return max(1, min(10, value))


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = str(item or "").strip()
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _structured_character_reference_names(writer_output: WriterOutput) -> list[str]:
    character_candidates: list[str] = []
    non_character_names: set[str] = set()

    for mention in writer_output.entity_mentions:
        if not bool(getattr(mention, "is_named", False)):
            continue
        if not bool(getattr(mention, "is_on_stage", True)):
            continue
        name = str(getattr(mention, "entity_name", "") or "").strip()
        kind = str(getattr(mention, "entity_kind", "") or "").strip().lower()
        if not name:
            continue
        if kind in _CHARACTER_KINDS:
            character_candidates.append(name)
        else:
            non_character_names.add(name)

    for change in writer_output.state_changes:
        name = str(getattr(change, "entity_name", "") or "").strip()
        kind = str(getattr(change, "entity_kind", "") or "").strip().lower()
        if not name:
            continue
        if kind in _CHARACTER_KINDS:
            character_candidates.append(name)
        else:
            non_character_names.add(name)

    for event in writer_output.new_events:
        for raw_name in event.involved_entity_names:
            name = str(raw_name or "").strip()
            if name and name not in non_character_names:
                character_candidates.append(name)

    return _dedupe(
        [
            name
            for name in character_candidates
            if name not in non_character_names
        ]
    )


def _mention_quotes(
    text: str,
    name: str,
    *,
    radius: int = 120,
    limit: int = 3,
) -> list[str]:
    quotes: list[str] = []
    for evidence_name in _prose_evidence_names(name):
        start_at = 0
        while len(quotes) < limit:
            index = text.find(evidence_name, start_at)
            if index < 0:
                break
            quote_start = max(0, index - radius)
            quote_end = min(len(text), index + len(evidence_name) + radius)
            quote = text[quote_start:quote_end]
            if quote not in quotes:
                quotes.append(quote)
            start_at = index + len(evidence_name)
        if len(quotes) >= limit:
            break
    return quotes


def _has_prose_evidence(text: str, name: str) -> bool:
    return any(evidence_name in text for evidence_name in _prose_evidence_names(name))


def _prose_evidence_names(name: str) -> list[str]:
    base_name = re.sub(r"\s*[（(][^（）()]{1,40}[）)]\s*$", "", name).strip()
    return _dedupe(
        [
            name,
            base_name if len(base_name) >= 2 else "",
        ]
    )


__all__ = [
    "EntityAdmissionResult",
    "EntityRegistrar",
    "LLMEntityAdmissionClassifier",
    "writer_output_admission_fingerprint",
]
