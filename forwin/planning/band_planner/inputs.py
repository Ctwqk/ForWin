"""The deterministic inputs of ``plan.band``: the band's beats, experience schedule, world digest and offered refs."""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.book_state.book_frame_repository import BookLexiconRepository, StoryCalendarRepository
from forwin.book_state.knowledge import FACT_REF_PREFIX, is_concealed
from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.query import BookStateQuery
from forwin.book_state.runtime import BookStateRuntime, resolve_world_node_location_id
from forwin.book_state.schema import ACTOR_NODE_TYPES
from forwin.book_state.story_clock import StoryCalendar
from forwin.book_state.thread_sampling import sample_active_threads
from forwin.experience.band_scheduler import BandExperienceScheduler
from forwin.experience.service import AudienceCalibrationProfile, ExperiencePlanningService
from forwin.experience.trope_cooldown import recent_trope_usage
from forwin.experience.types import BeatSheetExperienceInput
from forwin.models.draft import ChapterDraft
from forwin.models.phase import BandExperiencePlan
from forwin.models.project import ArcPlanVersion, ChapterPlan
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.planning.band_planner.types import band_id
from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
from forwin.planning.beat_sheet.types import READER_OBSERVER, ArcBeatSheetData, BandSpan, Milestone
from forwin.planning.progression_rules import active_progression_rules_for_chapter
from forwin.planning.world_digest import build_world_digest
from forwin.protocol.experience import BandDelightSchedule, BandRewardItem
from forwin.protocol.trope_library import trope_template_index
from forwin.runtime.policy import RuntimePolicy
from forwin.world_engine.conflict_repository import WorldConflictRepository
from forwin.world_engine.conflict_resolution import writer_response_text
from forwin.world_engine.propagation import latest_news_in_transit
from forwin.world_engine.travel import estimate_travel_from
from forwin.writer.prompt_core.world_engine import EVENT_FACT_TYPE, MAX_ADJUDICATION_EVENT_FACTS


# The digest window's chapter length when the previous band gives no measure of its own: one story day.
MINUTES_PER_DAY = 24 * 60
_PENDING_INTENT_STATUSES = frozenset({"planned", "due", "held"})
_TROPE_BLOCKING_RULES = frozenset({"trope_filter", "repetition_ban"})
_RECENT_EVENT_CHAPTERS = 3
_EVENTS_LIMIT = 20
_RELATIONS_LIMIT = 40
_NEWS_LIMIT = 20
_STALE_THREAD_WINDOW = 3
_THREAD_LIMIT = 20
_LOG = logging.getLogger(__name__)


class BandPayloadBudgetExceeded(ValueError):
    """The payload's required context cannot fit within its character budget."""

    def __init__(self, message: str, *, trim_record: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.trim_record = trim_record or {}


@dataclass(frozen=True)
class PendingResponse:
    """A plot response a chapter of the band must carry: by stating ``goal`` or by binding one of ``refs``.

    ``refs`` are the conflict's refs that some binding kind accepts; it is empty when only the goal can carry it.
    """

    conflict_id: str
    goal: str
    refs: frozenset[str]


@dataclass(frozen=True)
class OfferedRefs:
    """Every ref the prompt offers, by what may name it; an answer naming anything else is invalid."""

    actor_ids: frozenset[str]
    character_ids: frozenset[str]
    location_ids: frozenset[str]
    # fact ref -> proposition
    facts: Mapping[str, str]
    intent_ids: frozenset[str]
    event_ids: frozenset[str]
    clock_ids: frozenset[str]
    relation_ids: frozenset[str]
    news_fact_refs: frozenset[str]
    milestone_ids: frozenset[str]
    time_units: frozenset[str]
    # node id -> name of the concealed world nodes, whose names writer-facing plan fields must not carry.
    concealed_names: Mapping[str, str]
    rewards_by_chapter: Mapping[int, tuple[BandRewardItem, ...]]
    pending: tuple[PendingResponse, ...] = ()

    def binding_refs(self, kind: str) -> frozenset[str]:
        return {
            "intent": self.intent_ids, "event": self.event_ids, "clock": self.clock_ids,
            "relation": self.relation_ids, "news": self.news_fact_refs, "secret": frozenset(self.facts),
        }.get(kind, frozenset())


@dataclass
class BandPlanInputs:
    payload: dict[str, Any]
    digest_text: str
    offered: OfferedRefs
    # The latest canon chapter: the runtime, refs and story time are read as of it.
    as_of: int
    runtime: BookStateRuntime
    calendar: StoryCalendar | None
    lexicon: BookLexicon
    experience: BeatSheetExperienceInput
    calibration: AudienceCalibrationProfile
    schedule: BandDelightSchedule
    # Whether ``schedule`` is new; a band keeps the schedule it was first planned with.
    schedule_is_new: bool
    window_end_minute: int
    trim_record: dict[str, Any] = field(default_factory=dict)


def build_band_inputs(
    session: Session,
    *,
    project_id: str,
    arc_plan: ArcPlanVersion,
    beat_sheet: ArcBeatSheetData,
    band: BandSpan,
    chapter_numbers: Sequence[int],
    rows: Mapping[int, ChapterPlan],
    as_of: int,
    runtime: BookStateRuntime,
    policy: RuntimePolicy,
    notes: Sequence[str],
    trope_cost_ceiling: int,
) -> BandPlanInputs:
    """Everything ``plan.band`` reads for ``chapter_numbers`` of ``band``; ``rows`` are the band's plan rows."""
    world = runtime.world
    calendar = StoryCalendarRepository(session).get(project_id)
    lexicon = BookLexiconRepository(session).get(project_id)
    experience = BeatSheetExperienceInput.from_beat_sheet(beat_sheet, band)
    calibration = band_calibration(session, project_id=project_id, chapter_start=band.chapter_start)
    schedule = _saved_schedule(session, project_id=project_id, arc_plan_id=arc_plan.id, band=band)
    schedule_is_new = schedule is None
    if schedule is None:
        schedule = BandExperienceScheduler().derive_band_delight_schedule(
            band_id=band_id(band), chapter_start=band.chapter_start, chapter_end=band.chapter_end,
            experience=experience, active_band=[rows[number] for number in sorted(rows)],
            calibration=calibration, cost_ceiling=trope_cost_ceiling,
        )
    previous = previous_band(session, project_id, band)
    query = BookStateQuery(session)
    now = query.current_story_minute(project_id, as_of_chapter=as_of) or 0
    per_chapter = _minutes_per_chapter(query, project_id, previous, as_of=as_of) or MINUTES_PER_DAY
    # The window runs to the band's last chapter, however many of its chapters are planned now.
    window_end = now + max(1, band.chapter_end - as_of) * per_chapter
    digest = build_world_digest(
        session, project_id=project_id, as_of_chapter=as_of, protagonist_ref=beat_sheet.protagonist_ref,
        window_end_minute=window_end, budget_chars=policy.planning.digest_budget_chars, runtime=runtime,
    )

    actors = [node for node in world.nodes_by_id.values() if node.is_active and str(node.node_type) in ACTOR_NODE_TYPES]
    intents = [node for node in world.nodes_by_id.values() if node.is_active and str(node.node_type) == "intent"
               and world.get_state(node.id).get("status") in _PENDING_INTENT_STATUSES]
    due_onstage = []
    for node in intents:
        state = world.get_state(node.id)
        at = state.get("scheduled_at")
        if node.profile.get("requires_onstage") is True and (
            state.get("status") == "due" or (type(at) is int and at <= window_end)
        ):
            due_onstage.append(node.id)
    clocks = [node for node in world.nodes_by_id.values() if node.is_active and str(node.node_type) == "clock"
              and world.get_state(node.id).get("status") == "running"]
    events = sorted((node for node in world.nodes_by_id.values() if node.is_active and str(node.node_type) == "event"
                     and node.created_at_chapter > as_of - _RECENT_EVENT_CHAPTERS),
                    key=lambda node: (-node.created_at_chapter, node.id))[:_EVENTS_LIMIT]
    protagonist = beat_sheet.protagonist_ref
    relations = sorted((edge for edge in world.edges_by_id.values() if edge.is_active),
                       key=lambda edge: (protagonist not in (edge.source_id, edge.target_id), edge.id))[:_RELATIONS_LIMIT]
    news = _news(session, project_id, window_end=window_end)
    required_facts = {
        item.fact_ref for item in beat_sheet.reveal_targets
        if item.range[0] <= band.chapter_end and item.range[1] >= band.chapter_start
    }
    protections = [item for item in beat_sheet.protections if item.until_chapter >= band.chapter_start]
    required_facts.update(ref for item in protections for ref in (item.ref, item.fact_ref)
                          if ref.startswith(FACT_REF_PREFIX))
    facts = _facts(runtime, required_refs=required_facts | {item["fact_ref"] for item in news})
    milestones = _nearby_milestones(beat_sheet, band)
    bindable = {*(node.id for node in intents), *(node.id for node in events), *(node.id for node in clocks),
                *(edge.id for edge in relations), *(item["fact_ref"] for item in news), *(item["ref"] for item in facts)}
    pending_rows = WorldConflictRepository(session).pending_plot_responses(project_id)
    pending = [
        PendingResponse(
            conflict_id=row.id, goal=writer_response_text(row),
            refs=frozenset(bindable.intersection(
                [*json.loads(row.involved_refs_json or "[]"), row.intent_ref, row.actor_ref],
            )),
        )
        for row in pending_rows
    ]
    templates = trope_template_index()
    planned = set(chapter_numbers)
    payload: dict[str, Any] = {
        "arc": {
            "arc_number": int(arc_plan.arc_number or 0), "title": arc_plan.title, "goal": arc_plan.goal,
            "stakes": arc_plan.stakes, "payoff_direction": arc_plan.payoff_direction,
            "chapter_start": arc_plan.chapter_start, "chapter_end": arc_plan.chapter_end,
        },
        "band": {"band_index": band.band_index, "chapter_start": band.chapter_start, "chapter_end": band.chapter_end},
        "chapters_to_plan": sorted(planned),
        "kept_chapters": [_plan_summary(rows[number]) for number in sorted(rows) if number not in planned],
        "current_plans": [_plan_summary(rows[number]) for number in sorted(rows) if number in planned],
        "notes": [text for note in notes if (text := str(note).strip())],
        "protagonist_ref": protagonist,
        "phases": [phase.model_dump(mode="json") for phase in sorted(beat_sheet.phases, key=lambda item: item.chapter_range)
                   if phase.chapter_range[0] <= band.chapter_end and phase.chapter_range[1] >= band.chapter_start],
        "milestones": [item.model_dump(mode="json") for item in milestones],
        "reveal_targets": [item.model_dump(mode="json") for item in beat_sheet.reveal_targets
                           if item.range[0] <= band.chapter_end and item.range[1] >= band.chapter_start],
        "protections": [item.model_dump(mode="json") for item in protections],
        "exit_state": [item.model_dump(mode="json") for item in beat_sheet.exit_state],
        "experience_schedule": {
            "rewards": [
                {"chapter": item.chapter_hint, "category": item.category, "intent": item.intent,
                 "template_id": item.template_id,
                 "template_name": templates[item.template_id].display_name if item.template_id in templates else ""}
                for item in schedule.scheduled_rewards
            ],
            "curiosity_beats": [item.model_dump(mode="json") for item in schedule.curiosity_beats],
            "immersion_anchor_scene_goal": schedule.immersion_anchor_scene_goal,
        },
        "previous_band": (
            {"chapter_start": previous.chapter_start, "chapter_end": previous.chapter_end,
             "accepted_chapters": _accepted_summaries(session, project_id, previous)}
            if previous is not None else None
        ),
        "story_now": now,
        "time_units": sorted({*(unit.name for unit in calendar.units),
                              *(alias.name for alias in lexicon.time_unit_aliases)}) if calendar is not None else [],
        "actors": [
            {"id": node.id, "name": node.name, "kind": str(node.node_type),
             "concealed": is_concealed(world, node),
             "location_ref": resolve_world_node_location_id(world, node.id),
             "alive_status": world.get_state(node.id).get("alive_status"),
             "restraint": world.get_state(node.id).get("restraint"),
             "tier": world.get_state(node.id).get("actor_tier")}
            for node in sorted(actors, key=lambda item: item.id)
        ],
        "locations": [{"id": node.id, "name": node.name, "subworld_id": node.subworld_id}
                      for node in sorted(runtime.map.nodes_by_id.values(), key=lambda item: item.id)],
        "world_nodes": [
            {"id": node.id, "name": node.name, "kind": str(node.node_type),
             "concealed": is_concealed(world, node),
             "location_ref": resolve_world_node_location_id(world, node.id)}
            for node in sorted(world.nodes_by_id.values(), key=lambda item: item.id)
            if node.is_active and str(node.node_type) not in (*ACTOR_NODE_TYPES, "intent", "event", "clock")
        ],
        "facts": facts,
        "intents": [
            {"id": node.id, "owner_ref": node.profile.get("owner_ref"), "action_type": node.profile.get("action_type"),
             "summary": node.profile.get("summary") or node.summary, "status": world.get_state(node.id).get("status"),
             "scheduled_at": world.get_state(node.id).get("scheduled_at"),
             "requires_onstage": node.profile.get("requires_onstage"), "location_ref": node.profile.get("location_id")}
            for node in sorted(intents, key=lambda item: item.id)
        ],
        "due_onstage_intent_ids": sorted(due_onstage),
        "clocks": [
            {"id": node.id, "label": node.profile.get("label") or node.name, "scope": node.profile.get("scope"),
             "due_at": world.get_state(node.id).get("due_at")}
            for node in sorted(clocks, key=lambda item: item.id)
        ],
        "events": [{"id": node.id, "summary": node.summary or node.name, "chapter": node.created_at_chapter}
                   for node in events],
        "relations": [{"id": edge.id, "source": edge.source_id, "edge_type": edge.edge_type, "target": edge.target_id}
                      for edge in relations],
        "news": news,
        "pending_responses": [
            {"id": item.conflict_id, "goal": item.goal, "refs": sorted(item.refs), "summary": row.summary}
            for item, row in zip(pending, pending_rows, strict=True)
        ],
        "open_obligations": [
            {"id": item.id, "summary": item.summary, "deadline_chapter": item.deadline_chapter,
             "priority": item.priority, "subject_refs": item.subject_refs, "payoff_test": item.payoff_test}
            for item in NarrativeObligationRepository(session).list_active_for_context(
                project_id, chapter_number=as_of + 1,
            )
        ],
        "stalled_threads": _stalled_threads(session, project_id, as_of=as_of, runtime=runtime),
    }
    trim_record = _trim_payload(
        payload, runtime=runtime, lexicon=lexicon, rows=rows, window_minutes=window_end - now,
        budget_chars=policy.planning.band_payload_budget_chars, required_facts=required_facts,
    )
    offered = _offered_refs(payload, runtime=runtime)
    return BandPlanInputs(
        payload=payload, digest_text=digest.text, offered=offered, as_of=as_of, runtime=runtime,
        calendar=calendar, lexicon=lexicon, experience=experience, calibration=calibration, schedule=schedule,
        schedule_is_new=schedule_is_new, window_end_minute=window_end, trim_record=trim_record,
    )


def _trim_payload(
    payload: dict[str, Any], *, runtime: BookStateRuntime, lexicon: BookLexicon,
    rows: Mapping[int, ChapterPlan], window_minutes: int, budget_chars: int, required_facts: set[str],
) -> dict[str, Any]:
    """Narrow locality once, then trim optional items round-robin by category.

    Remove the least relevant item that no kept item references; pinned items are skipped,
    so trimming never leaves dangling refs.
    """
    protagonist = payload["protagonist_ref"]
    origin = resolve_world_node_location_id(runtime.world, protagonist)
    required_intents = set(payload["due_onstage_intent_ids"])
    # Audit warnings are not available yet; retain clocks due by the estimated band end, including overdue ones.
    required_clocks = {item["id"] for item in payload["clocks"]
                       if type(item["due_at"]) is int and item["due_at"] <= payload["story_now"] + window_minutes}
    required_actors = {protagonist} | {
        item["observer"] for item in payload["reveal_targets"] if item["observer"] != READER_OBSERVER
    } | {item["observer_ref"] for item in payload["protections"] if item["observer_ref"] != READER_OBSERVER} | {
        item["owner_ref"] for item in payload["intents"] if item["id"] in required_intents
    }
    news_actors = {item["observer_ref"] for item in payload["news"]}
    news_facts = {item["fact_ref"] for item in payload["news"]}
    location_order, actor_order, node_order = _payload_locality_orders(
        payload, runtime=runtime, lexicon=lexicon, rows=rows,
        protagonist=protagonist, origin=origin, window_minutes=window_minutes,
    )
    farthest = len(location_order) + 1

    def deadline(value):
        return max(0, value - payload["story_now"]) if isinstance(value, (int, float)) else float("inf")

    def rank(key, item):
        if key == "locations":
            return (location_order[item["id"]],)
        if key in {"actors", "world_nodes"}:
            return (node_order[item["id"]],)
        if key == "facts":
            fact = runtime.world.facts_by_id[item["ref"].removeprefix(FACT_REF_PREFIX)]
            return (min((node_order.get(ref, farthest) for ref in item["related_refs"]), default=farthest),
                    -fact.created_at_chapter)
        if key == "events":
            return (node_order[item["id"]], -item["chapter"])
        if key == "clocks":
            return (deadline(item["due_at"]), node_order[item["id"]])
        if key == "intents":
            return (actor_order.get(item["owner_ref"], farthest), deadline(item["scheduled_at"]))
        if key == "relations":
            return tuple(sorted(actor_order.get(item[ref], farthest) for ref in ("source", "target")))
        if key == "news":
            return (actor_order.get(item["observer_ref"], farthest), deadline(item["arrive_at"]))
        return (item["priority"], item["last_active_chapter"])

    locations = set(location_order)
    payload["actors"] = [item for item in payload["actors"] if item["id"] in required_actors | news_actors
                         or not item["location_ref"] or item["location_ref"] in locations]
    actors = {item["id"] for item in payload["actors"]}
    payload["world_nodes"] = [item for item in payload["world_nodes"]
                              if not item["location_ref"] or item["location_ref"] in locations]
    payload["intents"] = [item for item in payload["intents"] if item["owner_ref"] in actors]
    intents = {item["id"] for item in payload["intents"]}
    nodes = actors | locations | intents | {
        item["id"] for key in ("world_nodes", "events", "clocks") for item in payload[key]
    }
    payload["facts"] = [item for item in payload["facts"] if item["ref"] in required_facts | news_facts
                        or not item["related_refs"] or nodes.intersection(item["related_refs"])]
    payload["relations"] = [item for item in payload["relations"]
                            if item["source"] in actors and item["target"] in actors]
    for key in ("actors", "world_nodes", "intents"):
        for item in payload[key]:
            if item["location_ref"] not in locations:
                item["location_ref"] = ""
    for item in payload["facts"]:
        item["related_refs"] = [ref for ref in item["related_refs"] if ref in nodes]

    _sync_context_refs(payload)
    trimmable = ("facts", "events", "clocks", "relations", "news", "stalled_threads",
                 "locations", "intents", "actors", "world_nodes")
    optional = {}
    for key in trimmable:
        payload[key].sort(key=lambda item: (rank(key, item), item.get("id", item.get("ref", item.get("name", "")))))
        optional[key] = [item for item in payload[key]
                         if not (key == "actors" and item["id"] in required_actors)
                         and not (key == "locations" and item["id"] == origin)
                         and not (key == "facts" and item["ref"] in required_facts)
                         and not (key == "intents" and item["id"] in required_intents)
                         and not (key == "clocks" and item["id"] in required_clocks)]

    return _trim_optional_items(payload, optional=optional, trimmable=trimmable, budget_chars=budget_chars)


def _payload_locality_orders(
    payload: dict[str, Any], *, runtime: BookStateRuntime, lexicon: BookLexicon,
    rows: Mapping[int, ChapterPlan], protagonist: str, origin: str, window_minutes: int,
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    band_locations = {ref for row in rows.values() for ref in json.loads(row.location_refs_json or "[]")}
    subworlds = {runtime.map.nodes_by_id[ref].subworld_id for ref in band_locations
                if ref in runtime.map.nodes_by_id}
    if not subworlds and origin in runtime.map.nodes_by_id:
        subworlds.add(runtime.map.nodes_by_id[origin].subworld_id)
    travel = estimate_travel_from(runtime, lexicon, mover_id=protagonist, from_location_id=origin) if origin in runtime.map.nodes_by_id else {}

    def location_rank(item):
        estimate = travel.get(item["id"])
        minutes = estimate.minutes if estimate is not None and estimate.minutes is not None else float("inf")
        return (item["subworld_id"] not in subworlds, minutes, item["id"])

    payload["locations"] = sorted((item for item in payload["locations"]
                                    if not subworlds or item["subworld_id"] in subworlds
                                    or (travel.get(item["id"]) is not None
                                        and travel[item["id"]].minutes is not None
                                        and travel[item["id"]].minutes <= window_minutes)), key=location_rank)
    location_order = {item["id"]: index for index, item in enumerate(payload["locations"])}
    farthest = len(location_order) + 1
    actor_order = {item["id"]: (-1 if item["id"] == protagonist
                               else location_order.get(item["location_ref"], farthest))
                   for item in payload["actors"]}
    node_order = {**location_order, **actor_order}
    for key in ("world_nodes", "events", "clocks", "intents"):
        for item in payload[key]:
            node = runtime.world.nodes_by_id[item["id"]]
            state = runtime.world.get_state(node.id)
            refs = [node.profile.get("owner_ref"), *(node.profile.get("involved_node_refs") or []),
                    *(state.get("participant_ids") or [])]
            node_order[node.id] = min(
                [location_order.get(resolve_world_node_location_id(runtime.world, node.id), farthest),
                 *(actor_order.get(ref, farthest) for ref in refs)],
            )

    return location_order, actor_order, node_order


def _sync_context_refs(payload: dict[str, Any]) -> None:
    intents = {item["id"] for item in payload["intents"]}
    payload["due_onstage_intent_ids"] = [ref for ref in payload["due_onstage_intent_ids"] if ref in intents]
    bindable = intents | {item["ref"] for item in payload["facts"]} | {
        item["id"] for key in ("events", "clocks", "relations") for item in payload[key]
    }
    for item in payload["pending_responses"]:
        item["refs"] = [ref for ref in item["refs"] if ref in bindable]


def _payload_dependencies(key: str, item: dict[str, Any]) -> list[str]:
    if key == "facts":
        return item["related_refs"]
    if key == "news":
        return [item["fact_ref"], item["observer_ref"]]
    if key == "relations":
        return [item["source"], item["target"]]
    if key == "intents":
        return [item["owner_ref"], item["location_ref"]]
    if key in {"actors", "world_nodes"}:
        return [item["location_ref"]]
    return []


def _trim_optional_items(
    payload: dict[str, Any], *, optional: dict[str, list[dict[str, Any]]],
    trimmable: tuple[str, ...], budget_chars: int,
) -> dict[str, Any]:
    # A retained item keeps its subjects available. Dropping a location or actor must not
    # silently remove several other categories, or leave a news item without its fact.
    referenced = Counter(ref for key in trimmable for item in payload[key] for ref in _payload_dependencies(key, item) if ref)
    def size(value: Any) -> int:
        return len(json.dumps(value, ensure_ascii=False))

    before = current_size = size(payload)
    removed: Counter[str] = Counter()
    while current_size > budget_chars:
        candidates = {
            key: next((item for item in reversed(optional[key])
                       if not referenced[item.get("id", item.get("ref", ""))]), None)
            for key in trimmable
        }
        # Keep each category's best item while any category can still shed a worse one.
        multiple = any(item is not None and len(optional[key]) > 1 for key, item in candidates.items())
        progress = False
        for key, item in candidates.items():
            if item is None or (multiple and len(optional[key]) == 1):
                continue
            current_size -= size(item) + (2 if len(payload[key]) > 1 else 0)
            payload[key].remove(item)
            optional[key].remove(item)
            referenced.subtract(ref for ref in _payload_dependencies(key, item) if ref)
            old_context_size = size(payload["pending_responses"]) + size(payload["due_onstage_intent_ids"])
            _sync_context_refs(payload)
            current_size += size(payload["pending_responses"]) + size(payload["due_onstage_intent_ids"]) - old_context_size
            removed[key] += 1
            progress = True
            if current_size <= budget_chars:
                break
        if not progress:
            raise BandPayloadBudgetExceeded(
                f"band payload budget cannot fit required planning context: {current_size} characters "
                f"remain; planning.band_payload_budget_chars={budget_chars}",
                trim_record={"before_chars": before, "after_chars": current_size,
                             "budget_chars": budget_chars, "removed": dict(removed), "overflow": True},
            )
    if removed:
        _LOG.info("Band payload trimmed: before_chars=%s after_chars=%s budget_chars=%s removed=%s",
                  before, current_size, budget_chars, dict(removed))
    return {"before_chars": before, "after_chars": current_size, "budget_chars": budget_chars,
            "removed": dict(removed), "overflow": False}


def _offered_refs(payload: dict[str, Any], *, runtime: BookStateRuntime) -> OfferedRefs:
    """Derive validation refs from the final serialized-payload object."""
    rewards: dict[int, list[BandRewardItem]] = {}
    for item in payload["experience_schedule"]["rewards"]:
        rewards.setdefault(item["chapter"], []).append(BandRewardItem(
            chapter_hint=item["chapter"], category=item["category"], intent=item["intent"],
            template_id=item["template_id"],
        ))
    return OfferedRefs(
        actor_ids=frozenset(item["id"] for item in payload["actors"]),
        character_ids=frozenset(item["id"] for item in payload["actors"] if item["kind"] == "character"),
        location_ids=frozenset(item["id"] for item in payload["locations"]),
        facts={item["ref"]: item["proposition"] for item in payload["facts"]},
        intent_ids=frozenset(item["id"] for item in payload["intents"]),
        event_ids=frozenset(item["id"] for item in payload["events"]),
        clock_ids=frozenset(item["id"] for item in payload["clocks"]),
        relation_ids=frozenset(item["id"] for item in payload["relations"]),
        news_fact_refs=frozenset(item["fact_ref"] for item in payload["news"]),
        milestone_ids=frozenset(item["id"] for item in payload["milestones"]),
        time_units=frozenset(payload["time_units"]),
        concealed_names=_concealed_names(runtime),
        rewards_by_chapter={number: tuple(items) for number, items in rewards.items()},
        pending=tuple(PendingResponse(item["id"], item["goal"], frozenset(item["refs"]))
                      for item in payload["pending_responses"]),
    )


def band_calibration(session: Session, *, project_id: str, chapter_start: int) -> AudienceCalibrationProfile:
    """Audience calibration for the band's experience schedule: feedback trends, trope cooldown, progression bans."""
    calibration = ExperiencePlanningService().build_audience_calibration_profile(session=session, project_id=project_id)
    calibration.recent_template_ids, calibration.recent_trope_categories = recent_trope_usage(
        session, project_id=project_id,
    )
    blocked_ids: list[str] = []
    blocked_categories: list[str] = []
    for rule in active_progression_rules_for_chapter(session, project_id=project_id, chapter_number=chapter_start):
        if rule.rule_type in _TROPE_BLOCKING_RULES:
            blocked_ids.extend(text for item in rule.payload.get("blocked_template_ids", []) if (text := str(item).strip()))
            blocked_categories.extend(text for item in rule.payload.get("blocked_categories", [])
                                      if (text := str(item).strip()))
    calibration.progression_blocked_template_ids = blocked_ids
    calibration.progression_blocked_categories = blocked_categories
    return calibration


def previous_band(session: Session, project_id: str, band: BandSpan) -> BandSpan | None:
    """The band of an active beat sheet that ends right before ``band``, in this arc or the previous one."""
    repo = ArcBeatSheetRepository(session)
    for row in repo.active_for_project(project_id):
        for item in repo.load(row).band_breakdown:
            if item.chapter_end == band.chapter_start - 1:
                return item
    return None


def _minutes_per_chapter(query: BookStateQuery, project_id: str, previous: BandSpan | None, *, as_of: int) -> int | None:
    """The previous band's measured story minutes per chapter, when all of it is canon and time passed in it."""
    if previous is None or previous.chapter_end > as_of:
        return None
    start = query.current_story_minute(project_id, as_of_chapter=previous.chapter_start - 1)
    end = query.current_story_minute(project_id, as_of_chapter=previous.chapter_end)
    if start is None or end is None:
        return None
    average = round((end - start) / (previous.chapter_end - previous.chapter_start + 1))
    return average if average > 0 else None


def _saved_schedule(session: Session, *, project_id: str, arc_plan_id: str, band: BandSpan) -> BandDelightSchedule | None:
    row = session.scalar(
        select(BandExperiencePlan)
        .where(BandExperiencePlan.project_id == project_id, BandExperiencePlan.arc_id == arc_plan_id,
               BandExperiencePlan.band_id == band_id(band))
        .order_by(BandExperiencePlan.created_at.desc(), BandExperiencePlan.id.desc())
        .limit(1)
    )
    return BandDelightSchedule.model_validate_json(row.schedule_json) if row is not None else None


def _nearby_milestones(beat_sheet: ArcBeatSheetData, band: BandSpan) -> list[Milestone]:
    """Nearby milestones, plus overdue hard milestones the feasibility audit still requires."""
    width = band.chapter_end - band.chapter_start + 1
    low, high = band.chapter_start - width, band.chapter_end + width
    return sorted((item for item in beat_sheet.milestones if (item.target_range[0] <= high and item.target_range[1] >= low)
                   or (item.hard and item.target_range[1] < band.chapter_start)),
                  key=lambda item: (item.target_range, item.id))


def _facts(runtime: BookStateRuntime, *, required_refs: set[str]) -> list[dict[str, Any]]:
    """Standing and recent event facts, plus every fact required by the band's beats."""
    facts = runtime.world.facts_by_id.values()
    events = sorted((fact for fact in facts if fact.fact_type == EVENT_FACT_TYPE),
                    key=lambda fact: (-fact.created_at_chapter, fact.id))[:MAX_ADJUDICATION_EVENT_FACTS]
    event_ids = {fact.id for fact in events}
    shown = [fact for fact in facts if fact.fact_type != EVENT_FACT_TYPE or fact.id in event_ids
             or f"{FACT_REF_PREFIX}{fact.id}" in required_refs]
    return [
        {"ref": f"{FACT_REF_PREFIX}{fact.id}", "proposition": fact.proposition, "sensitivity": fact.sensitivity_level,
         "related_refs": list(fact.related_node_refs)}
        for fact in sorted(shown, key=lambda item: item.id)
    ]


def _concealed_names(runtime: BookStateRuntime) -> dict[str, str]:
    """Active world nodes concealed as the chapter world view conceals them; a name shorter than two characters
    is left out, since finding it inside other words would reject innocent text."""
    world = runtime.world
    names = {}
    for node in world.nodes_by_id.values():
        if node.is_active and is_concealed(world, node) and len(name := (node.name or "").strip()) >= 2:
            names[node.id] = name
    return names


def _news(session: Session, project_id: str, *, window_end: int) -> list[dict[str, Any]]:
    """News in transit that arrives within the window, one entry per fact, earliest arrival first."""
    seen: dict[str, dict[str, Any]] = {}
    for item in sorted(latest_news_in_transit(session, project_id),
                       key=lambda news: (news.arrive_at, news.fact_ref, news.observer_ref)):
        if item.arrive_at <= window_end and item.fact_ref not in seen:
            seen[item.fact_ref] = {"fact_ref": item.fact_ref, "observer_ref": item.observer_ref,
                                   "arrive_at": item.arrive_at}
    return list(seen.values())[:_NEWS_LIMIT]


def _plan_summary(plan: ChapterPlan) -> dict[str, Any]:
    return {"chapter_number": plan.chapter_number, "status": plan.status, "title": plan.title,
            "one_line": plan.one_line, "goals": json.loads(plan.goals_json or "[]")}


def _accepted_summaries(session: Session, project_id: str, band: BandSpan) -> list[dict[str, Any]]:
    """The accepted chapters of ``band``, each with its latest draft's summary (or its one-line plan)."""
    plans = session.scalars(
        select(ChapterPlan)
        .where(ChapterPlan.project_id == project_id, ChapterPlan.status == "accepted",
               ChapterPlan.chapter_number.between(band.chapter_start, band.chapter_end))
        .order_by(ChapterPlan.chapter_number)
    )
    summaries = []
    for plan in plans:
        draft = session.scalar(
            select(ChapterDraft).where(ChapterDraft.chapter_plan_id == plan.id)
            .order_by(ChapterDraft.version.desc(), ChapterDraft.created_at.desc()).limit(1)
        )
        summaries.append({"chapter_number": plan.chapter_number, "title": plan.title,
                          "summary": str(getattr(draft, "summary", "") or plan.one_line or "").strip()})
    return summaries


def _stalled_threads(session: Session, project_id: str, *, as_of: int, runtime: BookStateRuntime) -> list[dict[str, Any]]:
    """Active plot threads without a beat in the last few chapters, as the world digest lists them."""
    sampled = sample_active_threads(session=session, project_id=project_id, chapter_number=as_of,
                                    limit=_THREAD_LIMIT, stale_window=_STALE_THREAD_WINDOW, runtime=runtime)
    stalled = []
    for thread in sampled.threads:
        beat = sampled.latest_beats.get(thread.id)
        last_active = beat.chapter_number if beat is not None else thread.opened_at_chapter
        if as_of - last_active >= _STALE_THREAD_WINDOW:
            stalled.append({"name": thread.name, "description": thread.description,
                            "last_active_chapter": last_active, "priority": thread.priority})
    return stalled


__all__ = [
    "MINUTES_PER_DAY",
    "BandPayloadBudgetExceeded",
    "BandPlanInputs",
    "OfferedRefs",
    "PendingResponse",
    "band_calibration",
    "build_band_inputs",
    "previous_band",
]
