from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, get_args

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.book_state.book_frame_repository import BookLexiconRepository, StoryCalendarRepository
from forwin.book_state.knowledge import FACT_REF_PREFIX
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.runtime import BookStateRuntime, require_runtime_as_of
from forwin.models.narrative_obligation import NarrativeObligationRow
from forwin.models.project import ChapterPlan, Project
from forwin.narrative_obligations.repository import NarrativeObligationRepository, _CLOSED_STATUSES
from forwin.narrative_obligations.types import NarrativeObligation
from forwin.protocol.experience import RewardTag
from forwin.runtime.policy import WriterPolicy
from forwin.runtime.policy_store import ProjectPolicyMissing, ProjectPolicyStore
from forwin.world_view.builder import build_chapter_world_view
from forwin.world_view.types import ChapterWorldView


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EntityRow:
    id: str
    kind: str
    name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class LocationRow:
    id: str
    name: str
    region_id: str
    subworld_id: str


@dataclass(frozen=True)
class FulfillmentTarget:
    target_id: str
    description: str


@dataclass(frozen=True)
class IntentRow:
    id: str
    owner_id: str
    summary: str
    status: str


@dataclass(frozen=True)
class SecretRow:
    fact_ref: str
    proposition: str
    sensitivity: str


@dataclass(frozen=True)
class ChapterExtractionInput:
    project_id: str
    chapter_number: int
    entities: tuple[EntityRow, ...]
    locations: tuple[LocationRow, ...]
    calendar_units: tuple[str, ...]
    time_aliases: tuple[str, ...]
    fulfillment_targets: tuple[FulfillmentTarget, ...]
    intents: tuple[IntentRow, ...]
    secrets: tuple[SecretRow, ...]
    travel_modes: tuple[str, ...]
    scene_location_hints: tuple[str, ...] = ()
    # Validation-only ids from the same runtime; never rendered into the extraction prompt.
    runtime_fact_refs: tuple[str, ...] = ()


def _loads(raw: str | None, default: Any) -> Any:
    try:
        return json.loads(raw or "null") or default
    except (TypeError, json.JSONDecodeError):
        return default


def plan_goal_targets(goals: Any) -> list[tuple[str, str]]:
    """Keep the original goal positions even when invalid entries are skipped."""
    return [
        (f"goal:{index}", goal)
        for index, goal in enumerate(goals if isinstance(goals, list) else [], start=1)
        if isinstance(goal, str) and goal.strip()
    ]


def plan_binding_targets(plan: ChapterPlan | None) -> list[tuple[str, str]]:
    bindings = _loads(plan.world_bindings_json, []) if plan is not None else []
    return [
        (f"bind:{index}", json.dumps(binding, ensure_ascii=False, sort_keys=True))
        for index, binding in enumerate(bindings if isinstance(bindings, list) else [])
        if isinstance(binding, dict)
    ]


def _fulfillment_targets(
    session: Session, project_id: str, plan: ChapterPlan | None,
    *, judged_obligations: Sequence[NarrativeObligation] = (),
) -> tuple[FulfillmentTarget, ...]:
    goals = _loads(plan.goals_json, []) if plan else []
    experience = _loads(plan.experience_plan_json, {}) if plan else {}
    targets = [
        FulfillmentTarget(target_id, goal)
        for target_id, goal in plan_goal_targets(goals)
    ]
    targets.extend(FulfillmentTarget(*target) for target in plan_binding_targets(plan))
    if plan is not None:
        for prefix, raw in (("reveal", plan.reveal_directives_json), ("hint", plan.hint_directives_json)):
            directives = _loads(raw, [])
            for directive in directives if isinstance(directives, list) else []:
                if isinstance(directive, dict) and directive.get("id"):
                    targets.append(FulfillmentTarget(
                        f"{prefix}:{directive['id']}",
                        json.dumps(directive, ensure_ascii=False, sort_keys=True),
                    ))
    obligation_ids: list[str] = []
    refs = experience.get("obligations_to_resolve", []) if isinstance(experience, dict) else []
    if isinstance(refs, list):
        obligation_ids.extend(ref for ref in refs if isinstance(ref, str) and ref)
    if obligation_ids:
        obligations = {
            row.id: row for row in session.scalars(select(NarrativeObligationRow).where(
                NarrativeObligationRow.project_id == project_id,
                NarrativeObligationRow.id.in_(obligation_ids),
                NarrativeObligationRow.status.not_in(_CLOSED_STATUSES),
            ))
        }
        for ref in dict.fromkeys(obligation_ids):
            obligation = obligations.get(ref)
            if obligation is None:
                continue
            targets.append(FulfillmentTarget(
                f"obligation:{ref}",
                obligation.payoff_test or obligation.summary or ref,
            ))
    offered_ids = set(obligation_ids)
    for obligation in judged_obligations:
        if obligation.status in _CLOSED_STATUSES or obligation.id in offered_ids:
            continue
        offered_ids.add(obligation.id)
        targets.append(FulfillmentTarget(
            f"obligation:{obligation.id}", obligation.payoff_test or obligation.summary or obligation.id,
        ))
    if plan is None:
        return tuple(targets)
    rewards = experience.get("planned_reward_tags", []) if isinstance(experience, dict) else []
    planned = list(dict.fromkeys(
        tag for tag in (rewards if isinstance(rewards, list) else []) if isinstance(tag, str) and tag
    ))
    # Every reward tag is a target so an unplanned payoff the chapter delivers still counts;
    # the planned ones come first.
    targets.extend(FulfillmentTarget(f"reward:{tag}", f"{tag} (planned)") for tag in planned)
    targets.extend(
        FulfillmentTarget(f"reward:{tag}", f"{tag} (optional)")
        for tag in get_args(RewardTag)
        if tag not in planned
    )
    return tuple(targets)


def build_chapter_extraction_input(
    session: Session, *, project_id: str, chapter_number: int,
    scene_location_hints: Sequence[str] = (),
    runtime: BookStateRuntime | None = None,
) -> ChapterExtractionInput:
    if runtime is None:
        runtime = BookStateProjection(session).load_runtime_as_of(
            project_id, as_of_chapter=chapter_number - 1,
        )
    else:
        require_runtime_as_of(runtime, project_id, chapter_number - 1)
    calendar = StoryCalendarRepository(session).get(project_id)
    lexicon = BookLexiconRepository(session).get(project_id)
    plan = session.scalar(select(ChapterPlan).where(
        ChapterPlan.project_id == project_id,
        ChapterPlan.chapter_number == chapter_number,
    ))
    view = ChapterWorldView()
    if plan is not None:
        budget_chars = WriterPolicy().world_view_budget_chars
        project = session.get(Project, project_id)
        if project is not None:
            try:
                budget_chars = ProjectPolicyStore(session).load(project).policy.writer.world_view_budget_chars
            except ProjectPolicyMissing:
                pass
        view = build_chapter_world_view(
            session, project_id=project_id, chapter_plan=plan, budget_chars=budget_chars, runtime=runtime,
        )
    obligation_repo = NarrativeObligationRepository(session)
    judged_obligations = [
        # Includes the gate/acceptance selection and current-origin obligations
        # checked for expiry with chapter_number + 1 after acceptance.
        *obligation_repo.list_active_for_context(project_id, chapter_number=chapter_number + 1),
        *obligation_repo.list_planned_for_chapter(project_id, origin_chapter_number=chapter_number),
    ]
    entities = {}
    for entity in [*view.onstage, *view.known_entities]:
        node = runtime.world.get_node(entity.ref)
        if node is not None and node.is_active:
            entities[node.id] = EntityRow(
                node.id, str(node.node_type), entity.name or getattr(entity, "public_identity", ""),
                tuple(getattr(entity, "aliases", node.aliases)) if entity.name else (),
            )
    locations = {location.id: location for location in view.locations}
    if view.pov_location is not None:
        locations.setdefault(view.pov_location.id, view.pov_location)
    secret_refs = {topic.fact_ref for topic in view.forbidden_topics}
    secret_refs.update(fact.fact_ref for fact in view.pov_knowledge if fact.pov_only_secret)
    if plan is not None:
        for raw in (plan.reveal_directives_json, plan.hint_directives_json):
            items = _loads(raw, [])
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict) and isinstance(ref := item.get("fact_ref"), str):
                    secret_refs.add(f"{FACT_REF_PREFIX}{ref.strip().removeprefix(FACT_REF_PREFIX)}")
        bindings = _loads(plan.world_bindings_json, [])
        for binding in bindings if isinstance(bindings, list) else []:
            if (isinstance(binding, dict) and binding.get("kind") == "secret"
                    and isinstance(ref := binding.get("ref"), str)):
                secret_refs.add(f"{FACT_REF_PREFIX}{ref.strip().removeprefix(FACT_REF_PREFIX)}")
    intents = []
    for node in sorted(runtime.world.nodes_by_id.values(), key=lambda node: node.id):
        if not node.is_active:
            continue
        if node.node_type == "intent":
            state = runtime.world.get_state(node.id)
            status = state.get("status", "")
            if status in {"planned", "due"}:
                intents.append(IntentRow(
                    node.id, str(node.profile.get("owner_ref", "")),
                    str(node.profile.get("summary") or node.summary or node.name), str(status),
                ))
    location_hints = []
    for hint in scene_location_hints:
        if isinstance(hint, str) and (
            hint in runtime.map.nodes_by_id or (hint.startswith("new:") and hint[4:].strip())
        ):
            location_hints.append(hint)
        else:
            logger.warning("Dropping invalid scene_location_hint %r for project %s", hint, project_id)
    return ChapterExtractionInput(
        project_id=project_id,
        chapter_number=chapter_number,
        entities=tuple(entities[ref] for ref in sorted(entities)),
        locations=tuple(
            LocationRow(ref, locations[ref].name, node.region_id, node.subworld_id)
            for ref in sorted(locations)
            if (node := runtime.map.nodes_by_id.get(ref)) is not None
        ),
        calendar_units=tuple(unit.name for unit in calendar.units) if calendar else (),
        time_aliases=tuple(alias.name for alias in lexicon.time_unit_aliases),
        fulfillment_targets=_fulfillment_targets(session, project_id, plan, judged_obligations=judged_obligations),
        intents=tuple(intents),
        secrets=tuple(
            SecretRow(f"{FACT_REF_PREFIX}{fact.id}", fact.proposition, fact.sensitivity_level)
            for fact in sorted(runtime.world.facts_by_id.values(), key=lambda fact: fact.id)
            if f"{FACT_REF_PREFIX}{fact.id}" in secret_refs
        ),
        travel_modes=tuple(mode.id for mode in lexicon.travel_modes),
        scene_location_hints=tuple(location_hints),
        runtime_fact_refs=tuple(f"{FACT_REF_PREFIX}{ref}" for ref in sorted(runtime.world.facts_by_id)),
    )
