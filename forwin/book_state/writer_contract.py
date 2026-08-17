from __future__ import annotations

from hashlib import sha1
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from forwin.checker.reference_classifier import (
    looks_like_generic_character_reference,
    looks_like_non_character_reference,
)
from forwin.naming.types import EntityAdmissionPlan
from forwin.protocol.book_state import (
    FactPatch,
    GraphDelta,
    GraphDeltaType,
    NarrativePatch,
    NodePatch,
    WorldNode,
)
from forwin.protocol.writer import WriterOutput

from .projection import BookStateProjection
from .schema import WORLD_NODE_FIELDS

_KIND_ALIASES = {
    "person": "character",
    "human": "character",
    "organization": "faction",
    "organisation": "faction",
    "group": "faction",
    "object": "item",
    "artifact": "item",
    "resource": "item",
    "law": "rule",
    "protocol": "rule",
}
_FIELD_ALIASES = {
    "character": {
        "location": "state.location_id",
        "location_id": "state.location_id",
        "role": "profile.narrative_role",
        "role_state": "profile.narrative_role",
        "knowledge": "state.state_summary",
        "knowledge_state": "state.state_summary",
        "possession": "state.resource_summary",
        "possession_state": "state.resource_summary",
        "life_state": "state.status",
        "custody_state": "state.status",
        "injury_state": "state.health",
        "participation_state": "state.status",
    },
    "faction": {
        "location": "state.headquarters_location_id",
        "location_id": "state.headquarters_location_id",
        "goal": "state.current_goal",
    },
    "item": {
        "location": "state.location_id",
        "owner": "state.owner_id",
        "holder": "state.holder_id",
        "custody_state": "state.state_summary",
    },
    "rule": {
        "definition": "profile.public_version",
        "rule": "profile.public_version",
        "rule_text": "profile.public_version",
        "text": "profile.public_version",
        "content": "profile.public_version",
        "wording": "profile.public_version",
        "formulation": "profile.public_version",
        "public_version": "profile.public_version",
        "trigger": "profile.trigger_condition",
        "effect": "profile.effect_description",
    },
}
_WRITER_LOCATION_METADATA_PATH = "metadata.writer_location"
_MIN_EMBEDDED_LOCATION_LABEL_LENGTH = 3


class _MapLocationResolver:
    def __init__(self, nodes: Any) -> None:
        self.node_ids: set[str] = set()
        node_ids_by_label: dict[str, set[str]] = {}
        for node in nodes:
            node_id = str(node.id or "").strip()
            if not node_id:
                continue
            self.node_ids.add(node_id)
            for value in (node.name, *node.aliases):
                label = _normalize_location_text(value)
                if label:
                    node_ids_by_label.setdefault(label, set()).add(node_id)
        self.unique_node_id_by_label = {
            label: next(iter(node_ids))
            for label, node_ids in node_ids_by_label.items()
            if len(node_ids) == 1
        }

    def resolve(self, value: Any) -> str | None:
        raw_value = str(value or "").strip()
        if raw_value in self.node_ids:
            return raw_value
        text = _normalize_location_text(raw_value)
        if not text:
            return None
        exact = self.unique_node_id_by_label.get(text)
        if exact is not None:
            return exact
        candidates = [
            (len(label), node_id)
            for label, node_id in self.unique_node_id_by_label.items()
            if len(label) >= _MIN_EMBEDDED_LOCATION_LABEL_LENGTH
            and label in text
        ]
        if not candidates:
            return None
        longest = max(length for length, _node_id in candidates)
        node_ids = {
            node_id
            for length, node_id in candidates
            if length == longest
        }
        if len(node_ids) != 1:
            return None
        return next(iter(node_ids))


class WriterContractIssue(BaseModel):
    code: str
    message: str
    evidence_refs: list[str] = Field(default_factory=list)


class WriterContractDeltaResult(BaseModel):
    graph_deltas: list[GraphDelta] = Field(default_factory=list)
    issues: list[WriterContractIssue] = Field(default_factory=list)


class WriterContractDeltaBuilder:
    """Translate accepted WriterOutput structure into authoritative GraphDelta."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def build(
        self,
        *,
        project_id: str,
        chapter_number: int,
        writer_output: WriterOutput,
        review_verdict_id: str,
    ) -> WriterContractDeltaResult:
        runtime = BookStateProjection(self.session).load_runtime_as_of(
            project_id,
            as_of_chapter=max(int(chapter_number) - 1, 0),
        )
        location_resolver = _MapLocationResolver(runtime.map.nodes_by_id.values())
        node_by_name = _world_node_name_index(runtime.world.nodes_by_id.values())
        node_kind_by_name = {
            name: str(node.node_type)
            for name, node in node_by_name.items()
        }
        node_patches: list[NodePatch] = []
        fact_patches: list[FactPatch] = []
        narrative_patches: list[NarrativePatch] = []
        issues: list[WriterContractIssue] = []
        created_node_ids: set[str] = set()
        lore_evidence_refs: list[str] = []

        admission_plan = _admission_plan(writer_output)
        if admission_plan is not None:
            self._apply_admission_plan(
                project_id=project_id,
                chapter_number=chapter_number,
                plan=admission_plan,
                node_by_name=node_by_name,
                node_kind_by_name=node_kind_by_name,
                node_patches=node_patches,
                created_node_ids=created_node_ids,
                issues=issues,
            )

        mention_kind_by_name: dict[str, str] = {}
        for mention in writer_output.entity_mentions:
            if not bool(mention.is_named) or not bool(mention.is_on_stage):
                continue
            name = str(mention.entity_name or "").strip()
            kind = _normalize_kind(mention.entity_kind)
            if not name:
                continue
            mention_kind_by_name[name] = kind
            if name in node_by_name:
                continue
            if kind == "character":
                if admission_plan is None or not _plan_admits_name(admission_plan, name):
                    issues.append(_unresolved_character_issue(name, chapter_number))
                continue
            node = self._create_named_node(
                project_id=project_id,
                chapter_number=chapter_number,
                name=name,
                kind=kind,
                reason="writer entity mention",
                node_patches=node_patches,
                created_node_ids=created_node_ids,
            )
            node_by_name[name] = node
            node_kind_by_name[name] = kind

        for change in writer_output.state_changes:
            name = str(change.entity_name or "").strip()
            kind = _normalize_kind(change.entity_kind)
            node = node_by_name.get(name)
            if node is None and kind != "character" and name:
                node = self._create_named_node(
                    project_id=project_id,
                    chapter_number=chapter_number,
                    name=name,
                    kind=kind,
                    reason="writer state change target",
                    node_patches=node_patches,
                    created_node_ids=created_node_ids,
                )
                node_by_name[name] = node
                node_kind_by_name[name] = kind
            if node is None:
                issues.append(_unresolved_character_issue(name, chapter_number))
                continue
            resolved_kind = str(node.node_type or kind)
            field_path = _book_state_field_path(
                resolved_kind,
                str(change.field or ""),
            )
            reported_old = (
                str(change.old_value)
                if str(change.old_value or "").strip()
                else None
            )
            reported_new = str(change.new_value or "")
            if _is_location_id_field(field_path):
                node_patches.append(
                    NodePatch(
                        node_id=node.id,
                        node_type=resolved_kind,
                        op="set",
                        field_path=_WRITER_LOCATION_METADATA_PATH,
                        old_value=_current_node_value(
                            runtime,
                            node.id,
                            _WRITER_LOCATION_METADATA_PATH,
                        ),
                        new_value={
                            "reported_old": reported_old or "",
                            "reported_new": reported_new,
                        },
                        reason=str(change.reason or "writer location description"),
                    )
                )
                resolved_new = location_resolver.resolve(reported_new)
                if reported_new.strip() and resolved_new is None:
                    continue
                new_value = resolved_new or ""
            else:
                new_value = reported_new
            node_patches.append(
                NodePatch(
                    node_id=node.id,
                    node_type=resolved_kind,
                    op="set",
                    field_path=field_path,
                    old_value=_current_node_value(
                        runtime,
                        node.id,
                        field_path,
                    ),
                    new_value=new_value,
                    reason=str(change.reason or "writer state change"),
                )
            )

        patched_fields = {
            (patch.node_id, str(patch.field_path or ""))
            for patch in node_patches
            if str(patch.op or "") == "set"
        }
        for lore in writer_output.lore_candidates:
            name = str(lore.subject_name or "").strip()
            description = str(lore.description or "").strip()
            if not name or not description:
                continue
            node = node_by_name.get(name)
            reported_kind = _normalize_kind(lore.subject_type)
            resolved_kind = str(node.node_type) if node is not None else reported_kind
            if resolved_kind != "rule":
                continue
            if node is None:
                node = self._create_named_node(
                    project_id=project_id,
                    chapter_number=chapter_number,
                    name=name,
                    kind="rule",
                    reason="writer rule lore candidate",
                    node_patches=node_patches,
                    created_node_ids=created_node_ids,
                )
                node_by_name[name] = node
                node_kind_by_name[name] = "rule"
            field_path = "profile.public_version"
            patch_key = (node.id, field_path)
            if patch_key in patched_fields:
                continue
            current_value = _current_node_value(runtime, node.id, field_path)
            if current_value == description:
                continue
            node_patches.append(
                NodePatch(
                    node_id=node.id,
                    node_type="rule",
                    op="set",
                    field_path=field_path,
                    old_value=current_value,
                    new_value=description,
                    reason="writer rule lore candidate",
                )
            )
            patched_fields.add(patch_key)
            lore_evidence_refs.extend(
                str(ref or "").strip()
                for ref in lore.evidence_refs
                if str(ref or "").strip()
            )

        for index, event in enumerate(writer_output.new_events):
            participant_ids: list[str] = []
            unresolved_names: list[str] = []
            for name in event.involved_entity_names:
                normalized_name = str(name or "").strip()
                if not normalized_name:
                    continue
                node = node_by_name.get(normalized_name)
                if node is None:
                    kind = mention_kind_by_name.get(normalized_name, "")
                    if kind and kind != "character":
                        node = self._create_named_node(
                            project_id=project_id,
                            chapter_number=chapter_number,
                            name=normalized_name,
                            kind=kind,
                            reason="writer event participant",
                            node_patches=node_patches,
                            created_node_ids=created_node_ids,
                        )
                        node_by_name[normalized_name] = node
                        node_kind_by_name[normalized_name] = kind
                if node is None and (
                    looks_like_generic_character_reference(normalized_name)
                    or looks_like_non_character_reference(normalized_name)
                ):
                    continue
                if node is None:
                    unresolved_names.append(normalized_name)
                else:
                    participant_ids.append(node.id)
            if unresolved_names:
                issues.append(
                    WriterContractIssue(
                        code="unresolved_event_entity",
                        message=(
                            f"Event references unresolved entities: {', '.join(unresolved_names)}"
                        ),
                        evidence_refs=[f"chapter:{chapter_number}", f"event:{index}"],
                    )
                )
                continue
            event_id = _stable_id(
                "event",
                project_id,
                chapter_number,
                index,
                event.summary,
            )
            fact_id = _stable_id(
                "fact_event",
                project_id,
                chapter_number,
                index,
                event.summary,
            )
            event_summary = str(event.summary or "").strip()
            node_patches.append(
                NodePatch(
                    node_id=event_id,
                    node_type="event",
                    op="create",
                    new_value={
                        "project_id": project_id,
                        "name": event_summary[:80],
                        "summary": event_summary,
                        "description": event_summary,
                        "created_at_chapter": chapter_number,
                        "profile": {
                            "event_type": str(event.significance),
                            "planned_or_actual": "actual",
                            "expected_chapter": chapter_number,
                            "involved_node_refs": [
                                f"node:{node_id}" for node_id in participant_ids
                            ],
                            "public_summary": event_summary,
                        },
                        "state": {
                            "status": "occurred",
                            "actual_chapter": chapter_number,
                            "participant_ids": participant_ids,
                            "result_refs": [f"fact:{fact_id}"],
                            "state_summary": event_summary,
                        },
                        "metadata": {"source": "writer_output.new_events"},
                    },
                    reason=event_summary,
                )
            )
            fact_patches.append(
                FactPatch(
                    fact_id=fact_id,
                    op="create",
                    proposition=event_summary,
                    truth_value="true",
                    related_refs=[event_id, *participant_ids],
                    new_value={
                        "project_id": project_id,
                        "proposition": event_summary,
                        "fact_type": "event",
                        "truth_value": "true",
                        "confidence": 1.0,
                        "related_node_refs": [event_id, *participant_ids],
                        "source_refs": [f"chapter:{chapter_number}"],
                        "created_at_chapter": chapter_number,
                        "state": {
                            "status": "active",
                            "last_updated_chapter": chapter_number,
                            "canonicality_level": "canon",
                            "state_summary": event_summary,
                        },
                    },
                    reason=event_summary,
                )
            )

        narrative_by_title = {
            str(node.title or "").strip(): node
            for node in runtime.narrative.nodes_by_id.values()
            if str(node.node_type) == "plot_thread" and str(node.title or "").strip()
        }
        for index, beat in enumerate(writer_output.thread_beats):
            thread_name = str(beat.thread_name or "").strip()
            thread = narrative_by_title.get(thread_name)
            thread_id = (
                thread.id
                if thread is not None
                else _stable_id("plot_thread", project_id, thread_name)
            )
            if thread is None:
                narrative_patches.append(
                    NarrativePatch(
                        target_ref=f"plot_thread:{thread_id}",
                        op="create",
                        new_value={
                            "node_type": "plot_thread",
                            "title": thread_name,
                            "status": "active",
                            "payload": {
                                "description": str(beat.description or ""),
                                "priority": 5,
                                "beats": [],
                            },
                            "metadata": {
                                "created_at_chapter": chapter_number,
                                "source": "writer_output.thread_beats",
                            },
                        },
                        reason="writer thread beat created thread",
                    )
                )
            narrative_patches.append(
                NarrativePatch(
                    target_ref=f"plot_thread:{thread_id}",
                    op="append",
                    field_path="payload.beats",
                    new_value={
                        "chapter_number": chapter_number,
                        "beat_type": str(beat.beat_type),
                        "description": str(beat.description or ""),
                        "sequence": index,
                    },
                    reason=str(beat.description or "writer thread beat"),
                    evidence_refs=[f"chapter:{chapter_number}"],
                )
            )
            if str(beat.beat_type) == "resolution":
                narrative_patches.append(
                    NarrativePatch(
                        target_ref=f"plot_thread:{thread_id}",
                        op="set",
                        field_path="status",
                        new_value="resolved",
                        reason="writer resolved plot thread",
                    )
                )

        if issues:
            return WriterContractDeltaResult(issues=_dedupe_issues(issues))
        if not (node_patches or fact_patches or narrative_patches or writer_output.time_advance):
            return WriterContractDeltaResult()
        story_time = (
            str(writer_output.time_advance.new_time_label or "").strip()
            if writer_output.time_advance is not None
            else ""
        )
        summary = (
            str(writer_output.end_of_chapter_summary or "").strip()
            or str(writer_output.title or "").strip()
        )
        delta = GraphDelta(
            id=_stable_id(
                "writer_contract",
                project_id,
                chapter_number,
                summary,
            ),
            project_id=project_id,
            chapter_number=chapter_number,
            story_time=story_time,
            delta_type=GraphDeltaType.WORLD_STATE,
            operation="apply_writer_contract",
            target_type="chapter",
            target_id=f"chapter:{chapter_number}",
            source_type="writer_output",
            source_id=f"writer_output:{chapter_number}",
            world_line_id="main",
            summary=summary,
            node_patches=node_patches,
            fact_patches=fact_patches,
            narrative_patches=narrative_patches,
            evidence_refs=_unique_strings(
                [f"chapter:{chapter_number}", *lore_evidence_refs]
            ),
            review_verdict_id=review_verdict_id,
            metadata={
                "extraction_path": "writer_contract",
                "state_change_count": len(writer_output.state_changes),
                "rule_lore_count": len(
                    [
                        lore
                        for lore in writer_output.lore_candidates
                        if _normalize_kind(lore.subject_type) == "rule"
                        and str(lore.description or "").strip()
                    ]
                ),
                "event_count": len(writer_output.new_events),
                "thread_beat_count": len(writer_output.thread_beats),
                "time_advanced": writer_output.time_advance is not None,
            },
        )
        return WriterContractDeltaResult(graph_deltas=[delta])

    def _apply_admission_plan(
        self,
        *,
        project_id: str,
        chapter_number: int,
        plan: EntityAdmissionPlan,
        node_by_name: dict[str, WorldNode],
        node_kind_by_name: dict[str, str],
        node_patches: list[NodePatch],
        created_node_ids: set[str],
        issues: list[WriterContractIssue],
    ) -> None:
        for decision in plan.decisions:
            if decision.action == "plan_conflict":
                issues.append(
                    WriterContractIssue(
                        code="entity_admission_plan_conflict",
                        message=f"Entity admission rejected {decision.mention_name}: {decision.reason}",
                        evidence_refs=[f"entity_admission:{decision.mention_name}"],
                    )
                )
                continue
            if decision.action == "background_generic":
                continue
            if decision.action == "register_character":
                node = node_by_name.get(decision.canonical_name)
                if node is None:
                    node = WorldNode(
                        id=decision.entity_id,
                        project_id=project_id,
                        node_type="character",
                        name=decision.canonical_name,
                        aliases=list(decision.aliases),
                        summary=decision.role_hint,
                        description=decision.role_hint,
                        importance=decision.importance,
                        created_at_chapter=chapter_number,
                        profile={"narrative_role": decision.role_hint},
                        state={"status": "active", "last_seen_chapter": chapter_number},
                        metadata={
                            "source": "entity_admission_plan",
                            "mention_name": decision.mention_name,
                        },
                    )
                    node_patches.append(
                        NodePatch(
                            node_id=node.id,
                            node_type="character",
                            op="create",
                            new_value=node.model_dump(
                                mode="json",
                                exclude={"id", "node_type"},
                            ),
                            reason="Canon entity admission",
                        )
                    )
                    created_node_ids.add(node.id)
                for name in _decision_names(decision):
                    node_by_name[name] = node
                    node_kind_by_name[name] = "character"
                continue
            node = (
                node_by_name.get(decision.canonical_name)
                or node_by_name.get(decision.mention_name)
            )
            if node is None or str(node.node_type) != "character":
                issues.append(
                    WriterContractIssue(
                        code="entity_admission_alias_target_missing",
                        message=f"BookState alias target missing: {decision.canonical_name}",
                        evidence_refs=[f"entity_admission:{decision.mention_name}"],
                    )
                )
                continue
            for alias in decision.aliases:
                if alias not in node.aliases:
                    node_patches.append(
                        NodePatch(
                            node_id=node.id,
                            node_type="character",
                            op="append",
                            field_path="aliases",
                            new_value=alias,
                            reason="Canon entity alias admission",
                        )
                    )
                node_by_name[alias] = node
                node_kind_by_name[alias] = "character"
            node_by_name[decision.mention_name] = node
            node_kind_by_name[decision.mention_name] = "character"

    @staticmethod
    def _create_named_node(
        *,
        project_id: str,
        chapter_number: int,
        name: str,
        kind: str,
        reason: str,
        node_patches: list[NodePatch],
        created_node_ids: set[str],
    ) -> WorldNode:
        node_id = _stable_id(kind, project_id, name)
        node = WorldNode(
            id=node_id,
            project_id=project_id,
            node_type=kind,
            name=name,
            aliases=[],
            summary=f"第{chapter_number}章出现的{kind}：{name}",
            description=f"由结构化 WriterOutput 在第{chapter_number}章登记。",
            importance=4,
            created_at_chapter=chapter_number,
            profile={},
            state={"status": "active"},
            metadata={"source": "writer_contract", "reason": reason},
        )
        if node_id not in created_node_ids:
            node_patches.append(
                NodePatch(
                    node_id=node_id,
                    node_type=kind,
                    op="create",
                    new_value=node.model_dump(
                        mode="json",
                        exclude={"id", "node_type"},
                    ),
                    reason=reason,
                )
            )
            created_node_ids.add(node_id)
        return node


def _admission_plan(writer_output: WriterOutput) -> EntityAdmissionPlan | None:
    generation_meta = (
        writer_output.generation_meta
        if isinstance(writer_output.generation_meta, dict)
        else {}
    )
    payload = generation_meta.get("entity_admission_plan")
    if not isinstance(payload, dict):
        return None
    return EntityAdmissionPlan.model_validate(payload)


def _plan_admits_name(plan: EntityAdmissionPlan, name: str) -> bool:
    return any(
        name in _decision_names(decision)
        for decision in plan.decisions
        if decision.action in {"register_character", "register_alias"}
    )


def _decision_names(decision: Any) -> set[str]:
    return {
        str(value or "").strip()
        for value in (
            decision.mention_name,
            decision.canonical_name,
            *decision.aliases,
        )
        if str(value or "").strip()
    }


def _world_node_name_index(nodes: Any) -> dict[str, WorldNode]:
    index: dict[str, WorldNode] = {}
    for node in nodes:
        for name in (node.id, node.name, *node.aliases):
            normalized = str(name or "").strip()
            if normalized:
                index.setdefault(normalized, node)
    return index


def _normalize_kind(raw: str) -> str:
    kind = str(raw or "").strip().lower()
    return _KIND_ALIASES.get(kind, kind or "character")


def _book_state_field_path(kind: str, field: str) -> str:
    normalized_field = str(field or "").strip()
    mapped = _FIELD_ALIASES.get(kind, {}).get(normalized_field)
    if mapped:
        return mapped
    fields = WORLD_NODE_FIELDS.get(kind, {})
    if normalized_field in fields.get("state", set()):
        return f"state.{normalized_field}"
    if normalized_field in fields.get("profile", set()):
        return f"profile.{normalized_field}"
    return f"metadata.writer_state.{normalized_field or 'unspecified'}"


def _is_location_id_field(field_path: str) -> bool:
    return field_path.startswith("state.") and field_path.rsplit(".", 1)[-1].endswith(
        "location_id"
    )


def _current_node_value(runtime: Any, node_id: str, field_path: str) -> Any:
    node = runtime.world.nodes_by_id.get(node_id)
    if node is None:
        return None
    if field_path.startswith("state."):
        payload: Any = runtime.world.get_state(node_id)
        parts = field_path.removeprefix("state.").split(".")
    else:
        payload = node.model_dump(mode="json")
        parts = field_path.split(".")
    for part in parts:
        if not isinstance(payload, dict):
            return None
        payload = payload.get(part)
    return payload


def _normalize_location_text(value: Any) -> str:
    return "".join(str(value or "").split()).casefold()


def _stable_id(prefix: str, *parts: Any) -> str:
    digest = sha1(":".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _unresolved_character_issue(
    name: str,
    chapter_number: int,
) -> WriterContractIssue:
    return WriterContractIssue(
        code="unresolved_character_reference",
        message=f"Character is not present in BookState or EntityAdmissionPlan: {name}",
        evidence_refs=[f"chapter:{chapter_number}", f"entity:{name}"],
    )


def _dedupe_issues(issues: list[WriterContractIssue]) -> list[WriterContractIssue]:
    result: list[WriterContractIssue] = []
    seen: set[tuple[str, str]] = set()
    for issue in issues:
        key = (issue.code, issue.message)
        if key not in seen:
            result.append(issue)
            seen.add(key)
    return result


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


__all__ = [
    "WriterContractDeltaBuilder",
    "WriterContractDeltaResult",
    "WriterContractIssue",
]
