"""One world tick's candidates as the tick's single world_engine GraphDelta."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from forwin.book_state.book_frame_repository import BookLexiconRepository, StoryCalendarRepository
from forwin.book_state.extraction.chapter_delta_schema import Duration
from forwin.book_state.extraction.translator import (
    _OBSERVER_TYPES,
    _SYMMETRIC_EDGE_TYPES,
    _current_node_value,
    _edge_field_path,
    _leaf_patch,
    _stable_id,
)
from forwin.book_state.knowledge import FACT_REF_PREFIX, KnowledgeEvent, knowledge_event_to_patch
from forwin.book_state.narrative import PROTECTION_STATUS_PATH, RELEASED_PROTECTION_STATUS
from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.path_patch import apply_path_patch
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.runtime import BookStateRuntime
from forwin.book_state.schema import ACTOR_NODE_TYPES, WORLD_NODE_FIELDS
from forwin.book_state.story_clock import StoryCalendar, StoryClockError, duration_to_minutes, format_story_time
from forwin.protocol.book_state import (
    WORLD_EDGE_TYPES_BY_FAMILY,
    CognitionPatch,
    Directionality,
    EdgePatch,
    FactPatch,
    GraphDelta,
    GraphDeltaType,
    NarrativePatch,
    NodePatch,
    WorldEdge,
    WorldNode,
)

from .types import (
    AGENDA_PRIORITY_MAX,
    AGENDA_PRIORITY_MIN,
    NEW_AGENDA_REF,
    NEW_FACT_REF_PREFIX,
    WORLD_ENGINE_SOURCE_TYPE,
    AgendaChange,
    EffectEntityChange,
    EffectEvent,
    EffectFact,
    EffectItemTransfer,
    EffectMovement,
    EffectRelationChange,
    NewsItem,
    ProposedIntent,
    TickCandidate,
    TickContext,
)


# The narrative target_ref of a protection, a review_constraint node.
_PROTECTION_REF_PREFIX = "review_constraint:"


def _tick_local_id(project_id: str, tick_id: str, candidate_id: str, kind: str, local: int | str) -> str:
    """A stable id for something one candidate creates; a rebuild without other candidates keeps it."""
    return _stable_id(project_id, kind, "\0".join((tick_id, candidate_id, str(local))))


def tick_event_fact_ref(project_id: str, tick_id: str, candidate_id: str, event_index: int) -> str:
    """``fact:<id>`` of the fact the builder creates for a candidate's ``effects.events[event_index]``.

    Later candidates of the same tick, such as its news, reference the event's fact by this ref.
    """
    event_id = _tick_local_id(project_id, tick_id, candidate_id, "event", event_index)
    return f"{FACT_REF_PREFIX}{_stable_id(project_id, 'fact', event_id)}"


class TickIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Literal["error", "warning"]
    code: str
    message: str
    # The candidate to drop and rebuild without; empty for an issue of the tick as a whole.
    candidate_id: str


@dataclass(frozen=True)
class TickDeltaBuild:
    # None whenever an issue is an error.
    delta: GraphDelta | None
    issues: list[TickIssue]


class WorldTickDeltaBuilder:
    """Build a tick's candidates into the tick's single world_engine GraphDelta."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def build(
        self,
        *,
        context: TickContext,
        candidates: Sequence[TickCandidate],
        news_in_transit: Sequence[NewsItem],
        tick_id: str,
    ) -> TickDeltaBuild:
        project_id = context.project_id
        builder = _TickBuilder(
            context=context,
            tick_id=tick_id,
            # The tick lands after the chapter's own delta, so it builds on the committed chapter.
            runtime=BookStateProjection(self.session).load_runtime_as_of(
                project_id, as_of_chapter=context.chapter_number,
            ),
            calendar=StoryCalendarRepository(self.session).get(project_id),
            lexicon=BookLexiconRepository(self.session).get(project_id),
        )
        return builder.build(candidates, news_in_transit)


class _TickBuilder:
    """One build: patches read the as-of N runtime, so every old_value is the pre-tick value.

    Effects take the translator's patch forms and old_value rules, and the movements,
    item_transfers and knowledge_tags metadata take its shapes, so the world gate reads both alike.
    """

    def __init__(
        self,
        *,
        context: TickContext,
        tick_id: str,
        runtime: BookStateRuntime,
        calendar: StoryCalendar | None,
        lexicon: BookLexicon,
    ) -> None:
        self.context = context
        self.project_id = context.project_id
        self.chapter = context.chapter_number
        self.tick_id = tick_id
        self.runtime = runtime
        self.world = runtime.world
        self.map = runtime.map
        self.calendar = calendar
        self.lexicon = lexicon
        self.issues: list[TickIssue] = []
        self.node_patches: list[NodePatch] = []
        self.created_nodes: dict[str, NodePatch] = {}
        self.edge_patches: list[EdgePatch] = []
        self.fact_patches: list[FactPatch] = []
        self.created_facts: set[str] = set()
        self.cognition_patches: list[CognitionPatch] = []
        self.narrative_patches: list[NarrativePatch] = []
        self.locations: dict[str, str] = {}
        self.holders: dict[str, str] = {}
        self.movements: list[dict[str, Any]] = []
        self.item_transfers: list[dict[str, Any]] = []
        self.knowledge_tags: list[dict[str, Any]] = []
        # The candidate being built: its id, summary, new agendas and new facts by key.
        self.candidate_id = ""
        self.reason = ""
        self.new_agendas: list[str] = []
        self.new_facts: dict[str, str] = {}

    def build(self, candidates: Sequence[TickCandidate], news_in_transit: Sequence[NewsItem]) -> TickDeltaBuild:
        seen: set[str] = set()
        for candidate in candidates:
            self.candidate_id = candidate.candidate_id
            self.reason = candidate.summary.strip()
            self.new_agendas = []
            self.new_facts = {}
            if candidate.candidate_id in seen:
                self._issue("error", "duplicate_candidate", f"candidate id {candidate.candidate_id!r} repeats")
                continue
            seen.add(candidate.candidate_id)
            self._candidate(candidate)
        self.candidate_id, self.reason = "", ""
        news = self._news(news_in_transit)
        issues = list(self.issues)
        if any(issue.severity == "error" for issue in issues):
            return TickDeltaBuild(None, issues)
        now = self.context.now
        delta = GraphDelta(
            id=f"world-tick:{self.project_id}:{self.tick_id}",
            project_id=self.project_id,
            chapter_number=self.chapter,
            story_time=format_story_time(self.calendar, now) if self.calendar is not None else "",
            story_time_start=self.context.window_start,
            story_time_end=now,
            delta_type=GraphDeltaType.WORLD_STATE,
            operation="apply_world_tick",
            target_type="chapter",
            target_id=f"chapter:{self.chapter}",
            source_type=WORLD_ENGINE_SOURCE_TYPE,
            source_id=self.tick_id,
            world_line_id="main",
            summary="\n".join(text for candidate in candidates if (text := candidate.summary.strip())),
            node_patches=self.node_patches,
            edge_patches=self.edge_patches,
            fact_patches=self.fact_patches,
            cognition_patches=self.cognition_patches,
            narrative_patches=self.narrative_patches,
            evidence_refs=[self._evidence_ref()],
            metadata={
                "tick_id": self.tick_id,
                "seed": self.context.seed,
                "source_chapter": self.chapter,
                "movements": self.movements,
                "item_transfers": self.item_transfers,
                "knowledge_tags": self.knowledge_tags,
                "news_in_transit": news,
                "candidate_ids": [candidate.candidate_id for candidate in candidates],
                "candidate_impacts": {candidate.candidate_id: candidate.impact_level for candidate in candidates},
            },
        )
        return TickDeltaBuild(delta, issues)

    def _candidate(self, candidate: TickCandidate) -> None:
        for index, change in enumerate(candidate.agenda_changes):
            self._agenda_change(candidate, change, index)
        for index, intent in enumerate(candidate.new_intents):
            self._new_intent(candidate, intent, index)
        for intent_ref, status in candidate.intent_status_changes.items():
            intent_id = self._node_of_type(intent_ref, {"intent"}, "intent")
            if intent_id is not None:
                self._set_node_field(intent_id, "state.status", status)
        for intent_ref, slip_count in candidate.slip_updates.items():
            intent_id = self._node_of_type(intent_ref, {"intent"}, "intent")
            if intent_id is not None:
                # A slip puts the intent off to now, to be planned again.
                self._set_node_field(intent_id, "state.slip_count", slip_count)
                self._set_node_field(intent_id, "state.scheduled_at", self.context.now)
        for actor_ref, minute in candidate.next_turn_at.items():
            actor_id = self._node_of_type(actor_ref, ACTOR_NODE_TYPES, "actor")
            if actor_id is not None:
                self._set_node_field(actor_id, "state.next_turn_at", minute)
        for constraint_ref in candidate.protection_releases:
            self._release_protection(constraint_ref)
        effects = candidate.effects
        for fact in effects.new_facts:
            self._new_fact(fact)
        movers = {movement.entity_ref.strip() for movement in effects.movements}
        for change in effects.entity_changes:
            self._entity_change(change, movers)
        for relation in effects.relation_changes:
            self._relation_change(relation)
        for movement in effects.movements:
            self._movement(movement)
        for transfer in effects.item_transfers:
            self._item_transfer(transfer)
        for index, event in enumerate(effects.events):
            self._event(candidate, event, index)
        for item in effects.knowledge:
            self._knowledge(
                item.observer_ref, item.fact_ref, state=item.state, channel=item.channel, source_ref=item.source_ref,
            )

    # issues and provenance

    def _issue(self, severity: str, code: str, message: str) -> None:
        self.issues.append(TickIssue(severity=severity, code=code, message=message, candidate_id=self.candidate_id))

    def _unresolved(self, message: str) -> None:
        self._issue("error", "unresolved_ref", message)

    def _evidence_ref(self) -> str:
        return f"world_tick:{self.tick_id}"

    def _provenance(self) -> dict[str, str]:
        return {"source": WORLD_ENGINE_SOURCE_TYPE, "tick_id": self.tick_id, "candidate_id": self.candidate_id}

    def _local_id(self, kind: str, local: int | str) -> str:
        return _tick_local_id(self.project_id, self.tick_id, self.candidate_id, kind, local)

    # references

    def _node(self, ref: str, label: str) -> str | None:
        text = ref.strip()
        if text in self.world.nodes_by_id or text in self.created_nodes:
            return text
        self._unresolved(f"{label} {text!r} is not a BookState node")
        return None

    def _node_of_type(self, ref: str, node_types: Collection[str], label: str) -> str | None:
        node_id = self._node(ref, label)
        if node_id is None:
            return None
        if self._node_type(node_id) not in node_types:
            self._unresolved(f"{label} {node_id!r} is a {self._node_type(node_id)}, not one of {sorted(node_types)}")
            return None
        return node_id

    def _owner(self, candidate: TickCandidate) -> str | None:
        if not candidate.actor_id.strip():
            self._unresolved("the candidate creates an agenda or intent but names no actor")
            return None
        return self._node_of_type(candidate.actor_id, ACTOR_NODE_TYPES, "actor")

    def _location(self, ref: str, label: str) -> str | None:
        text = ref.strip()
        if text in self.map.nodes_by_id:
            return text
        self._unresolved(f"{label} {text!r} is not a map node")
        return None

    def _holder(self, ref: str, label: str) -> str | None:
        return self._location(ref, label) if ref.strip() in self.map.nodes_by_id else self._node(ref, label)

    def _observer(self, ref: str) -> tuple[str, str] | None:
        node_id = self._node(ref, "observer")
        if node_id is None:
            return None
        observer_type = _OBSERVER_TYPES.get(self._node_type(node_id))
        if observer_type is None:
            self._unresolved(f"{node_id!r} is not an observer")
            return None
        return observer_type, node_id

    def _fact_ref(self, ref: str) -> str | None:
        """``fact:<id>`` of an existing or created fact; ``fact:new:<key>`` names this candidate's new fact."""
        text = ref.strip()
        if text.startswith(NEW_FACT_REF_PREFIX):
            resolved = self.new_facts.get(text.removeprefix(NEW_FACT_REF_PREFIX))
            if resolved is not None:
                return resolved
        elif text.startswith(FACT_REF_PREFIX) and self._fact_exists(text.removeprefix(FACT_REF_PREFIX)):
            return text
        self._unresolved(f"fact ref {text!r} names no fact")
        return None

    def _fact_exists(self, fact_id: str) -> bool:
        return fact_id in self.world.facts_by_id or fact_id in self.created_facts

    def _target_ref(self, ref: str) -> str | None:
        """An intent target: a BookState node, a map node or a fact."""
        text = ref.strip()
        if text.startswith(FACT_REF_PREFIX):
            return self._fact_ref(text)
        if text in self.map.nodes_by_id:
            return text
        return self._node(text, "intent target")

    # nodes

    def _node_type(self, node_id: str) -> str:
        node = self.world.nodes_by_id.get(node_id)
        return str(node.node_type) if node is not None else str(self.created_nodes[node_id].node_type)

    def _create_node(self, node: WorldNode) -> None:
        patch = NodePatch(
            node_id=node.id,
            node_type=str(node.node_type),
            op="create",
            new_value=node.model_dump(mode="json", exclude={"id", "node_type"}),
            reason=self.reason,
        )
        self.node_patches.append(patch)
        self.created_nodes[node.id] = patch

    def _field_path(self, node_id: str, field: str) -> str | None:
        node_type = self._node_type(node_id)
        text = field.strip()
        fields = WORLD_NODE_FIELDS.get(node_type, {})
        for section in ("state", "profile"):
            if text.split(".", 1)[0] in fields.get(section, set()):
                return f"{section}.{text}"
        self._issue("error", "unknown_field", f"{text!r} is not a {node_type} state or profile field")
        return None

    def _set_node_field(self, node_id: str, field_path: str, value: Any) -> None:
        created = self.created_nodes.get(node_id)
        if created is not None:
            # A node created by this delta takes the value in its create payload.
            apply_path_patch(created.new_value, field_path, value)
            return
        if field_path.startswith("state."):
            # State is snapshot-backed, so a field without a value yet needs no old_value.
            leaf = ("set", field_path, _current_node_value(self.runtime, node_id, field_path), value)
        else:
            leaf = _leaf_patch(self.world.nodes_by_id[node_id].model_dump(mode="json"), field_path, value)
        if leaf is None:
            # Historical rewind restores non-state fields from old_value.
            self._issue("error", "missing_old_value", f"{node_id}:{field_path} has no current value or parent")
            return
        op, patch_path, old_value, new_value = leaf
        self.node_patches.append(NodePatch(
            node_id=node_id,
            node_type=self._node_type(node_id),
            op=op,
            field_path=patch_path,
            old_value=old_value,
            new_value=new_value,
            reason=self.reason,
        ))

    # protections

    def _release_protection(self, ref: str) -> None:
        """Release a protection by status, including at chapter zero."""
        node = self.runtime.narrative.nodes_by_id.get(ref.strip())
        if node is None or node.node_type != "review_constraint":
            self._unresolved(f"protection {ref.strip()!r} is not a review_constraint node")
            return
        old_value = node.payload.get("status")
        self.narrative_patches.append(NarrativePatch(
            target_ref=f"{_PROTECTION_REF_PREFIX}{node.id}",
            op="set",
            field_path=PROTECTION_STATUS_PATH,
            old_value=old_value,
            new_value=RELEASED_PROTECTION_STATUS,
            reason=self.reason,
            evidence_refs=[self._evidence_ref()],
        ))

    # agendas and intents

    def _agenda_change(self, candidate: TickCandidate, change: AgendaChange, index: int) -> None:
        priority = (
            None if change.priority is None
            else min(max(change.priority, AGENDA_PRIORITY_MIN), AGENDA_PRIORITY_MAX)
        )
        if change.agenda_ref == NEW_AGENDA_REF:
            owner = self._owner(candidate)
            if owner is None:
                return
            agenda_id = self._local_id("agenda", index)
            goal = change.goal.strip()
            state: dict[str, Any] = {"status": change.status or "active"}
            if priority is not None:
                state["priority"] = priority
            self._create_node(WorldNode(
                id=agenda_id,
                project_id=self.project_id,
                node_type="agenda",
                name=goal,
                created_at_chapter=self.chapter,
                profile={"owner_ref": owner, "goal": goal, "motivation": change.motivation.strip()},
                state={**state, "means_refs": [], "constraints": [], "secrecy_refs": []},
                metadata=self._provenance(),
            ))
            self.new_agendas.append(agenda_id)
            return
        agenda_id = self._node_of_type(change.agenda_ref, {"agenda"}, "agenda")
        if agenda_id is None:
            return
        # Empty fields leave a field unchanged.
        for field_path, value in (
            ("state.status", change.status),
            ("state.priority", priority),
            ("profile.goal", change.goal.strip()),
            ("profile.motivation", change.motivation.strip()),
        ):
            if value not in ("", None):
                self._set_node_field(agenda_id, field_path, value)

    def _new_intent(self, candidate: TickCandidate, intent: ProposedIntent, index: int) -> None:
        owner = self._owner(candidate)
        agenda_id = self._intent_agenda(intent.agenda_ref)
        targets = [self._target_ref(ref) for ref in intent.target_refs]
        location_id = self._location(intent.location_id, "intent location") if intent.location_id.strip() else ""
        scheduled_at = self._scheduled_at(intent.scheduled_in)
        if owner is None or agenda_id is None or None in targets or location_id is None or scheduled_at is None:
            return
        summary = intent.summary.strip()
        self._create_node(WorldNode(
            id=self._local_id("intent", index),
            project_id=self.project_id,
            node_type="intent",
            name=summary,
            summary=summary,
            created_at_chapter=self.chapter,
            profile={
                "owner_ref": owner,
                "agenda_ref": agenda_id,
                "action_type": intent.action_type,
                "summary": summary,
                "target_refs": targets,
                "location_id": location_id,
                "requires_onstage": intent.requires_onstage,
                "impact_level": intent.impact_level,
                "expected_effects": intent.expected_effects.model_dump(mode="json"),
                "preconditions": [],
            },
            state={"status": "planned", "scheduled_at": scheduled_at, "slip_count": 0},
            metadata=self._provenance(),
        ))

    def _intent_agenda(self, ref: str) -> str | None:
        """An intent's agenda: none, an existing agenda, or ``new`` for the candidate's one new agenda."""
        text = ref.strip()
        if not text:
            return ""
        if text != NEW_AGENDA_REF:
            return self._node_of_type(text, {"agenda"}, "intent agenda")
        if len(self.new_agendas) == 1:
            return self.new_agendas[0]
        self._unresolved(
            f"intent agenda {NEW_AGENDA_REF!r} needs exactly one new agenda, found {len(self.new_agendas)}"
        )
        return None

    def _scheduled_at(self, wait: Duration) -> int | None:
        """``now`` plus the wait in the book's units; a negative or zero wait is now."""
        calendar = self.calendar if self.calendar is not None else StoryCalendar("", 0, ())
        try:
            minutes = duration_to_minutes(calendar, self.lexicon, amount=max(wait.amount, 0.0), unit_name=wait.unit)
        except StoryClockError as exc:
            self._issue("error", "invalid_duration", f"scheduled_in {wait.amount} {wait.unit!r}: {exc}")
            return None
        return self.context.now + minutes

    # effects

    def _entity_change(self, change: EffectEntityChange, movers: set[str]) -> None:
        node_id = self._node(change.ref, "entity")
        if node_id is None:
            return
        field_path = self._field_path(node_id, change.field)
        if field_path is None:
            return
        # Location and holder changes are moves and transfers, which the review gate checks.
        node_type = self._node_type(node_id)
        if field_path == "state.location_id" and node_type == "character":
            if {node_id, change.ref.strip()} & movers:
                # The candidate's own movement for this entity carries the move.
                return
            to_id = self._location(str(change.new_value or ""), "location")
            if to_id is not None:
                self._record_movement(node_id, to_id)
        elif field_path == "state.holder_id" and node_type == "item":
            to_id = self._holder(str(change.new_value or ""), "holder")
            if to_id is not None:
                self._record_transfer(node_id, "", to_id)
        else:
            self._set_node_field(node_id, field_path, change.new_value)

    def _relation_change(self, change: EffectRelationChange) -> None:
        families = [
            family for family, edge_types in WORLD_EDGE_TYPES_BY_FAMILY.items() if change.edge_type in edge_types
        ]
        if len(families) != 1:
            self._issue("error", "edge_type_invalid", f"{change.edge_type!r} has no unique edge family")
            return
        source_id = self._relation_endpoint(change.source_ref)
        target_id = self._relation_endpoint(change.target_ref)
        if source_id is None or target_id is None:
            return
        symmetric = change.edge_type in _SYMMETRIC_EDGE_TYPES
        same_type = sorted(
            (edge for edge in self.world.edges_by_id.values() if edge.edge_type == change.edge_type),
            key=lambda edge: edge.id,
        )
        forward = [edge for edge in same_type if (edge.source_id, edge.target_id) == (source_id, target_id)]
        backward = [edge for edge in same_type if (edge.source_id, edge.target_id) == (target_id, source_id)]
        matching = forward + backward if symmetric else forward
        active = next((edge for edge in matching if edge.is_active), None)
        if change.change == "create":
            # A create earlier in this tick counts as existing too.
            pairs = {(source_id, target_id), (target_id, source_id)} if symmetric else {(source_id, target_id)}
            pending = next(
                (
                    patch for patch in self.edge_patches
                    if str(patch.op) == "create" and patch.edge_type == change.edge_type
                    and (patch.source_id, patch.target_id) in pairs
                ),
                None,
            )
            existing_id = active.id if active is not None else pending.edge_id if pending is not None else ""
            if existing_id:
                self._issue("warning", "relation_exists", f"relation {existing_id} already exists")
            elif not symmetric and not forward and any(edge.is_active for edge in backward):
                # An ended reverse edge is a past role; only a live one blocks the flip.
                self._direction_mismatch(change)
            else:
                self._create_relation(change, matching, source_id, target_id, families[0])
            return
        if active is None:
            if not symmetric and any(edge.is_active for edge in backward):
                self._direction_mismatch(change)
            else:
                self._issue("warning", "relation_missing", f"no active {change.edge_type} relation to {change.change}")
            return
        endpoints = {
            "source_id": active.source_id, "target_id": active.target_id,
            "edge_type": active.edge_type, "edge_family": str(active.edge_family),
        }
        if change.change == "end":
            self.edge_patches.append(EdgePatch(
                edge_id=active.id, op="deactivate", **endpoints,
                field_path="is_active", old_value=True, reason=self.reason,
            ))
            return
        field_path = _edge_field_path(change.field)
        if field_path is None:
            self._issue("error", "unknown_field", f"{change.field!r} is not a settable relation field")
            return
        leaf = _leaf_patch(active.model_dump(mode="json"), field_path, change.new_value)
        if leaf is None:
            self._issue("error", "missing_old_value", f"{active.id}:{field_path} has no current value or parent")
            return
        op, patch_path, old_value, new_value = leaf
        self.edge_patches.append(EdgePatch(
            edge_id=active.id, op=op, **endpoints, field_path=patch_path,
            old_value=old_value, new_value=new_value, reason=self.reason,
        ))

    def _create_relation(
        self,
        change: EffectRelationChange,
        matching: list[WorldEdge],
        source_id: str,
        target_id: str,
        edge_family: str,
    ) -> None:
        ended = [edge for edge in matching if edge.ended_at_chapter is not None]
        # A create on an ended relation reactivates that same edge.
        symmetric = change.edge_type in _SYMMETRIC_EDGE_TYPES
        # A symmetric relation has one id whichever endpoint comes first.
        first, second = sorted((source_id, target_id)) if symmetric else (source_id, target_id)
        edge_id = (
            max(ended, key=lambda edge: (edge.ended_at_chapter, edge.id)).id
            if ended
            else _stable_id(self.project_id, "edge", f"{first}/{change.edge_type}/{second}")
        )
        if any(patch.edge_id == edge_id for patch in self.edge_patches):
            return
        endpoints = {
            "source_id": source_id, "target_id": target_id,
            "edge_type": change.edge_type, "edge_family": edge_family,
        }
        directionality = Directionality.UNDIRECTED if symmetric else Directionality.DIRECTED
        self.edge_patches.append(EdgePatch(
            edge_id=edge_id,
            op="create",
            **endpoints,
            new_value={
                "project_id": self.project_id,
                **endpoints,
                "directionality": directionality.value,
                "established_at_chapter": self.chapter,
                "valid_from_chapter": self.chapter,
                "evidence_refs": [self._evidence_ref()],
                "metadata": self._provenance(),
            },
            reason=self.reason,
        ))

    def _direction_mismatch(self, change: EffectRelationChange) -> None:
        self._issue(
            "warning", "relation_direction_mismatch",
            f"only the reverse {change.edge_type} relation exists; directed relations are not flipped",
        )

    def _relation_endpoint(self, ref: str) -> str | None:
        """A BookState node, or a map node id standing for the site_state node of that site."""
        text = ref.strip()
        if text not in self.map.nodes_by_id or text in self.world.nodes_by_id:
            return self._node(text, "relation endpoint")
        sites = [
            node_id for node_id, node in sorted(self.world.nodes_by_id.items())
            if str(node.node_type) == "site_state" and node.profile.get("map_node_id") == text
        ]
        if not sites:
            self._unresolved(f"location {text!r} has no site_state node")
            return None
        return sites[0]

    def _movement(self, movement: EffectMovement) -> None:
        mover = self._node(movement.entity_ref, "mover")
        to_id = self._location(movement.to_location_id, "destination")
        if mover is None or to_id is None:
            return
        if "location_id" not in WORLD_NODE_FIELDS.get(self._node_type(mover), {}).get("state", set()):
            self._issue("error", "unknown_field", f"{mover!r} has no state.location_id")
            return
        self._record_movement(
            mover, to_id, depart_minute=movement.depart_at, arrive_minute=movement.arrive_at,
            travel_mode_id=movement.travel_mode_id,
        )

    def _record_movement(
        self,
        mover: str,
        to_id: str,
        *,
        depart_minute: int | None = None,
        arrive_minute: int | None = None,
        travel_mode_id: str = "",
    ) -> None:
        from_id = self._current_state(mover, "location_id", self.locations)
        self._set_node_field(mover, "state.location_id", to_id)
        self.locations[mover] = to_id
        self.movements.append({
            "entity_id": mover,
            "from_id": from_id,
            "to_id": to_id,
            "depart_minute": depart_minute,
            "arrive_minute": arrive_minute,
            "travel_mode_id": travel_mode_id,
            "evidence": self.reason,
        })

    def _item_transfer(self, transfer: EffectItemTransfer) -> None:
        item_id = self._node(transfer.item_ref, "item")
        to_id = self._holder(transfer.to_ref, "recipient")
        from_id = self._holder(transfer.from_ref, "giver") if transfer.from_ref.strip() else ""
        if item_id is None or to_id is None or from_id is None:
            return
        if "holder_id" not in WORLD_NODE_FIELDS.get(self._node_type(item_id), {}).get("state", set()):
            self._issue("error", "unknown_field", f"{item_id!r} has no state.holder_id")
            return
        self._record_transfer(item_id, from_id, to_id)

    def _record_transfer(self, item_id: str, from_id: str, to_id: str) -> None:
        from_id = from_id or self._current_state(item_id, "holder_id", self.holders)
        self._set_node_field(item_id, "state.holder_id", to_id)
        self.holders[item_id] = to_id
        self.item_transfers.append({"item_id": item_id, "from_id": from_id, "to_id": to_id, "evidence": self.reason})

    def _current_state(self, node_id: str, field: str, moved: dict[str, str]) -> str:
        """The field as earlier entries of this tick left it, else its pre-tick value."""
        if node_id in moved:
            return moved[node_id]
        created = self.created_nodes.get(node_id)
        if created is not None:
            return str(created.new_value.get("state", {}).get(field) or "")
        return str(_current_node_value(self.runtime, node_id, f"state.{field}") or "")

    def _event(self, candidate: TickCandidate, event: EffectEvent, index: int) -> None:
        participants: list[str] = []
        resolved = True
        for ref in event.participant_refs:
            node_id = self._node(ref, "event participant")
            if node_id is None:
                resolved = False
            elif node_id not in participants:
                participants.append(node_id)
        location_id = self._location(event.location_id, "event location") if event.location_id.strip() else ""
        if not resolved or location_id is None:
            return
        summary = event.summary.strip()
        event_id = self._local_id("event", index)
        fact_ref = tick_event_fact_ref(self.project_id, self.tick_id, self.candidate_id, index)
        fact_id = fact_ref.removeprefix(FACT_REF_PREFIX)
        sensitivity = "secret" if event.visibility == "secret" else "public"
        self._create_node(WorldNode(
            id=event_id,
            project_id=self.project_id,
            node_type="event",
            name=summary[:80],
            summary=summary,
            description=summary,
            created_at_chapter=self.chapter,
            profile={
                "event_type": candidate.impact_level,
                "planned_or_actual": "actual",
                "expected_chapter": self.chapter,
                "involved_node_refs": [f"node:{node_id}" for node_id in participants],
                "location_id": location_id,
                "public_summary": summary if sensitivity == "public" else "",
            },
            state={
                "status": "occurred",
                "actual_chapter": self.chapter,
                "actual_story_time": event.at,
                "participant_ids": participants,
                "result_refs": [fact_ref],
                "visibility_level": event.visibility,
                "state_summary": summary,
            },
            metadata=self._provenance(),
        ))
        related = [event_id, *participants]
        self._create_fact(
            fact_id, proposition=summary, truth_value=True, sensitivity=sensitivity, related=related,
            extra={
                "fact_type": "event",
                "confidence": 1.0,
                "state": {
                    "status": "active",
                    "last_updated_chapter": self.chapter,
                    "canonicality_level": "canon",
                    "state_summary": summary,
                },
            },
        )

    def _new_fact(self, fact: EffectFact) -> None:
        # Keys are the candidate's own: two candidates' k1 are two facts.
        fact_id = self._local_id("fact", fact.key)
        fact_ref = f"{FACT_REF_PREFIX}{fact_id}"
        self.new_facts[fact.key] = fact_ref
        self._create_fact(
            fact_id, proposition=fact.proposition.strip(), truth_value=fact.truth_value,
            sensitivity=fact.sensitivity, related=[],
        )
        for observer_ref in fact.known_by:
            self._knowledge(observer_ref, fact_ref, state="knows", channel="witnessed", source_ref="")

    def _create_fact(
        self,
        fact_id: str,
        *,
        proposition: str,
        truth_value: bool,
        sensitivity: str,
        related: list[str],
        extra: dict[str, Any] | None = None,
    ) -> None:
        truth = "true" if truth_value else "false"
        self.fact_patches.append(FactPatch(
            fact_id=fact_id,
            op="create",
            proposition=proposition,
            truth_value=truth,
            related_refs=related,
            sensitivity_level=sensitivity,
            new_value={
                "project_id": self.project_id,
                "proposition": proposition,
                "truth_value": truth,
                "related_node_refs": related,
                "source_refs": [self._evidence_ref()],
                "created_at_chapter": self.chapter,
                "sensitivity_level": sensitivity,
                **(extra or {}),
                "metadata": self._provenance(),
            },
            reason=self.reason,
        ))
        self.created_facts.add(fact_id)

    def _knowledge(self, observer_ref: str, fact_ref: str, *, state: str, channel: str, source_ref: str) -> None:
        observer = self._observer(observer_ref)
        resolved_fact = self._fact_ref(fact_ref)
        source_id = self._node(source_ref, "knowledge source") if source_ref.strip() else ""
        if observer is None or resolved_fact is None or source_id is None:
            return
        observer_type, observer_id = observer
        view = self.runtime.cognition_by_observer.get(observer)
        patch = knowledge_event_to_patch(KnowledgeEvent(
            observer_type=observer_type, observer_id=observer_id, fact_ref=resolved_fact,
            state=state, channel=channel, source_ref=source_id, at=self.context.now,
        ))
        self.cognition_patches.append(patch.model_copy(update={
            "old_value": {resolved_fact: view.knowledge_by_ref.get(resolved_fact) if view is not None else None},
            "reason": self.reason,
        }))
        self.knowledge_tags.append({
            "observer_type": observer_type,
            "observer_id": observer_id,
            "fact_ref": resolved_fact,
            "state": state,
            "channel": channel,
            "source_id": source_id,
            "touches_secret_ref": "",
            "contradicts_ref": "",
            "flashback": False,
            "evidence": self.reason,
        })

    # news

    def _news(self, items: Sequence[NewsItem]) -> list[dict[str, Any]]:
        """The queue to carry forward; an item naming no observer node or no fact is dropped."""
        queue: list[dict[str, Any]] = []
        for item in items:
            observer = item.observer_ref.strip()
            fact = item.fact_ref.strip()
            known_observer = (
                observer in self.world.nodes_by_id or observer in self.created_nodes
            ) and _OBSERVER_TYPES.get(self._node_type(observer)) is not None
            if known_observer and not fact.startswith(NEW_FACT_REF_PREFIX) and self._fact_exists(
                fact.removeprefix(FACT_REF_PREFIX)
            ):
                queue.append(item.model_dump(mode="json"))
            else:
                self._issue(
                    "warning", "unresolved_news", f"news of {fact!r} for {observer!r} names no fact or observer",
                )
        return queue


__all__ = ["TickDeltaBuild", "TickIssue", "WorldTickDeltaBuilder", "tick_event_fact_ref"]
