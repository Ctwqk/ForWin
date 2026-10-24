"""Deterministic translation of a validated chapter extraction into one chapter GraphDelta."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from sqlalchemy.orm import Session

from forwin.book_state.book_frame_repository import BookLexiconRepository, StoryCalendarRepository
from forwin.book_state.knowledge import FACT_REF_PREFIX, KnowledgeEvent, is_concealed, knowledge_event_to_patch
from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.path_patch import apply_path_patch
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.query import BookStateQuery
from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
from forwin.book_state.schema import ACTOR_NODE_TYPES, WORLD_NODE_FIELDS
from forwin.book_state.story_clock import (
    StoryCalendar, StoryClockError, duration_to_minutes, format_story_time,
)
from forwin.naming.types import EntityAdmissionDecision, EntityAdmissionPlan
from forwin.protocol.book_state import (
    WORLD_EDGE_TYPES_BY_FAMILY,
    CognitionPatch,
    Directionality,
    EdgePatch,
    FactPatch,
    GraphDelta,
    GraphDeltaType,
    MapEdge,
    MapNode,
    MapNodeType,
    MapPatch,
    NarrativePatch,
    NodePatch,
    ObserverType,
    WorldEdge,
    WorldNode,
)

from .chapter_delta_extractor import elapsed_minutes
from .chapter_delta_schema import (
    NEW_REF_PREFIX,
    ChapterDeltaExtraction,
    Duration,
    EntityChange,
    EventItem,
    ExtractionIssue,
    IntentInteraction,
    ItemTransfer,
    KnowledgeEventItem,
    Movement,
    NewEntity,
    NewFact,
    NewSite,
    RelationChange,
    ThreadBeat,
)


SOURCE_TYPE = "chapter_extraction"
READER_REF = "reader"
_THREAD_REF_PREFIX = "plot_thread:"
# MapGraph adds a runtime copy of every bidirectional edge under this suffix.
_REVERSE_EDGE_SUFFIX = "__reverse"
_NEW_SITE_EDGE_HOURS = 0.25
_NEW_ENTITY_NODE_TYPES = frozenset({"faction", "organization", "group", "item", "location"})
_OBSERVER_TYPES = {"character": "character", "faction": "faction", "organization": "group", "group": "group"}
_MAP_NODE_TYPES = frozenset(node_type.value for node_type in MapNodeType)
# Relations that read the same from either endpoint; every other edge type is directed.
_SYMMETRIC_EDGE_TYPES = frozenset(
    {"family_of", "kin_of", "ally_of", "enemy_of", "rival_of", "romantic_with", "allied_with"}
)
_EDGE_SET_FIELDS = frozenset({"weight", "confidence", "status", "visibility", "truth_relation", "visibility_default"})
_EDGE_SET_PREFIXES = ("state.", "metadata.")
_INTENT_STATUS_BY_EFFECT = {"disrupted": "disrupted", "fulfilled": "resolved"}
_TOPIC_SUBJECT_TYPES = frozenset({*ACTOR_NODE_TYPES, "family", "institution", "item", "location", "region"})


@dataclass(frozen=True)
class TranslationResult:
    delta: GraphDelta | None
    issues: tuple[ExtractionIssue, ...]
    # Display name of every node, site and fact ref the delta and its review can name.
    names: Mapping[str, str] = field(default_factory=dict)
    # Writer-safe subject names, separate from the propositions retained for audit.
    fact_topics: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


class ChapterDeltaTranslator:
    """Translate a validated ChapterDeltaExtraction into the chapter's single GraphDelta."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def translate(
        self,
        *,
        project_id: str,
        chapter_number: int,
        extraction: ChapterDeltaExtraction,
        admission_plan: EntityAdmissionPlan,
        chapter_summary: str,
        review_verdict_id: str = "",
    ) -> TranslationResult:
        as_of_chapter = max(int(chapter_number) - 1, 0)
        calendar = StoryCalendarRepository(self.session).get(project_id)
        start = BookStateQuery(self.session).current_story_minute(project_id, as_of_chapter=as_of_chapter)
        if start is None:
            start = calendar.opening_minute if calendar is not None else 0
        builder = _DeltaBuilder(
            project_id=project_id,
            chapter_number=int(chapter_number),
            runtime=BookStateProjection(self.session).load_runtime_as_of(project_id, as_of_chapter=as_of_chapter),
            calendar=calendar,
            lexicon=BookLexiconRepository(self.session).get(project_id),
            start=start,
            admission_plan=admission_plan,
        )
        return builder.build(extraction, chapter_summary=chapter_summary, review_verdict_id=review_verdict_id)


class _DeltaBuilder:
    """One translation: patches read the as-of N−1 runtime, so every old_value is the pre-chapter value."""

    def __init__(
        self,
        *,
        project_id: str,
        chapter_number: int,
        runtime: BookStateRuntime,
        calendar: StoryCalendar | None,
        lexicon: BookLexicon,
        start: int,
        admission_plan: EntityAdmissionPlan,
    ) -> None:
        self.project_id = project_id
        self.chapter = chapter_number
        self.runtime = runtime
        self.world = runtime.world
        self.map = runtime.map
        self.calendar = calendar
        self.lexicon = lexicon
        self.start = start
        self.admission_plan = admission_plan
        self.decisions = _decisions_by_name(admission_plan)
        self.declared: dict[str, NewEntity] = {}
        self.issues: list[ExtractionIssue] = []
        self.node_patches: list[NodePatch] = []
        self.created_nodes: dict[str, NodePatch] = {}
        self.edge_patches: list[EdgePatch] = []
        self.fact_patches: list[FactPatch] = []
        self.created_facts: set[str] = set()
        self.map_patches: list[MapPatch] = []
        self.new_sites: dict[str, MapNode] = {}
        self.cognition_patches: list[CognitionPatch] = []
        self.discovered: set[tuple[str, str, str]] = set()
        self.narrative_patches: list[NarrativePatch] = []
        self.created_threads: dict[str, NarrativePatch] = {}
        self.resolved_threads: set[str] = set()
        self.locations: dict[str, str] = {}
        self.holders: dict[str, str] = {}
        self.movers: set[str] = set()
        self.movements: list[dict[str, Any]] = []
        self.knowledge_tags: list[dict[str, Any]] = []
        self.flashback_refs: list[dict[str, Any]] = []
        self.item_transfers: list[dict[str, Any]] = []
        self.intent_interactions: list[dict[str, Any]] = []

    def build(
        self, extraction: ChapterDeltaExtraction, *, chapter_summary: str, review_verdict_id: str,
    ) -> TranslationResult:
        elapsed = self._elapsed(extraction)
        end = self.start + (elapsed or 0)
        for entity in [
            *(change.new_entity for change in extraction.entity_changes),
            *(transfer.new_item for transfer in extraction.item_transfers),
        ]:
            if entity is not None:
                self.declared.setdefault(entity.name.strip(), entity)
        self.movers = {movement.entity_ref.strip() for movement in extraction.movements}
        self._apply_admission_plan()
        for index, site in enumerate(extraction.new_sites):
            self._new_site(site, f"new_sites[{index}]")
        for index, change in enumerate(extraction.entity_changes):
            self._entity_change(change, f"entity_changes[{index}]")
        for index, relation in enumerate(extraction.relation_changes):
            self._relation_change(relation, f"relation_changes[{index}]")
        for index, item in enumerate(extraction.knowledge_events):
            self._knowledge_event(item, f"knowledge_events[{index}]")
        for index, movement in enumerate(extraction.movements):
            self._movement(movement, f"movements[{index}]")
        for index, transfer in enumerate(extraction.item_transfers):
            self._item_transfer(transfer, f"item_transfers[{index}]")
        for index, event in enumerate(extraction.events):
            self._event(event, index)
        for index, beat in enumerate(extraction.thread_beats):
            self._thread_beat(beat, index)
        for index, interaction in enumerate(extraction.intent_interactions):
            self._intent_interaction(interaction, f"intent_interactions[{index}]")
        issues = tuple(self.issues)
        if any(issue.severity == "error" for issue in issues):
            return TranslationResult(None, issues, self._display_names(), self._fact_topics())
        delta = GraphDelta(
            id=f"chapter-delta:{self.project_id}:{self.chapter}:{extraction.body_sha256[:16]}",
            project_id=self.project_id,
            chapter_number=self.chapter,
            story_time=format_story_time(self.calendar, end) if self.calendar is not None else "",
            story_time_start=self.start,
            story_time_end=end,
            delta_type=GraphDeltaType.WORLD_STATE,
            operation="apply_chapter_extraction",
            target_type="chapter",
            target_id=f"chapter:{self.chapter}",
            source_type=SOURCE_TYPE,
            source_id=f"chapter:{self.chapter}:{extraction.body_sha256}",
            world_line_id="main",
            summary=chapter_summary.strip(),
            node_patches=self.node_patches,
            edge_patches=self.edge_patches,
            fact_patches=self.fact_patches,
            map_patches=self.map_patches,
            cognition_patches=self.cognition_patches,
            narrative_patches=self.narrative_patches,
            evidence_refs=[f"chapter:{self.chapter}"],
            review_verdict_id=review_verdict_id,
            metadata={
                "extraction_path": SOURCE_TYPE,
                "movements": self.movements,
                "knowledge_tags": self.knowledge_tags,
                "flashback_refs": self.flashback_refs,
                "item_transfers": self.item_transfers,
                "plan_fulfillment": [item.model_dump(mode="json") for item in extraction.plan_fulfillment],
                "intent_interactions": self.intent_interactions,
                "time_elapsed_minutes": elapsed,
            },
        )
        return TranslationResult(delta, issues, self._display_names(), self._fact_topics())

    def _display_names(self) -> dict[str, str]:
        """Names of the world nodes, map nodes and facts before the chapter plus the ones it creates."""
        names = {node_id: node.name for node_id, node in self.world.nodes_by_id.items()}
        names.update((node_id, node.name) for node_id, node in self.map.nodes_by_id.items())
        names.update((node.id, node.name) for node in self.new_sites.values())
        names.update(
            (node_id, str(patch.new_value.get("name") or ""))
            for node_id, patch in self.created_nodes.items()
            if isinstance(patch.new_value, dict)
        )
        names.update(
            (f"{FACT_REF_PREFIX}{fact_id}", fact.proposition) for fact_id, fact in self.world.facts_by_id.items()
        )
        names.update(
            (f"{FACT_REF_PREFIX}{patch.fact_id}", str(patch.proposition or ""))
            for patch in self.fact_patches
            if str(patch.op) == "create"
        )
        return {ref: text for ref, name in names.items() if (text := str(name or "").strip())}

    def _fact_topics(self) -> dict[str, tuple[str, ...]]:
        """Name visible subjects only, including nodes and facts created by this chapter."""
        world = ObjectiveWorldGraph(
            nodes=list(self.world.nodes_by_id.values()), states_by_node_id=self.world.states_by_node_id,
        )
        for patch in self.created_nodes.values():
            world.apply_node_patch(patch)
        places = {**self.map.nodes_by_id, **{node.id: node for node in self.new_sites.values()}}
        subjects_by_fact = {
            f"{FACT_REF_PREFIX}{fact.id}": fact.related_node_refs for fact in self.world.facts_by_id.values()
        }
        subjects_by_fact.update(
            (f"{FACT_REF_PREFIX}{patch.fact_id}", patch.related_refs)
            for patch in self.fact_patches if str(patch.op) == "create"
        )
        topics: dict[str, tuple[str, ...]] = {}
        for fact_ref, refs in subjects_by_fact.items():
            names: list[str] = []
            for ref in refs:
                node = world.get_node(ref)
                place = places.get(ref)
                if node is not None:
                    if str(node.node_type) in _TOPIC_SUBJECT_TYPES and not is_concealed(world, node):
                        names.append(node.name.strip())
                elif place is not None:
                    names.append(place.name.strip())
            topics[fact_ref] = tuple(dict.fromkeys(name for name in names if name))
        return topics

    # issues and time

    def _issue(self, severity: str, code: str, path: str, message: str) -> None:
        self.issues.append(ExtractionIssue(severity=severity, code=code, path=path, message=message))

    def _unresolved(self, path: str, message: str) -> None:
        self._issue("error", "unresolved_ref", path, message)

    def _elapsed(self, extraction: ChapterDeltaExtraction) -> int | None:
        try:
            return elapsed_minutes(extraction, self.calendar, self.lexicon)
        except StoryClockError as exc:
            self._issue("warning", "elapsed_unknown", "time.elapsed", str(exc))
            return None

    def _offset_minute(self, offset: Duration | None, path: str) -> int | None:
        if offset is None:
            return None
        calendar = self.calendar if self.calendar is not None else StoryCalendar("", 0, ())
        try:
            return self.start + duration_to_minutes(
                calendar, self.lexicon, amount=offset.amount, unit_name=offset.unit,
            )
        except StoryClockError as exc:
            self._issue("warning", "offset_unknown", path, str(exc))
            return None

    # references

    def _entity(self, ref: str, path: str) -> str | None:
        """World node id of an entity ref: "" for a background mention, None when unresolved."""
        text = ref.strip()
        if text.startswith(NEW_REF_PREFIX):
            return self._new_entity(text.removeprefix(NEW_REF_PREFIX).strip(), path)
        if text in self.world.nodes_by_id or text in self.created_nodes:
            return text
        self._unresolved(path, f"unknown entity ref {text!r}")
        return None

    def _new_entity(self, name: str, path: str) -> str | None:
        decision = self.decisions.get(name)
        if decision is not None:
            if decision.action == "background_generic":
                return ""
            if decision.action in {"register_character", "register_alias"} and self._node_exists(decision.entity_id):
                return decision.entity_id
            self._unresolved(path, f"entity admission did not admit {name!r} ({decision.action})")
            return None
        entity = self.declared.get(name)
        if entity is None or entity.kind not in _NEW_ENTITY_NODE_TYPES:
            self._unresolved(path, f"new entity {name!r} has no admission decision or declared non-character kind")
            return None
        existing = self._named_node(entity.kind, name)
        if existing is not None:
            return existing
        node_id = _stable_id(self.project_id, entity.kind, name)
        if not self._node_exists(node_id):
            self._create_node(
                WorldNode(
                    id=node_id,
                    project_id=self.project_id,
                    node_type=entity.kind,
                    name=name,
                    aliases=[alias for alias in entity.aliases if alias.strip()],
                    created_at_chapter=self.chapter,
                    state={"status": "active"},
                    metadata={"source": SOURCE_TYPE, "mention_name": name},
                ),
                reason="chapter extraction new entity",
            )
        return node_id

    def _named_node(self, kind: str, name: str) -> str | None:
        """A node of ``kind`` named ``name`` or aliased so, ignoring case and whitespace."""
        wanted = _normalized_name(name)
        if not wanted:
            return None
        candidates = [
            *(
                (node_id, node.name, node.aliases)
                for node_id, node in sorted(self.world.nodes_by_id.items())
                if str(node.node_type) == kind
            ),
            *(
                (node_id, patch.new_value.get("name", ""), patch.new_value.get("aliases", []))
                for node_id, patch in self.created_nodes.items()
                if str(patch.node_type) == kind
            ),
        ]
        return next(
            (
                node_id for node_id, node_name, aliases in candidates
                if wanted in {_normalized_name(label) for label in (node_name, *aliases)}
            ),
            None,
        )

    def _location(self, ref: str, path: str) -> str | None:
        text = ref.strip()
        if text.startswith(NEW_REF_PREFIX):
            site = self.new_sites.get(text.removeprefix(NEW_REF_PREFIX).strip())
            if site is not None:
                return site.id
        elif text in self.map.nodes_by_id:
            return text
        self._unresolved(path, f"unknown location ref {text!r}")
        return None

    def _holder(self, ref: str, path: str) -> str | None:
        text = ref.strip()
        is_site = text in self.map.nodes_by_id or (
            text.startswith(NEW_REF_PREFIX) and text.removeprefix(NEW_REF_PREFIX).strip() in self.new_sites
        )
        return self._location(text, path) if is_site else self._entity(text, path)

    def _observer(self, ref: str, path: str) -> tuple[str, str] | None:
        if ref.strip() == READER_REF:
            return ObserverType.READER.value, READER_REF
        node_id = self._entity(ref, path)
        if not node_id:
            return None
        observer_type = _OBSERVER_TYPES.get(self._node_type(node_id))
        if observer_type is None:
            self._unresolved(path, f"{node_id!r} is not an observer")
            return None
        return observer_type, node_id

    def _fact_ref(self, ref: str, path: str) -> str | None:
        text = ref.strip()
        fact_id = text.removeprefix(FACT_REF_PREFIX)
        if text.startswith(FACT_REF_PREFIX) and (fact_id in self.world.facts_by_id or fact_id in self.created_facts):
            return text
        self._unresolved(path, f"unknown fact ref {text!r}")
        return None

    # nodes

    def _node_exists(self, node_id: str) -> bool:
        return bool(node_id) and (node_id in self.world.nodes_by_id or node_id in self.created_nodes)

    def _node_type(self, node_id: str) -> str:
        node = self.world.nodes_by_id.get(node_id)
        return str(node.node_type) if node is not None else str(self.created_nodes[node_id].node_type)

    def _create_node(self, node: WorldNode, *, reason: str) -> None:
        patch = NodePatch(
            node_id=node.id,
            node_type=str(node.node_type),
            op="create",
            new_value=node.model_dump(mode="json", exclude={"id", "node_type"}),
            reason=reason,
        )
        self.node_patches.append(patch)
        self.created_nodes[node.id] = patch

    def _field_path(self, node_id: str, field: str, path: str) -> str | None:
        node_type = self._node_type(node_id)
        text = field.strip()
        fields = WORLD_NODE_FIELDS.get(node_type, {})
        for section in ("state", "profile"):
            if text.split(".", 1)[0] in fields.get(section, set()):
                return f"{section}.{text}"
        self._issue("error", "unknown_field", path, f"{text!r} is not a {node_type} state or profile field")
        return None

    def _set_node_field(self, node_id: str, field_path: str, value: Any, *, path: str, reason: str) -> None:
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
            self._issue("error", "missing_old_value", path, f"{node_id}:{field_path} has no current value or parent")
            return
        op, patch_path, old_value, new_value = leaf
        self.node_patches.append(NodePatch(
            node_id=node_id,
            node_type=self._node_type(node_id),
            op=op,
            field_path=patch_path,
            old_value=old_value,
            new_value=new_value,
            reason=reason,
        ))

    def _apply_admission_plan(self) -> None:
        for decision in self.admission_plan.decisions:
            if decision.action == "register_character" and decision.entity_id and not self._node_exists(decision.entity_id):
                self._create_node(_admitted_character(self.project_id, self.chapter, decision), reason="Canon entity admission")
            elif decision.action == "register_alias":
                target = self.world.nodes_by_id.get(decision.entity_id)
                if target is None:
                    continue
                appended = [
                    patch.new_value for patch in self.node_patches
                    if patch.node_id == target.id and patch.field_path == "aliases"
                ]
                for alias in decision.aliases:
                    if alias.strip() and alias not in target.aliases and alias not in appended:
                        appended.append(alias)
                        self.node_patches.append(NodePatch(
                            node_id=target.id,
                            node_type=str(target.node_type),
                            op="append",
                            field_path="aliases",
                            old_value=list(target.aliases),
                            new_value=alias,
                            reason="Canon entity alias admission",
                        ))

    # sections

    def _new_site(self, site: NewSite, path: str) -> None:
        name = site.name.strip()
        node_id = _stable_id(self.project_id, "map_node", name)
        existing = self.map.nodes_by_id.get(node_id) or self.new_sites.get(name)
        if existing is not None:
            # Named again (in this chapter or an earlier one): reuse the site.
            self.new_sites[name] = existing
            return
        near_id = ""
        if site.near_ref.strip():
            near_id = self._location(site.near_ref, f"{path}.near_ref")
            if near_id is None:
                return
            anchor = self.map.nodes_by_id.get(near_id) or next(
                node for node in self.new_sites.values() if node.id == near_id
            )
            region_id = anchor.region_id
        elif site.region_ref.strip():
            region_id = site.region_ref.strip()
            anchor = next(
                (node for _node_id, node in sorted(self.map.nodes_by_id.items()) if node.region_id == region_id),
                None,
            )
            if anchor is None:
                self._unresolved(f"{path}.region_ref", f"unknown region ref {region_id!r}")
                return
            # Join the site to the region's anchor node so moves into it have a route.
            near_id = anchor.id
        else:
            self._issue("error", "new_site_unanchored", path, f"new site {name!r} has neither near_ref nor region_ref")
            return
        node = MapNode(
            id=node_id,
            project_id=self.project_id,
            subworld_id=anchor.subworld_id,
            region_id=region_id,
            node_type=site.kind if site.kind in _MAP_NODE_TYPES else "site",
            name=name,
            metadata={"source": SOURCE_TYPE, "created_at_chapter": self.chapter},
        )
        self.map_patches.append(MapPatch(
            target_type="map_node", target_id=node_id, op="create",
            new_value=node.model_dump(mode="json"), reason=site.evidence,
        ))
        if near_id:
            edge = MapEdge(
                id=_stable_id(self.project_id, "map_edge", f"{node_id}/{near_id}"),
                project_id=self.project_id,
                subworld_id=anchor.subworld_id,
                from_node_id=node_id,
                to_node_id=near_id,
                edge_type="path",
                bidirectional=True,
                travel_time=_NEW_SITE_EDGE_HOURS,
                metadata={"source": SOURCE_TYPE, "created_at_chapter": self.chapter},
            )
            self.map_patches.append(MapPatch(
                target_type="map_edge", target_id=edge.id, op="create",
                new_value=edge.model_dump(mode="json"), reason=site.evidence,
            ))
        self._create_node(
            WorldNode(
                id=_stable_id(self.project_id, "site_state", node_id),
                project_id=self.project_id,
                node_type="site_state",
                name=name,
                created_at_chapter=self.chapter,
                profile={"map_node_id": node_id, "site_type": site.kind},
                state={"status": "active"},
                metadata={"source": SOURCE_TYPE},
            ),
            reason=site.evidence,
        )
        self.new_sites[name] = node

    def _entity_change(self, change: EntityChange, path: str) -> None:
        ref = change.ref.strip() or (
            f"{NEW_REF_PREFIX}{change.new_entity.name.strip()}" if change.new_entity is not None else ""
        )
        if not ref:
            self._unresolved(f"{path}.ref", "entity change names no entity")
            return
        node_id = self._entity(ref, f"{path}.ref")
        if not node_id:
            return
        if change.flashback:
            # A flashback recalls the past; it never changes current state.
            self.flashback_refs.append({"path": path, "refs": [node_id]})
            return
        field_path = self._field_path(node_id, change.field, f"{path}.field")
        if field_path is None:
            return
        # Location and holder changes are moves and transfers, which the review gate checks.
        node_type = self._node_type(node_id)
        if field_path == "state.location_id" and node_type == "character":
            if {node_id, ref} & self.movers:
                # The extraction's own movement for this entity carries the move.
                return
            to_id = self._location(str(change.new_value or ""), f"{path}.new_value")
            if to_id is not None:
                self._record_movement(node_id, "", to_id, path=path, evidence=change.evidence)
        elif field_path == "state.holder_id" and node_type == "item":
            to_id = self._holder(str(change.new_value or ""), f"{path}.new_value")
            if to_id is not None:
                self._record_transfer(node_id, "", to_id, path=path, evidence=change.evidence)
        else:
            self._set_node_field(node_id, field_path, change.new_value, path=f"{path}.field", reason=change.evidence)

    def _relation_change(self, change: RelationChange, path: str) -> None:
        families = [family for family, edge_types in WORLD_EDGE_TYPES_BY_FAMILY.items() if change.edge_type in edge_types]
        if len(families) != 1:
            self._issue("error", "edge_type_invalid", f"{path}.edge_type", f"{change.edge_type!r} has no unique edge family")
            return
        source_id = self._relation_endpoint(change.source_ref, f"{path}.source_ref")
        target_id = self._relation_endpoint(change.target_ref, f"{path}.target_ref")
        if not source_id or not target_id:
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
        if change.op == "create":
            # A create earlier in this chapter counts as existing too.
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
                self._issue("warning", "relation_exists", path, f"relation {existing_id} already exists")
            elif not symmetric and not forward and any(edge.is_active for edge in backward):
                # An ended reverse edge is a past role; only a live one blocks the flip.
                self._direction_mismatch(change, path)
            else:
                self._create_relation(change, matching, source_id, target_id, families[0])
            return
        if active is None:
            if not symmetric and any(edge.is_active for edge in backward):
                self._direction_mismatch(change, path)
            else:
                self._issue("warning", "relation_missing", path, f"no active {change.edge_type} relation to {change.op}")
            return
        endpoints = {
            "source_id": active.source_id, "target_id": active.target_id,
            "edge_type": active.edge_type, "edge_family": str(active.edge_family),
        }
        if change.op == "end":
            self.edge_patches.append(EdgePatch(
                edge_id=active.id, op="deactivate", **endpoints,
                field_path="is_active", old_value=True, reason=change.evidence,
            ))
            return
        field_path = _edge_field_path(change.field)
        if field_path is None:
            self._issue("error", "unknown_field", f"{path}.field", f"{change.field!r} is not a settable relation field")
            return
        leaf = _leaf_patch(active.model_dump(mode="json"), field_path, change.new_value)
        if leaf is None:
            self._issue("error", "missing_old_value", f"{path}.field", f"{active.id}:{field_path} has no current value or parent")
            return
        op, patch_path, old_value, new_value = leaf
        self.edge_patches.append(EdgePatch(
            edge_id=active.id, op=op, **endpoints, field_path=patch_path,
            old_value=old_value, new_value=new_value, reason=change.evidence,
        ))

    def _create_relation(
        self, change: RelationChange, matching: list[WorldEdge], source_id: str, target_id: str, edge_family: str,
    ) -> None:
        ended = [edge for edge in matching if edge.ended_at_chapter is not None]
        # A create on an ended relation reactivates that same edge.
        symmetric = change.edge_type in _SYMMETRIC_EDGE_TYPES
        # A symmetric relation has one id whichever endpoint the prose names first.
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
                "evidence_refs": [f"chapter:{self.chapter}"],
                "metadata": {"appellation": change.appellation, "source": SOURCE_TYPE},
            },
            reason=change.evidence,
        ))

    def _direction_mismatch(self, change: RelationChange, path: str) -> None:
        self._issue(
            "warning", "relation_direction_mismatch", path,
            f"only the reverse {change.edge_type} relation exists; directed relations are not flipped",
        )

    def _relation_endpoint(self, ref: str, path: str) -> str | None:
        """An entity ref, or a map node id standing for the site_state node of that site."""
        text = ref.strip()
        if text not in self.map.nodes_by_id or text in self.world.nodes_by_id:
            return self._entity(text, path)
        sites = [
            *(node_id for node_id, node in sorted(self.world.nodes_by_id.items())
              if str(node.node_type) == "site_state" and node.profile.get("map_node_id") == text),
            *(node_id for node_id, patch in self.created_nodes.items()
              if str(patch.node_type) == "site_state" and patch.new_value["profile"].get("map_node_id") == text),
        ]
        if not sites:
            self._unresolved(path, f"location {text!r} has no site_state node")
            return None
        return sites[0]

    def _knowledge_event(self, item: KnowledgeEventItem, path: str) -> None:
        observer = self._observer(item.observer_ref, f"{path}.observer_ref")
        if item.fact_ref.strip():
            fact_ref = self._fact_ref(item.fact_ref, f"{path}.fact_ref")
        elif item.new_fact is not None:
            fact_ref = self._create_fact(item.new_fact, reason=item.evidence)
        else:
            fact_ref = None
            self._unresolved(f"{path}.fact_ref", "knowledge event names no fact")
        source_id = self._entity(item.source_ref, f"{path}.source_ref") if item.source_ref.strip() else ""
        touches = (
            self._fact_ref(item.touches_secret_ref, f"{path}.touches_secret_ref")
            if item.touches_secret_ref.strip() else ""
        )
        contradicts = (
            self._fact_ref(item.contradicts_ref, f"{path}.contradicts_ref") if item.contradicts_ref.strip() else ""
        )
        if observer is None or fact_ref is None or source_id is None or touches is None or contradicts is None:
            return
        observer_type, observer_id = observer
        view = self.runtime.cognition_by_observer.get(observer)
        patch = knowledge_event_to_patch(KnowledgeEvent(
            observer_type=observer_type, observer_id=observer_id, fact_ref=fact_ref,
            state=item.state, channel=item.channel, source_ref=source_id, at=self.start,
        ))
        self.cognition_patches.append(patch.model_copy(update={
            "old_value": {fact_ref: view.knowledge_by_ref.get(fact_ref) if view is not None else None},
            "reason": item.evidence,
        }))
        self.knowledge_tags.append({
            "observer_type": observer_type,
            "observer_id": observer_id,
            "fact_ref": fact_ref,
            "state": item.state,
            "channel": item.channel,
            "source_id": source_id,
            "touches_secret_ref": touches,
            "contradicts_ref": contradicts,
            "flashback": item.flashback,
            "evidence": item.evidence,
        })
        if item.flashback:
            self.flashback_refs.append({"path": path, "refs": [observer_id, fact_ref]})

    def _create_fact(self, fact: NewFact, *, reason: str) -> str:
        proposition = fact.proposition.strip()
        fact_id = _stable_id(self.project_id, "fact", proposition)
        if fact_id not in self.world.facts_by_id and fact_id not in self.created_facts:
            truth_value = "true" if fact.truth_value else "false"
            self.fact_patches.append(FactPatch(
                fact_id=fact_id,
                op="create",
                proposition=proposition,
                truth_value=truth_value,
                sensitivity_level=fact.sensitivity,
                new_value={
                    "project_id": self.project_id,
                    "proposition": proposition,
                    "truth_value": truth_value,
                    "sensitivity_level": fact.sensitivity,
                    "source_refs": [f"chapter:{self.chapter}"],
                    "created_at_chapter": self.chapter,
                    "metadata": {"source": SOURCE_TYPE},
                },
                reason=reason,
            ))
            self.created_facts.add(fact_id)
        return f"{FACT_REF_PREFIX}{fact_id}"

    def _movement(self, movement: Movement, path: str) -> None:
        mover = self._entity(movement.entity_ref, f"{path}.entity_ref")
        to_id = self._location(movement.to_ref, f"{path}.to_ref")
        from_id = self._location(movement.from_ref, f"{path}.from_ref") if movement.from_ref.strip() else ""
        if not mover or to_id is None or from_id is None:
            return
        if "location_id" not in WORLD_NODE_FIELDS.get(self._node_type(mover), {}).get("state", set()):
            self._issue("error", "unknown_field", f"{path}.entity_ref", f"{mover!r} has no state.location_id")
            return
        self._record_movement(
            mover, from_id, to_id, path=path, evidence=movement.evidence,
            depart_minute=self._offset_minute(movement.depart_offset, f"{path}.depart_offset"),
            arrive_minute=self._offset_minute(movement.arrive_offset, f"{path}.arrive_offset"),
            travel_mode_id=movement.travel_mode_id, discovers_route=movement.discovers_route,
        )

    def _record_movement(
        self,
        mover: str,
        from_id: str,
        to_id: str,
        *,
        path: str,
        evidence: str,
        depart_minute: int | None = None,
        arrive_minute: int | None = None,
        travel_mode_id: str = "",
        discovers_route: bool = False,
    ) -> None:
        from_id = from_id or self._current_location(mover)
        self._set_node_field(mover, "state.location_id", to_id, path=path, reason=evidence)
        if discovers_route:
            self._discover_route(mover, from_id, to_id)
        self.locations[mover] = to_id
        self.movements.append({
            "entity_id": mover,
            "from_id": from_id,
            "to_id": to_id,
            "depart_minute": depart_minute,
            "arrive_minute": arrive_minute,
            "travel_mode_id": travel_mode_id,
            "evidence": evidence,
        })

    def _current_location(self, node_id: str) -> str:
        return self._current_state(node_id, "location_id", self.locations)

    def _current_holder(self, node_id: str) -> str:
        return self._current_state(node_id, "holder_id", self.holders)

    def _current_state(self, node_id: str, field: str, moved: dict[str, str]) -> str:
        """The field as earlier entries of this chapter left it, else its pre-chapter value."""
        if node_id in moved:
            return moved[node_id]
        created = self.created_nodes.get(node_id)
        if created is not None:
            return str(created.new_value.get("state", {}).get(field) or "")
        return str(_current_node_value(self.runtime, node_id, f"state.{field}") or "")

    def _discover_route(self, mover: str, from_id: str, to_id: str) -> None:
        """Reveal the edges of the route the prose says the mover found that it did not know yet."""
        observer = (_OBSERVER_TYPES.get(self._node_type(mover), ""), mover)
        view = self.runtime.cognition_by_observer.get(observer)
        if view is None or from_id not in self.map.nodes_by_id or to_id not in self.map.nodes_by_id:
            return
        for edge_id in self.map.shortest_path(from_id, to_id).path_edge_ids:
            edge = self.map.edges_by_id[edge_id]
            # The same availability rule the observer's path search applies.
            if self.map._edge_available(edge, cognition=view, allow_hidden=False, allow_blocked=False):
                continue
            base_id = str(edge.metadata.get("runtime_reverse_of") or edge.id)
            base = self.map.edges_by_id.get(base_id, edge)
            discovered_ids = (base_id, f"{base_id}{_REVERSE_EDGE_SUFFIX}") if base.bidirectional else (base_id,)
            for discovered_id in discovered_ids:
                ref = f"map_edge:{discovered_id}"
                if ref in view.visible_refs or (*observer, ref) in self.discovered:
                    continue
                self.discovered.add((*observer, ref))
                self.cognition_patches.append(CognitionPatch(
                    observer_type=observer[0],
                    observer_id=mover,
                    op="append",
                    field_path="visible_refs",
                    old_value=sorted(view.visible_refs),
                    new_value=ref,
                    reason="route discovered by movement",
                    evidence_refs=[f"chapter:{self.chapter}"],
                ))

    def _item_transfer(self, transfer: ItemTransfer, path: str) -> None:
        ref = transfer.item_ref.strip() or (
            f"{NEW_REF_PREFIX}{transfer.new_item.name.strip()}" if transfer.new_item is not None else ""
        )
        if ref:
            item_id = self._entity(ref, f"{path}.item_ref")
        else:
            item_id = None
            self._unresolved(f"{path}.item_ref", "item transfer names no item")
        to_id = self._holder(transfer.to_ref, f"{path}.to_ref")
        from_id = self._holder(transfer.from_ref, f"{path}.from_ref") if transfer.from_ref.strip() else ""
        if not item_id or to_id is None or from_id is None:
            return
        if "holder_id" not in WORLD_NODE_FIELDS.get(self._node_type(item_id), {}).get("state", set()):
            self._issue("error", "unknown_field", f"{path}.item_ref", f"{item_id!r} has no state.holder_id")
            return
        self._record_transfer(item_id, from_id, to_id, path=path, evidence=transfer.evidence)

    def _record_transfer(self, item_id: str, from_id: str, to_id: str, *, path: str, evidence: str) -> None:
        if not to_id:
            self._issue("warning", "item_transfer_dropped", path, f"{item_id!r} went to a background mention")
            return
        from_id = from_id or self._current_holder(item_id)
        self._set_node_field(item_id, "state.holder_id", to_id, path=path, reason=evidence)
        self.holders[item_id] = to_id
        self.item_transfers.append({"item_id": item_id, "from_id": from_id, "to_id": to_id, "evidence": evidence})

    def _event(self, event: EventItem, index: int) -> None:
        path = f"events[{index}]"
        participants: list[str] = []
        resolved = True
        for ref_index, ref in enumerate(event.participant_refs):
            node_id = self._entity(ref, f"{path}.participant_refs[{ref_index}]")
            if node_id is None:
                resolved = False
            elif node_id and node_id not in participants:
                participants.append(node_id)
        location_id = self._location(event.location_ref, f"{path}.location_ref") if event.location_ref.strip() else ""
        if not resolved or location_id is None:
            return
        summary = event.summary.strip()
        event_id = _stable_id(self.project_id, "event", f"{self.chapter}/{index}/{summary}")
        fact_id = _stable_id(self.project_id, "fact", event_id)
        fact_ref = f"{FACT_REF_PREFIX}{fact_id}"
        sensitivity = "secret" if event.visibility == "secret" else "public"
        if event.flashback:
            # A recalled event leaves its fact only; no event happens in the current timeline.
            self.flashback_refs.append({"path": path, "refs": [fact_ref, *participants]})
            related = list(participants)
        else:
            self._create_node(
                WorldNode(
                    id=event_id,
                    project_id=self.project_id,
                    node_type="event",
                    name=summary[:80],
                    summary=summary,
                    description=summary,
                    created_at_chapter=self.chapter,
                    profile={
                        "event_type": event.significance,
                        "planned_or_actual": "actual",
                        "expected_chapter": self.chapter,
                        "involved_node_refs": [f"node:{node_id}" for node_id in participants],
                        "location_id": location_id,
                        "public_summary": summary if sensitivity == "public" else "",
                    },
                    state={
                        "status": "occurred",
                        "actual_chapter": self.chapter,
                        "participant_ids": participants,
                        "result_refs": [fact_ref],
                        "visibility_level": event.visibility,
                        "state_summary": summary,
                    },
                    metadata={"source": SOURCE_TYPE},
                ),
                reason=summary,
            )
            related = [event_id, *participants]
        self.fact_patches.append(FactPatch(
            fact_id=fact_id,
            op="create",
            proposition=summary,
            truth_value="true",
            related_refs=related,
            sensitivity_level=sensitivity,
            new_value={
                "project_id": self.project_id,
                "proposition": summary,
                "fact_type": "event",
                "truth_value": "true",
                "confidence": 1.0,
                "related_node_refs": related,
                "source_refs": [f"chapter:{self.chapter}"],
                "created_at_chapter": self.chapter,
                "sensitivity_level": sensitivity,
                "state": {
                    "status": "active",
                    "last_updated_chapter": self.chapter,
                    "canonicality_level": "canon",
                    "state_summary": summary,
                },
                "metadata": {"source": SOURCE_TYPE, "flashback": event.flashback},
            },
            reason=summary,
        ))
        self.created_facts.add(fact_id)

    def _thread_beat(self, beat: ThreadBeat, index: int) -> None:
        path = f"thread_beats[{index}]"
        thread_id = self._thread(beat, path)
        if thread_id is None:
            return
        entry = {
            "chapter_number": self.chapter,
            "beat_type": beat.beat_type,
            "description": beat.evidence,
            "sequence": index,
        }
        resolution = beat.beat_type == "resolution"
        created = self.created_threads.get(thread_id)
        if created is not None:
            created.new_value["payload"]["beats"].append(entry)
            if resolution:
                created.new_value["status"] = "resolved"
            return
        node = self.runtime.narrative.nodes_by_id[thread_id]
        target_ref = f"{_THREAD_REF_PREFIX}{thread_id}"
        self.narrative_patches.append(NarrativePatch(
            target_ref=target_ref,
            op="append",
            field_path="payload.beats",
            old_value=_get_path(node.model_dump(mode="json"), "payload.beats"),
            new_value=entry,
            reason=beat.evidence,
            evidence_refs=[f"chapter:{self.chapter}"],
        ))
        if resolution and node.status != "resolved" and thread_id not in self.resolved_threads:
            self.resolved_threads.add(thread_id)
            self.narrative_patches.append(NarrativePatch(
                target_ref=target_ref,
                op="set",
                field_path="status",
                old_value=node.status,
                new_value="resolved",
                reason=beat.evidence,
                evidence_refs=[f"chapter:{self.chapter}"],
            ))

    def _thread(self, beat: ThreadBeat, path: str) -> str | None:
        threads = {
            node_id: node for node_id, node in sorted(self.runtime.narrative.nodes_by_id.items())
            if str(node.node_type) == "plot_thread"
        }
        if beat.thread_ref.strip():
            thread_id = beat.thread_ref.strip().removeprefix(_THREAD_REF_PREFIX)
            if thread_id in threads or thread_id in self.created_threads:
                return thread_id
            self._unresolved(f"{path}.thread_ref", f"unknown plot thread {thread_id!r}")
            return None
        title = beat.new_thread_title.strip()
        if not title:
            self._issue("warning", "untargeted_thread_beat", path, "thread beat names no thread")
            return None
        existing = next((node_id for node_id, node in threads.items() if node.title.strip() == title), None)
        if existing is not None:
            return existing
        thread_id = _stable_id(self.project_id, "plot_thread", title)
        if thread_id in threads:
            return thread_id
        if thread_id not in self.created_threads:
            patch = NarrativePatch(
                target_ref=f"{_THREAD_REF_PREFIX}{thread_id}",
                op="create",
                new_value={
                    "node_type": "plot_thread",
                    "title": title,
                    "status": "active",
                    "payload": {"description": beat.evidence, "priority": 5, "beats": []},
                    "metadata": {"created_at_chapter": self.chapter, "source": SOURCE_TYPE},
                },
                reason=beat.evidence,
                evidence_refs=[f"chapter:{self.chapter}"],
            )
            self.narrative_patches.append(patch)
            self.created_threads[thread_id] = patch
        return thread_id

    def _intent_interaction(self, interaction: IntentInteraction, path: str) -> None:
        intent_id = self._entity(interaction.intent_ref, f"{path}.intent_ref")
        if not intent_id:
            return
        if self._node_type(intent_id) != "intent":
            self._unresolved(f"{path}.intent_ref", f"{intent_id!r} is not an intent")
            return
        self.intent_interactions.append({
            "intent_id": intent_id, "effect": interaction.effect, "evidence": interaction.evidence,
        })
        status = _INTENT_STATUS_BY_EFFECT.get(interaction.effect)
        if status is not None:
            self._set_node_field(intent_id, "state.status", status, path=path, reason=interaction.evidence)


def _admitted_character(project_id: str, chapter_number: int, decision: EntityAdmissionDecision) -> WorldNode:
    return WorldNode(
        id=decision.entity_id,
        project_id=project_id,
        node_type="character",
        name=decision.canonical_name or decision.mention_name,
        aliases=list(decision.aliases),
        summary=decision.role_hint,
        description=decision.role_hint,
        importance=min(max(int(decision.importance), 1), 10),
        created_at_chapter=chapter_number,
        profile={"narrative_role": decision.role_hint},
        state={
            "status": "active",
            "alive_status": "alive",
            "restraint": {"state": "free", "by_ref": "", "since": None},
            "last_seen_chapter": chapter_number,
        },
        metadata={"source": "entity_admission_plan", "mention_name": decision.mention_name},
    )


def _decisions_by_name(plan: EntityAdmissionPlan) -> dict[str, EntityAdmissionDecision]:
    """Decisions by every name they cover; a decision's own mention name wins."""
    index: dict[str, EntityAdmissionDecision] = {}
    for decision in plan.decisions:
        index.setdefault(decision.mention_name.strip(), decision)
    for decision in plan.decisions:
        for name in (decision.canonical_name, *decision.aliases):
            index.setdefault(name.strip(), decision)
    index.pop("", None)
    return index


def _normalized_name(text: str) -> str:
    return "".join(str(text or "").split()).casefold()


def _stable_id(project_id: str, kind: str, name: str) -> str:
    """Hyphenated ``kind-<20 hex>`` id, the same scheme as the world seed's ``seed_ref_id``."""
    digest = sha256(f"{project_id}\0{kind}\0{name}".encode()).hexdigest()[:20]
    return f"{kind}-{digest}"


def _leaf_patch(payload: dict[str, Any], field_path: str, value: Any) -> tuple[str, str, Any, Any] | None:
    """(op, field_path, old_value, new_value) setting one leaf of ``payload``.

    A leaf without a value yet merges into its parent dict, whose current value is the
    old_value rewind restores; None when that parent does not exist either.
    """
    old_value = _get_path(payload, field_path)
    if old_value is not None:
        return "set", field_path, old_value, value
    parent, _, leaf = field_path.rpartition(".")
    current = _get_path(payload, parent) if parent else None
    if not isinstance(current, dict):
        return None
    return "merge", parent, dict(current), {leaf: value}


def _edge_field_path(field: str) -> str | None:
    text = field.strip()
    if text == "appellation":
        return "metadata.appellation"
    if text in _EDGE_SET_FIELDS or (text.startswith(_EDGE_SET_PREFIXES) and text.split(".", 1)[1]):
        return text
    return None


def _current_node_value(runtime: BookStateRuntime, node_id: str, field_path: str) -> Any:
    node = runtime.world.nodes_by_id.get(node_id)
    if node is None:
        return None
    if field_path.startswith("state."):
        return _get_path(runtime.world.get_state(node_id), field_path.removeprefix("state."))
    return _get_path(node.model_dump(mode="json"), field_path)


def _get_path(payload: Any, field_path: str) -> Any:
    for part in field_path.split("."):
        if not isinstance(payload, dict):
            return None
        payload = payload.get(part)
    return payload


__all__ = ["ChapterDeltaTranslator", "TranslationResult"]
