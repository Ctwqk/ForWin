"""Compile a locked Genesis world seed into chapter-0 BookState."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.audit.events import DecisionEventInfo, DecisionEventType
from forwin.book_state.book_frame_repository import BookLexiconRepository, StoryCalendarRepository
from forwin.book_state.knowledge import KnowledgeEvent, knowledge_event_to_patch
from forwin.book_state.lexicon import BookLexicon, lexicon_from_payload
from forwin.book_state.narrative import promise_node_payload
from forwin.book_state.repository import BookStateRepository, _loads
from forwin.book_state.reviewer import BookStateReviewGate
from forwin.book_state.rule_predicates import parse_checkable
from forwin.book_state.story_clock import (
    StoryCalendar,
    calendar_from_payload,
    duration_to_minutes,
    format_story_time,
)
from forwin.canon.admission import (
    GENESIS_SEED_SOURCE_TYPE,
    CanonAdmissionService,
    CanonStaleVersion,
    CanonWriteFailure,
)
from forwin.genesis.helpers import _pack_stage_payload, _world_seed_validation_issues
from forwin.genesis.world_seed import normalize_world_seed, story_engine_actor_refs
from forwin.map.models import MapNodeRow
from forwin.models.genesis import BookGenesisRevision
from forwin.models.project import Project
from forwin.protocol.book_state import (
    WORLD_EDGE_TYPES_BY_FAMILY,
    ApprovedGraphDeltaSet,
    CognitionPatch,
    EdgePatch,
    FactPatch,
    GraphDelta,
    GraphDeltaType,
    NarrativePatch,
    NodePatch,
    ReaderPromise,
)
from forwin.state.updater import StateUpdater

from .commands import HandoffStageError


SEED_DELTA_SECTIONS = ("world", "cognition", "narrative")
OBSERVER_TYPES = {"character": "character", "faction": "faction", "organization": "group", "group": "group"}
_PATCH_REASON = "Genesis world seed"


class WorldSeedCommitError(HandoffStageError):
    """The world seed cannot become chapter-0 BookState; ``issues`` read ``"<code> <path>: <detail>"``."""

    def __init__(self, issues: Iterable[str]) -> None:
        self.issues = tuple(issues)
        super().__init__("world_seed commit failed: " + "; ".join(self.issues))


@dataclass(frozen=True)
class WorldSeedCommitSummary:
    skipped: bool
    graph_delta_ids: tuple[str, ...]
    actor_count: int
    agenda_count: int
    intent_count: int
    clock_count: int
    fact_count: int
    knowledge_count: int
    rule_count: int
    promise_count: int


def seed_ref_id(project_id: str, kind: str, ref: str) -> str:
    """Stable BookState id of a seed item; hyphenated so it never reads as a story id literal."""
    digest = sha256(f"{project_id}\0{kind}\0{ref}".encode()).hexdigest()[:20]
    return f"{kind}-{digest}"


def world_seed_delta_ids(project_id: str) -> tuple[str, ...]:
    return tuple(f"genesis-seed:{project_id}:{section}" for section in SEED_DELTA_SECTIONS)


def map_node_ids_by_genesis_ref(session: Session, project_id: str) -> dict[str, str]:
    """Genesis map_atlas node id -> database map node id, from ``metadata.source_node_id``."""
    node_ids: dict[str, str] = {}
    rows = session.execute(
        select(MapNodeRow.id, MapNodeRow.metadata_json)
        .where(MapNodeRow.project_id == project_id)
        .order_by(MapNodeRow.id.asc())
    )
    for node_id, metadata_json in rows:
        metadata = _loads(metadata_json, {})
        source_node_id = str(metadata.get("source_node_id") or "").strip() if isinstance(metadata, dict) else ""
        if source_node_id:
            node_ids.setdefault(source_node_id, node_id)
    return node_ids


def build_world_seed_deltas(
    *,
    project_id: str,
    revision_id: str,
    seed: Mapping,
    story_engine: Mapping,
    map_node_ids: Mapping[str, str],
    calendar: StoryCalendar,
    lexicon: BookLexicon,
) -> list[GraphDelta]:
    """The world, cognition and narrative deltas of a validated seed, all at the opening minute.

    Raises ``WorldSeedCommitError`` listing every reference that cannot be resolved.
    """
    builder = _SeedDeltaBuilder(
        project_id=project_id,
        seed=normalize_world_seed(seed, story_engine=story_engine, map_atlas={}),
        story_engine=story_engine,
        map_node_ids=map_node_ids,
        calendar=calendar,
        lexicon=lexicon,
    )
    world = builder.world_patches()
    cognition = builder.cognition_patches()
    narrative = builder.narrative_patches()
    if builder.issues:
        raise WorldSeedCommitError(builder.issues)
    common = {
        "project_id": project_id,
        "chapter_number": 0,
        "story_time": format_story_time(calendar, calendar.opening_minute),
        "story_time_start": calendar.opening_minute,
        "story_time_end": calendar.opening_minute,
        "source_type": GENESIS_SEED_SOURCE_TYPE,
        "source_id": revision_id,
    }
    world_id, cognition_id, narrative_id = world_seed_delta_ids(project_id)
    return [
        GraphDelta(id=world_id, delta_type=GraphDeltaType.WORLD_STATE, **common, **world),
        GraphDelta(id=cognition_id, delta_type=GraphDeltaType.COGNITION, cognition_patches=cognition, **common),
        GraphDelta(id=narrative_id, delta_type=GraphDeltaType.NARRATIVE_CONTROL, narrative_patches=narrative, **common),
    ]


class WorldSeedCommitter:
    def __init__(self, admission: CanonAdmissionService | None = None) -> None:
        self.admission = admission or CanonAdmissionService()

    def commit(
        self,
        *,
        session: Session,
        project: Project,
        revision: BookGenesisRevision,
        pack: Mapping,
    ) -> WorldSeedCommitSummary:
        """Write the seed as chapter-0 canon plus the book's calendar and lexicon; never commits."""
        pack = dict(pack)
        # A lock can be set by a raw stage-state patch, so this is the real gate.
        errors = [issue for issue in _world_seed_validation_issues(pack) if issue.severity == "error"]
        if errors:
            raise WorldSeedCommitError(f"{issue.code} {issue.path}: {issue.message}" for issue in errors)
        story_engine = _pack_stage_payload(pack, "story_engine")
        seed = normalize_world_seed(
            _pack_stage_payload(pack, "world_seed"),
            story_engine=story_engine,
            map_atlas=_pack_stage_payload(pack, "map"),
        )
        counts = _seed_counts(seed)
        delta_ids = world_seed_delta_ids(project.id)
        if BookStateRepository(session).graph_delta_ids_exist(list(delta_ids)) == set(delta_ids):
            return WorldSeedCommitSummary(skipped=True, graph_delta_ids=delta_ids, **counts)

        calendar = calendar_from_payload(seed["calendar"])
        lexicon = lexicon_from_payload(seed["lexicon"])
        StoryCalendarRepository(session).save(project.id, calendar)
        BookLexiconRepository(session).save(project.id, lexicon)
        changes = ApprovedGraphDeltaSet(
            project_id=project.id,
            chapter_number=0,
            graph_deltas=build_world_seed_deltas(
                project_id=project.id,
                revision_id=revision.id,
                seed=seed,
                story_engine=story_engine,
                map_node_ids=map_node_ids_by_genesis_ref(session, project.id),
                calendar=calendar,
                lexicon=lexicon,
            ),
            approved_by=[GENESIS_SEED_SOURCE_TYPE],
        )
        verdict = BookStateReviewGate(session).review(changes)
        if not verdict.accepted:
            raise WorldSeedCommitError(
                f"{issue.code} {issue.target_ref}: {issue.message}"
                for issue in verdict.issues
                if issue.severity == "error"
            )
        try:
            result = self.admission.commit_world_seed(
                session=session,
                project_id=project.id,
                revision_id=revision.id,
                approved_changes=changes,
            )
        except (CanonStaleVersion, CanonWriteFailure) as exc:
            raise WorldSeedCommitError([f"canon_conflict world_seed: {exc}"]) from exc
        StateUpdater(session).save_decision_event(
            DecisionEventInfo(
                project_id=project.id,
                scope="project",
                event_family="business_event",
                event_type=DecisionEventType.WORLD_SEED_COMMITTED,
                actor_type="system",
                summary="Genesis world_seed 已写入第 0 章 BookState。",
                payload=dict(counts),
                related_object_type="book_genesis_revision",
                related_object_id=revision.id,
            )
        )
        return WorldSeedCommitSummary(skipped=False, graph_delta_ids=tuple(result.graph_delta_ids), **counts)


def _seed_counts(seed: Mapping[str, Any]) -> dict[str, int]:
    actors = seed["actors"]
    return {
        "actor_count": len(actors),
        "agenda_count": sum(len(actor["agendas"]) for actor in actors),
        "intent_count": sum(len(actor["intents"]) for actor in actors),
        "clock_count": len(seed["clocks"]),
        "fact_count": len(seed["facts"]),
        "knowledge_count": len(seed["knowledge"]),
        "rule_count": len(seed["rules"]),
        "promise_count": len(seed["reader_promises"]),
    }


def _text(value: Any) -> str:
    return "" if value is None else str(value)


class _SeedDeltaBuilder:
    """Turns one normalized seed into create patches, collecting unresolvable references."""

    def __init__(
        self,
        *,
        project_id: str,
        seed: Mapping[str, Any],
        story_engine: Mapping,
        map_node_ids: Mapping[str, str],
        calendar: StoryCalendar,
        lexicon: BookLexicon,
    ) -> None:
        self.project_id = project_id
        self.seed = seed
        self.map_node_ids = map_node_ids
        self.calendar = calendar
        self.lexicon = lexicon
        self.opening = calendar.opening_minute
        self.issues: list[str] = []
        self.entities = {
            _text(item.get("id")): item
            for key in ("core_cast", "opposition", "factions")
            for item in story_engine.get(key) or []
            if isinstance(item, Mapping) and item.get("id")
        }
        self.actor_kinds = story_engine_actor_refs(story_engine)
        self.actor_ids = {ref: seed_ref_id(project_id, kind, ref) for ref, kind in self.actor_kinds.items()}
        self.fact_ids = {
            fact["id"]: seed_ref_id(project_id, "fact", fact["id"])
            for fact in seed["facts"]
            if fact["id"]
        }

    # resolution

    def _issue(self, code: str, path: str, detail: str) -> None:
        self.issues.append(f"{code} {path}: {detail}")

    def _item_id(self, item: Mapping, path: str) -> str:
        if not item["id"]:
            self._issue("missing_id", f"{path}.id", "seed items need an id to derive a BookState id")
        return item["id"]

    def _actor(self, ref: str, path: str) -> str:
        if ref not in self.actor_ids:
            self._issue("unresolved_ref", path, ref)
            return ""
        return self.actor_ids[ref]

    def _fact_ref(self, fact_id: str, path: str) -> str:
        if fact_id not in self.fact_ids:
            self._issue("unresolved_ref", path, fact_id)
            return ""
        return f"fact:{self.fact_ids[fact_id]}"

    def _refs(self, refs: list[str], path: str) -> list[str]:
        """Actor refs become node ids and fact ids become ``fact:<node id>``."""
        resolved: list[str] = []
        for index, ref in enumerate(refs):
            if ref in self.actor_ids:
                resolved.append(self.actor_ids[ref])
            else:
                resolved.append(self._fact_ref(ref, f"{path}[{index}]"))
        return resolved

    def _location(self, location_ref: str, path: str) -> str:
        if not location_ref:
            return ""
        if location_ref not in self.map_node_ids:
            self._issue("unresolved_location_ref", path, location_ref)
            return ""
        return self.map_node_ids[location_ref]

    def _rule_checkable(self, payload: Any, path: str) -> dict[str, Any] | None:
        if payload is None:
            return None
        predicate, errors = parse_checkable(payload, scope=None)
        if predicate is None:
            for error in errors:
                self._issue("invalid_checkable", path, error)
            return None
        params = predicate.params
        if predicate.kind == "actor_cannot_enter":
            params["location_ref"] = self._location(params["location_ref"], f"{path}.location_ref")
            for key in ("except_refs", "except_member_of"):
                params[key] = [self._actor(ref, f"{path}.{key}[{index}]") for index, ref in enumerate(params[key])]
        elif predicate.kind == "blocked_channel":
            params["fact_ref"] = self._fact_ref(params["fact_ref"], f"{path}.fact_ref")
            params["source_refs"] = [
                self._actor(ref, f"{path}.source_refs[{index}]") for index, ref in enumerate(params["source_refs"])
            ]
        elif predicate.kind == "requires_travel_mode":
            mode_ids = {mode.id for mode in self.lexicon.travel_modes}
            for index, ref in enumerate(params["mode_ids"]):
                if ref not in mode_ids:
                    self._issue("unresolved_travel_mode_ref", f"{path}.mode_ids[{index}]", ref)
        return {"kind": predicate.kind, **params}

    def _minutes_after_opening(self, duration: Mapping[str, Any]) -> int:
        return self.opening + duration_to_minutes(
            self.calendar, self.lexicon, amount=duration["amount"], unit_name=duration["unit"],
        )

    def _node(
        self, kind: str, ref: str, *, name: str, summary: str = "",
        profile: dict[str, Any], state: dict[str, Any],
    ) -> NodePatch:
        return NodePatch(
            node_id=seed_ref_id(self.project_id, kind, ref),
            node_type=kind,
            op="create",
            new_value={
                "project_id": self.project_id,
                "name": name,
                "summary": summary,
                "created_at_chapter": 0,
                "profile": profile,
                "state": state,
                "metadata": {"genesis_ref": ref, "source": GENESIS_SEED_SOURCE_TYPE},
            },
            reason=_PATCH_REASON,
        )

    # sections

    def world_patches(self) -> dict[str, list]:
        node_patches: list[NodePatch] = []
        for index, actor in enumerate(self.seed["actors"]):
            path = f"actors[{index}]"
            node_patches.append(self._actor_node(actor, path))
            for agenda_index, agenda in enumerate(actor["agendas"]):
                node_patches.append(self._agenda_node(actor, agenda, f"{path}.agendas[{agenda_index}]"))
            for intent_index, intent in enumerate(actor["intents"]):
                node_patches.append(self._intent_node(actor, intent, f"{path}.intents[{intent_index}]"))
        for index, rule in enumerate(self.seed["rules"]):
            path = f"rules[{index}]"
            rule_id = self._item_id(rule, path)
            node_patches.append(self._node(
                "rule", rule_id, name=rule["statement"],
                profile={
                    "statement": rule["statement"], "hardness": rule["hardness"],
                    "checkable": self._rule_checkable(rule["checkable"], f"{path}.checkable"),
                },
                state={"status": "active"},
            ))
        for index, clock in enumerate(self.seed["clocks"]):
            path = f"clocks[{index}]"
            clock_id = self._item_id(clock, path)
            owner_ref = self._actor(clock["owner_ref"], f"{path}.owner_ref") if clock["owner_ref"] else ""
            node_patches.append(self._node(
                "clock", clock_id, name=clock["label"],
                profile={
                    "label": clock["label"], "owner_ref": owner_ref,
                    "scope": clock["scope"], "on_expire": clock["on_expire"],
                },
                state={"due_at": self._minutes_after_opening(clock["due_in"]), "status": "running"},
            ))
        return {
            "node_patches": node_patches,
            "edge_patches": self._edge_patches(),
            "fact_patches": self._fact_patches(),
        }

    def _actor_node(self, actor: Mapping[str, Any], path: str) -> NodePatch:
        ref = actor["ref"]
        kind = self.actor_kinds.get(ref, "")
        if not kind:
            self._issue("unresolved_ref", f"{path}.ref", ref)
        entity = self.entities.get(ref, {})
        role = _text(entity.get("role"))
        culture_tag = _text(entity.get("culture_profile_id"))
        location_id = self._location(actor["location_ref"], f"{path}.location_ref")
        if kind == "faction":
            goal = _text(entity.get("goal"))
            return self._node(
                kind, ref, name=_text(entity.get("name")), summary=goal,
                profile={"narrative_role": role, "culture_tag": culture_tag},
                state={
                    "actor_tier": actor["actor_tier"], "next_turn_at": self.opening,
                    "location_id": location_id, "goal": goal, "resources": actor["resources"],
                },
            )
        faction_ref = _text(entity.get("affiliated_faction") or entity.get("backing_faction"))
        return self._node(
            "character", ref, name=_text(entity.get("name")), summary=role,
            profile={"narrative_role": role, "culture_tag": culture_tag, "fear": _text(entity.get("fear"))},
            state={
                "alive_status": "alive",
                "location_id": location_id,
                "restraint": {"state": "free", "by_ref": "", "since": None},
                "actor_tier": actor["actor_tier"],
                "next_turn_at": self.opening,
                "travel_mode_ids": actor["travel_mode_ids"],
                "resources": actor["resources"],
                "goal": _text(entity.get("desire")),
                "faction_id": self.actor_ids[faction_ref] if self.actor_kinds.get(faction_ref) == "faction" else "",
            },
        )

    def _agenda_node(self, actor: Mapping[str, Any], agenda: Mapping[str, Any], path: str) -> NodePatch:
        agenda_id = self._item_id(agenda, path)
        return self._node(
            "agenda", f"{actor['ref']}/{agenda_id}", name=agenda["goal"],
            profile={
                "owner_ref": self._actor(actor["ref"], path),
                "goal": agenda["goal"],
                "motivation": agenda["motivation"],
                "horizon": agenda["horizon"],
            },
            state={
                "status": "active",
                "priority": agenda["priority"],
                "means_refs": self._refs(agenda["means_refs"], f"{path}.means_refs"),
                "constraints": agenda["constraints"],
                "secrecy_refs": self._refs(agenda["secrecy_refs"], f"{path}.secrecy_refs"),
            },
        )

    def _intent_node(self, actor: Mapping[str, Any], intent: Mapping[str, Any], path: str) -> NodePatch:
        intent_id = self._item_id(intent, path)
        return self._node(
            "intent", f"{actor['ref']}/{intent_id}", name=intent["summary"],
            profile={
                "owner_ref": self._actor(actor["ref"], path),
                "agenda_ref": seed_ref_id(self.project_id, "agenda", f"{actor['ref']}/{intent['agenda_id']}"),
                "action_type": intent["action_type"],
                "summary": intent["summary"],
                "target_refs": self._refs(intent["target_refs"], f"{path}.target_refs"),
                "location_id": self._location(intent["location_ref"], f"{path}.location_ref"),
                "requires_onstage": intent["requires_onstage"],
                "impact_level": intent["impact_level"],
                "expected_effects": [],
                "preconditions": [],
            },
            state={
                "status": "planned",
                "scheduled_at": self._minutes_after_opening(intent["scheduled_in"]),
                "slip_count": 0,
            },
        )

    def _edge_patches(self) -> list[EdgePatch]:
        patches: list[EdgePatch] = []
        seen: set[str] = set()
        for index, relation in enumerate(self.seed["relations"]):
            path = f"relations[{index}]"
            edge_type = relation["edge_type"]
            ref = f"{relation['source']}/{edge_type}/{relation['target']}"
            if ref in seen:
                self._issue("duplicate_ref", path, ref)
            seen.add(ref)
            families = [family for family, edge_types in WORLD_EDGE_TYPES_BY_FAMILY.items() if edge_type in edge_types]
            if len(families) != 1:
                self._issue("invalid_edge_type", f"{path}.edge_type", edge_type)
            source_id = self._actor(relation["source"], f"{path}.source")
            target_id = self._actor(relation["target"], f"{path}.target")
            edge_family = families[0] if len(families) == 1 else ""
            patches.append(EdgePatch(
                edge_id=seed_ref_id(self.project_id, "edge", ref),
                op="create",
                source_id=source_id,
                target_id=target_id,
                edge_type=edge_type,
                edge_family=edge_family,
                new_value={
                    "project_id": self.project_id,
                    "source_id": source_id,
                    "target_id": target_id,
                    "edge_type": edge_type,
                    "edge_family": edge_family,
                    "established_at_chapter": 0,
                    "valid_from_chapter": 0,
                    "visibility_default": "hidden" if relation["visibility"] == "secret" else "visible",
                    "metadata": {"note": relation["note"], "appellation": relation["appellation"], "genesis_ref": ref},
                },
                reason=_PATCH_REASON,
            ))
        return patches

    def _fact_patches(self) -> list[FactPatch]:
        patches: list[FactPatch] = []
        for index, fact in enumerate(self.seed["facts"]):
            path = f"facts[{index}]"
            fact_id = self._item_id(fact, path)
            truth_value = "true" if fact["truth_value"] else "false"
            related_refs = self._refs(fact["related_refs"], f"{path}.related_refs")
            patches.append(FactPatch(
                fact_id=seed_ref_id(self.project_id, "fact", fact_id),
                op="create",
                proposition=fact["proposition"],
                truth_value=truth_value,
                related_refs=related_refs,
                sensitivity_level=fact["sensitivity"],
                new_value={
                    "project_id": self.project_id,
                    "proposition": fact["proposition"],
                    "truth_value": truth_value,
                    "related_node_refs": related_refs,
                    "sensitivity_level": fact["sensitivity"],
                    "created_at_chapter": 0,
                    "metadata": {"genesis_ref": fact_id, "source": GENESIS_SEED_SOURCE_TYPE},
                },
                reason=_PATCH_REASON,
            ))
        return patches

    def cognition_patches(self) -> list[CognitionPatch]:
        patches: list[CognitionPatch] = []
        for index, item in enumerate(self.seed["knowledge"]):
            path = f"knowledge[{index}]"
            observer_ref = item["observer_ref"]
            observer_id = self._actor(observer_ref, f"{path}.observer_ref")
            fact_ref = self._fact_ref(item["fact_id"], f"{path}.fact_id")
            if not observer_id or not fact_ref:
                continue
            patches.append(knowledge_event_to_patch(KnowledgeEvent(
                observer_type=OBSERVER_TYPES[self.actor_kinds[observer_ref]],
                observer_id=observer_id,
                fact_ref=fact_ref,
                state=item["state"],
                channel="seeded",
                at=self.opening,
            )))
        return patches

    def narrative_patches(self) -> list[NarrativePatch]:
        patches: list[NarrativePatch] = []
        for index, item in enumerate(self.seed["reader_promises"]):
            promise_id = seed_ref_id(self.project_id, "promise", self._item_id(item, f"reader_promises[{index}]"))
            promise = ReaderPromise(
                promise_id=promise_id,
                project_id=self.project_id,
                promise_type=item["promise_type"],
                summary=item["summary"],
                created_at_chapter=0,
                expected_payoff_window=item["expected_payoff_window"],
                status="open",
            )
            patches.append(NarrativePatch(
                target_ref=f"promise:{promise_id}",
                op="create",
                new_value=promise_node_payload(promise),
                reason=_PATCH_REASON,
            ))
        return patches


__all__ = [
    "OBSERVER_TYPES",
    "SEED_DELTA_SECTIONS",
    "WorldSeedCommitError",
    "WorldSeedCommitSummary",
    "WorldSeedCommitter",
    "build_world_seed_deltas",
    "map_node_ids_by_genesis_ref",
    "seed_ref_id",
    "world_seed_delta_ids",
]
