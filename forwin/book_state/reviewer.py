from __future__ import annotations

from collections import defaultdict
from copy import copy, deepcopy
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from forwin.book_state.book_frame_repository import BookLexiconRepository
from forwin.book_state.cognition import CognitionView
from forwin.book_state.knowledge import FACT_REF_PREFIX, HIDDEN_SENSITIVITY
from forwin.book_state.lexicon import TravelMode
from forwin.book_state.map_graph import MapGraph
from forwin.book_state.path_patch import apply_path_patch
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.reveal_schedule import earliest_reveal_chapters
from forwin.book_state.runtime import BookStateRuntime
from forwin.book_state.schema import validate_graph_delta
from forwin.protocol.book_state import (
    ApprovedGraphDeltaSet,
    GraphDelta,
    NodePatch,
    ObserverType,
    PathResult,
)


# The source_type ChapterDeltaTranslator gives the chapter's delta; only it carries the v6 metadata.
CHAPTER_EXTRACTION_SOURCE_TYPE = "chapter_extraction"
DEFAULT_TRAVEL_TOLERANCE = 0.2
_AWARE_STATES = frozenset({"knows", "believes"})


class BookStateReviewIssue(BaseModel):
    severity: str
    code: str
    target_ref: str
    message: str


class BookStateReviewVerdict(BaseModel):
    project_id: str
    chapter_number: int
    verdict_id: str
    accepted: bool
    issues: list[BookStateReviewIssue] = Field(default_factory=list)
    approved_changes: ApprovedGraphDeltaSet | None = None


class BookStateReviewGate:
    """Deterministic guardrail for BookState graph patches before commit."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.projection = BookStateProjection(session)

    def review(
        self,
        changes: ApprovedGraphDeltaSet,
        *,
        base_chapter: int | None = None,
        travel_tolerance: float = DEFAULT_TRAVEL_TOLERANCE,
    ) -> BookStateReviewVerdict:
        if base_chapter is None:
            base_chapter = self.projection.repo.compile_base_chapter(
                changes.project_id, changes.chapter_number
            )
        runtime = self.projection.load_runtime_as_of(
            changes.project_id,
            as_of_chapter=base_chapter,
        )
        issues: list[BookStateReviewIssue] = []
        for delta in changes.graph_deltas:
            issues.extend(_canon_permission_issues(delta))
            issues.extend(_schema_issues(delta))
            issues.extend(_immutable_rule_definition_issues(runtime, delta))
            if delta.source_type == CHAPTER_EXTRACTION_SOURCE_TYPE:
                issues.extend(
                    _ChapterExtractionChecks(
                        self.session, runtime, delta, travel_tolerance=travel_tolerance
                    ).run()
                )
            else:
                issues.extend(_location_patch_issues(runtime, delta))
        accepted = not any(issue.severity == "error" for issue in issues)
        return BookStateReviewVerdict(
            project_id=changes.project_id,
            chapter_number=changes.chapter_number,
            verdict_id=f"book_state_review_{changes.project_id}_{changes.chapter_number}",
            accepted=accepted,
            issues=issues,
            approved_changes=changes if accepted else None,
        )


def _schema_issues(delta: GraphDelta) -> list[BookStateReviewIssue]:
    report = validate_graph_delta(delta)
    return [
        BookStateReviewIssue(
            severity=issue.severity,
            code=issue.code,
            target_ref=issue.target,
            message=issue.message,
        )
        for issue in report.issues
    ]


def _canon_permission_issues(delta: GraphDelta) -> list[BookStateReviewIssue]:
    if delta.allowed_for_canon:
        return []
    return [
        BookStateReviewIssue(
            severity="error",
            code="delta_not_allowed_for_canon",
            target_ref=f"delta:{delta.id}",
            message="GraphDelta is not approved for BookState canon.",
        )
    ]


def _immutable_rule_definition_issues(
    runtime: BookStateRuntime,
    delta: GraphDelta,
) -> list[BookStateReviewIssue]:
    if not _is_writer_delta(delta) or _has_explicit_rule_bridge(delta):
        return []
    issues: list[BookStateReviewIssue] = []
    for patch in delta.node_patches:
        node = runtime.world.nodes_by_id.get(patch.node_id)
        if node is None or str(node.node_type or "") != "rule":
            continue
        field_path = str(patch.field_path or "").strip()
        if not _is_immutable_rule_definition_path(field_path):
            continue
        current = _world_node_field_value(runtime, node.id, field_path)
        if _same_value(current, patch.new_value):
            continue
        issues.append(
            BookStateReviewIssue(
                severity="error",
                code="immutable_rule_definition_conflict",
                target_ref=f"node:{node.id}:{field_path or 'definition'}",
                message=(
                    f"writer delta cannot rewrite canonical rule definition {node.name or node.id} "
                    f"at {field_path or 'definition'} without an explicit canon bridge"
                ),
            )
        )
    return issues


def _is_writer_delta(delta: GraphDelta) -> bool:
    return (
        str(delta.source_type or "") in {"writer", "writer_output", "writing"}
        or str(delta.operation or "") == "apply_writer_contract"
        or str(delta.metadata.get("extraction_path") or "") == "writer_contract"
    )


def _has_explicit_rule_bridge(delta: GraphDelta) -> bool:
    return bool(delta.metadata.get("explicit_rule_definition_bridge")) and str(
        delta.source_type or ""
    ) in {"operator", "canon_repair", "retcon"}


def _is_immutable_rule_definition_path(field_path: str) -> bool:
    return (
        field_path in {"name", "aliases", "summary", "description", "profile"}
        or field_path.startswith(("profile.", "metadata.writer_state."))
    )


def _world_node_field_value(
    runtime: BookStateRuntime,
    node_id: str,
    field_path: str,
):
    node = runtime.world.nodes_by_id.get(node_id)
    if node is None:
        return None
    value = node.model_dump(mode="python")
    for part in field_path.split(".") if field_path else []:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _same_value(left, right) -> bool:
    return left == right


def _location_patch_issues(runtime: BookStateRuntime, delta: GraphDelta) -> list[BookStateReviewIssue]:
    """Reachability of a non-extraction location change, from the patch's old location."""
    issues: list[BookStateReviewIssue] = []
    for patch in delta.node_patches:
        if not _is_location_patch(patch):
            continue
        old_location = str(patch.old_value or "")
        new_location = str(patch.new_value or "")
        if not old_location or not new_location or old_location == new_location:
            continue
        if old_location not in runtime.map.nodes_by_id or new_location not in runtime.map.nodes_by_id:
            issues.append(
                BookStateReviewIssue(
                    severity="error",
                    code="movement_unknown_map_node",
                    target_ref=f"node:{patch.node_id}",
                    message=f"movement references unknown map node: {old_location} -> {new_location}",
                )
            )
            continue
        result = runtime.map.shortest_path(old_location, new_location, metric="travel_time")
        if not result.reachable:
            issues.append(
                BookStateReviewIssue(
                    severity="error",
                    code="movement_unreachable",
                    target_ref=f"node:{patch.node_id}",
                    message=f"no objective map path for movement {old_location} -> {new_location}: {result.blocked_reason}",
                )
            )
    return issues


def _is_location_patch(patch: NodePatch) -> bool:
    return str(patch.op) in {"set", "replace"} and patch.field_path == "state.location_id"


class _ChapterExtractionChecks:
    """World gate v6: one chapter_extraction delta against the runtime before the chapter.

    Movements, knowledge tags and item transfers are read from the delta's metadata in
    extraction order; the matching location and holder patches carry the same moves.
    """

    def __init__(
        self,
        session: Session,
        runtime: BookStateRuntime,
        delta: GraphDelta,
        *,
        travel_tolerance: float,
    ) -> None:
        self.session = session
        self.runtime = runtime
        self.delta = delta
        self.tolerance = travel_tolerance
        self.movements = _records(delta.metadata.get("movements"))
        self.knowledge_tags = _records(delta.metadata.get("knowledge_tags"))
        self.item_transfers = _records(delta.metadata.get("item_transfers"))
        self.facts = _fact_traits(runtime, delta)
        self.issues: list[BookStateReviewIssue] = []
        self._reveal_schedule: dict[str, int] | None = None

    def run(self) -> list[BookStateReviewIssue]:
        self._movement_issues()
        self._dead_entity_issues()
        self._item_holder_issues()
        self._knowledge_issues()
        return self.issues

    def _issue(self, severity: str, code: str, target_ref: str, message: str) -> None:
        self.issues.append(
            BookStateReviewIssue(severity=severity, code=code, target_ref=target_ref, message=message)
        )

    # movements

    def _movement_issues(self) -> None:
        if not self.movements:
            return
        graph = self._working_map()
        lexicon = BookLexiconRepository(self.session).get(self.delta.project_id)
        modes = {mode.id: mode for mode in lexicon.travel_modes}
        start, end = self.delta.story_time_start, self.delta.story_time_end
        chapter_minutes = end - start if start is not None and end is not None else None
        positions: dict[str, list[int]] = defaultdict(list)
        for index, patch in enumerate(self.delta.node_patches):
            if _is_location_patch(patch):
                positions[patch.node_id].append(index)
        for movement in self.movements:
            mover = str(movement.get("entity_id") or "")
            target = f"node:{mover}"
            depart, arrive = _minute(movement.get("depart_minute")), _minute(movement.get("arrive_minute"))
            self._time_offset_issues(target, depart, arrive)
            # The movement's own location patch; restraint releases before it count.
            position = positions[mover].pop(0) if positions[mover] else len(self.delta.node_patches)
            self._restraint_issues(mover, position)
            from_id, to_id = str(movement.get("from_id") or ""), str(movement.get("to_id") or "")
            if not from_id or not to_id or from_id == to_id:
                continue
            path = self._route(graph, mover, from_id, to_id)
            if path is None:
                continue
            if depart is not None and arrive is not None and arrive >= depart:
                claimed = arrive - depart
            else:
                claimed = chapter_minutes
            if claimed is not None:
                self._speed_issues(graph, modes, mover, path, claimed)

    def _working_map(self) -> MapGraph:
        """The map with this delta's new sites, where movers know the routes the delta discovers."""
        discovered: dict[tuple[str, str], set[str]] = defaultdict(set)
        for patch in self.delta.cognition_patches:
            if patch.field_path == "visible_refs" and str(patch.op) == "append":
                discovered[(str(patch.observer_type), patch.observer_id)].add(str(patch.new_value))
        views = dict(self.runtime.cognition_by_observer)
        for key, refs in discovered.items():
            if key in views:
                views[key] = _with_visible_refs(views[key], refs)
        graph = MapGraph(
            nodes=list(self.runtime.map.nodes_by_id.values()),
            # MapGraph rebuilds the runtime reverse copy of each bidirectional edge.
            edges=[
                edge for edge in self.runtime.map.edges_by_id.values()
                if not edge.metadata.get("runtime_reverse_of")
            ],
            cognition_by_observer=views,
        )
        for patch in self.delta.map_patches:
            graph.apply_map_patch(patch)
        return graph

    def _time_offset_issues(self, target: str, depart: int | None, arrive: int | None) -> None:
        start, end = self.delta.story_time_start, self.delta.story_time_end
        problems = []
        if start is not None and end is not None:
            problems.extend(
                f"{label} minute {minute} is outside the chapter's [{start}, {end}]"
                for label, minute in (("depart", depart), ("arrive", arrive))
                if minute is not None and not start <= minute <= end
            )
        if depart is not None and arrive is not None and arrive < depart:
            problems.append(f"arrive minute {arrive} precedes depart minute {depart}")
        if problems:
            self._issue("error", "time_offset_out_of_range", target, "; ".join(problems))

    def _restraint_issues(self, mover: str, position: int) -> None:
        if mover not in self.runtime.world.nodes_by_id:
            return
        before = self.runtime.world.get_state(mover)
        if _restraint_state(before) == "free":
            return
        node = {"state": deepcopy(before)}
        for patch in self.delta.node_patches[:position]:
            if patch.node_id == mover and str(patch.op) != "create" and _is_state_path(patch.field_path):
                apply_path_patch(node, patch.field_path, patch.new_value, op=str(patch.op))
        if _restraint_state(node["state"]) != "free":
            self._issue(
                "error", "restrained_entity_moved", f"node:{mover}",
                f"{mover} is {_restraint_state(before)} and moves without being freed first",
            )

    def _route(self, graph: MapGraph, mover: str, from_id: str, to_id: str) -> PathResult | None:
        """The mover's known route when it has a cognition overlay, else the objective one."""
        target = f"node:{mover}"
        objective = graph.shortest_path(from_id, to_id, metric="travel_time")
        if not objective.reachable:
            self._issue(
                "error", "movement_unreachable", target,
                f"no objective map path for movement {from_id} -> {to_id}: {objective.blocked_reason}",
            )
            return None
        observer = self._overlay_key(mover)
        if observer is None:
            return objective
        known = graph.shortest_path(from_id, to_id, metric="travel_time", observer=observer)
        if not known.reachable:
            self._issue(
                "error", "movement_route_unknown_to_mover", target,
                f"{mover} knows no route {from_id} -> {to_id}; the objective path uses edges it does not know",
            )
            return None
        return known

    def _speed_issues(
        self, graph: MapGraph, modes: dict[str, TravelMode], mover: str, path: PathResult, claimed: int,
    ) -> None:
        edge_types = {
            str(graph.edges_by_id[edge_id].edge_type)
            for edge_id in path.path_edge_ids
            if edge_id in graph.edges_by_id
        }
        # The fastest of the mover's travel modes allowed on every edge of the path.
        multiplier = max(
            (
                modes[mode_id].speed_multiplier
                for mode_id in self.runtime.world.get_state(mover).get("travel_mode_ids") or []
                if mode_id in modes and edge_types <= set(modes[mode_id].allowed_edge_types)
            ),
            default=1.0,
        )
        shortest = path.total_travel_time * 60 / multiplier
        if shortest <= 0:
            return
        detail = (
            f"{mover} covers {path.from_node_id} -> {path.to_node_id} in {claimed} min; "
            f"the shortest is {shortest:g} min"
        )
        if claimed < shortest * (1 - self.tolerance):
            self._issue("error", "movement_too_fast", f"node:{mover}", detail)
        elif claimed < shortest:
            self._issue("warning", "movement_fast_within_tolerance", f"node:{mover}", detail)

    # entities and items

    def _dead_entity_issues(self) -> None:
        """Entities dead before the chapter that act in it; flashback items are exempt."""
        actors = [(str(movement.get("entity_id") or ""), "moves") for movement in self.movements]
        for patch in self.delta.node_patches:
            if str(patch.op) != "create" or str(patch.node_type) != "event" or not isinstance(patch.new_value, dict):
                continue
            state = patch.new_value.get("state") if isinstance(patch.new_value.get("state"), dict) else {}
            actors.extend((str(node_id), "takes part in an event") for node_id in state.get("participant_ids") or [])
        for tag in self.knowledge_tags:
            if tag.get("flashback"):
                continue
            if str(tag.get("observer_type") or "") != ObserverType.READER.value:
                actors.append((str(tag.get("observer_id") or ""), "learns a fact"))
            if tag.get("channel") == "told" and tag.get("source_id"):
                actors.append((str(tag["source_id"]), "tells a fact"))
        for node_id, action in actors:
            if node_id and self.runtime.world.get_state(node_id).get("alive_status") == "dead":
                self._issue("error", "dead_entity_acts", f"node:{node_id}", f"dead entity {node_id} {action}")

    def _item_holder_issues(self) -> None:
        holders: dict[str, str] = {}
        for transfer in self.item_transfers:
            item_id = str(transfer.get("item_id") or "")
            from_id, to_id = str(transfer.get("from_id") or ""), str(transfer.get("to_id") or "")
            current = holders.get(item_id, str(self.runtime.world.get_state(item_id).get("holder_id") or ""))
            # An item with no recorded holder has nothing to contradict.
            if current and from_id and from_id != current:
                self._issue(
                    "error", "item_holder_mismatch", f"node:{item_id}",
                    f"{item_id} is handed over by {from_id} but is held by {current}",
                )
            holders[item_id] = to_id

    # knowledge

    def _knowledge_issues(self) -> None:
        learned: dict[tuple[str, str], str] = {}
        revealed: set[str] = set()
        for tag in self.knowledge_tags:
            observer_id = str(tag.get("observer_id") or "")
            fact_ref = str(tag.get("fact_ref") or "")
            state, channel = str(tag.get("state") or ""), str(tag.get("channel") or "")
            source_id = str(tag.get("source_id") or "")
            if channel == "told" and source_id:
                source_state = learned.get((source_id, fact_ref)) or self._recorded_state(source_id, fact_ref)
                if source_state not in _AWARE_STATES:
                    self._issue(
                        "error", "told_by_unaware_source", f"node:{source_id}",
                        f"{source_id} tells {fact_ref} without knowing it (state: {source_state})",
                    )
            if channel == "rumor" and state == "knows" and self._hidden(fact_ref):
                self._issue(
                    "warning", "unexplained_secret_knowledge", fact_ref,
                    f"{observer_id} knows the secret {fact_ref} from a rumor",
                )
            if str(tag.get("observer_type") or "") == ObserverType.READER.value:
                for ref in (fact_ref, str(tag.get("touches_secret_ref") or "")):
                    if ref and ref not in revealed and self._hidden(ref):
                        revealed.add(ref)
                        self._reveal_issues(ref)
            contradicts = str(tag.get("contradicts_ref") or "")
            # A false fact that contradicts a secret is misdirection, not a contradiction of canon.
            if contradicts and self._hidden(contradicts) and self._true(fact_ref):
                self._issue(
                    "error", "contradicts_hidden_fact", contradicts,
                    f"{fact_ref} is presented as true but contradicts the hidden {contradicts}",
                )
            learned[(observer_id, fact_ref)] = state

    def _recorded_state(self, observer_id: str, fact_ref: str) -> str:
        """The observer's state for the fact before the chapter, defaulting as CognitionView.knowledge_state."""
        key = self._overlay_key(observer_id)
        recorded = self.runtime.cognition_by_observer[key].knowledge_by_ref.get(fact_ref) if key else None
        if recorded:
            return recorded
        return "unknown" if self._hidden(fact_ref) else "knows"

    def _overlay_key(self, node_id: str) -> tuple[str, str] | None:
        """The cognition overlay of a world node; node ids are unique across observer types."""
        return next(
            (
                key for key in self.runtime.cognition_by_observer
                if key[1] == node_id and key[0] != ObserverType.READER.value
            ),
            None,
        )

    def _reveal_issues(self, fact_ref: str) -> None:
        if self._reveal_schedule is None:
            self._reveal_schedule = earliest_reveal_chapters(self.session, self.delta.project_id)
        earliest = self._reveal_schedule.get(fact_ref.removeprefix(FACT_REF_PREFIX))
        if earliest is None:
            self._issue(
                "warning", "unscheduled_reveal", fact_ref,
                f"the reader advances on {fact_ref}, which has no reveal schedule",
            )
        elif self.delta.chapter_number < earliest:
            self._issue(
                "error", "premature_reveal", fact_ref,
                f"the reader advances on {fact_ref} in chapter {self.delta.chapter_number}; "
                f"its earliest reveal chapter is {earliest}",
            )

    def _hidden(self, fact_ref: str) -> bool:
        sensitivity, _truth = self.facts.get(fact_ref.removeprefix(FACT_REF_PREFIX), ("", ""))
        return sensitivity in HIDDEN_SENSITIVITY

    def _true(self, fact_ref: str) -> bool:
        _sensitivity, truth = self.facts.get(fact_ref.removeprefix(FACT_REF_PREFIX), ("", ""))
        return truth == "true"


def _records(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _minute(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _is_state_path(field_path: str) -> bool:
    return field_path == "state" or field_path.startswith("state.")


def _restraint_state(state: dict[str, Any]) -> str:
    restraint = state.get("restraint")
    return str((restraint.get("state") if isinstance(restraint, dict) else None) or "free")


def _with_visible_refs(view: CognitionView, refs: set[str]) -> CognitionView:
    """A copy of ``view`` that also sees ``refs``; the runtime's view stays untouched."""
    augmented = copy(view)
    augmented.visible_refs = view.visible_refs | refs
    return augmented


def _fact_traits(runtime: BookStateRuntime, delta: GraphDelta) -> dict[str, tuple[str, str]]:
    """Fact id -> (sensitivity_level, truth_value) before the chapter plus the facts it creates."""
    traits = {
        fact_id: (str(fact.sensitivity_level or ""), str(fact.truth_value or ""))
        for fact_id, fact in runtime.world.facts_by_id.items()
    }
    for patch in delta.fact_patches:
        if str(patch.op) != "create":
            continue
        payload = patch.new_value if isinstance(patch.new_value, dict) else {}
        traits[patch.fact_id] = (
            str(patch.sensitivity_level or payload.get("sensitivity_level") or ""),
            str(patch.truth_value or payload.get("truth_value") or "true"),
        )
    return traits


__all__ = ["BookStateReviewGate", "BookStateReviewIssue", "BookStateReviewVerdict"]
