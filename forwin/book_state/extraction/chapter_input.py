from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.book_state.book_frame_repository import BookLexiconRepository, StoryCalendarRepository
from forwin.book_state.knowledge import FACT_REF_PREFIX
from forwin.book_state.projection import BookStateProjection
from forwin.models.narrative_obligation import NarrativeObligationRow
from forwin.models.project import ChapterPlan


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


def _loads(raw: str | None, default: Any) -> Any:
    try:
        return json.loads(raw or "null") or default
    except (TypeError, json.JSONDecodeError):
        return default


def _fulfillment_targets(session: Session, project_id: str, plan: ChapterPlan | None) -> tuple[FulfillmentTarget, ...]:
    if plan is None:
        return ()
    goals = _loads(plan.goals_json, [])
    contract = _loads(plan.task_contract_json, {})
    experience = _loads(plan.experience_plan_json, {})
    targets = [
        FulfillmentTarget(f"goal:{index}", goal)
        for index, goal in enumerate(goals if isinstance(goals, list) else [], start=1)
        if isinstance(goal, str) and goal.strip()
    ]
    obligation_ids: list[str] = []
    # Existing obligation patches store this key in the experience plan.
    for payload in (contract, experience):
        refs = payload.get("obligations_to_resolve", []) if isinstance(payload, dict) else []
        if isinstance(refs, list):
            obligation_ids.extend(ref for ref in refs if isinstance(ref, str) and ref)
    if obligation_ids:
        obligations = {
            row.id: row for row in session.scalars(select(NarrativeObligationRow).where(
                NarrativeObligationRow.project_id == project_id,
                NarrativeObligationRow.id.in_(obligation_ids),
            ))
        }
        for ref in dict.fromkeys(obligation_ids):
            obligation = obligations.get(ref)
            targets.append(FulfillmentTarget(
                f"obligation:{ref}",
                (obligation.payoff_test or obligation.summary or ref) if obligation else ref,
            ))
    rewards = experience.get("planned_reward_tags", []) if isinstance(experience, dict) else []
    if isinstance(rewards, list):
        targets.extend(
            FulfillmentTarget(f"reward:{tag}", tag)
            for tag in dict.fromkeys(tag for tag in rewards if isinstance(tag, str) and tag)
        )
    return tuple(targets)


def build_chapter_extraction_input(
    session: Session, *, project_id: str, chapter_number: int,
) -> ChapterExtractionInput:
    runtime = BookStateProjection(session).load_runtime_as_of(
        project_id, as_of_chapter=chapter_number - 1,
    )
    calendar = StoryCalendarRepository(session).get(project_id)
    lexicon = BookLexiconRepository(session).get(project_id)
    plan = session.scalar(select(ChapterPlan).where(
        ChapterPlan.project_id == project_id,
        ChapterPlan.chapter_number == chapter_number,
    ))
    entities = []
    intents = []
    for node in sorted(runtime.world.nodes_by_id.values(), key=lambda node: node.id):
        if not node.is_active:
            continue
        if node.node_type in {"character", "faction", "organization", "group", "item", "location"}:
            entities.append(EntityRow(node.id, str(node.node_type), node.name, tuple(node.aliases)))
        if node.node_type == "intent":
            state = runtime.world.get_state(node.id)
            status = state.get("status", "")
            if status in {"planned", "due"}:
                intents.append(IntentRow(
                    node.id, str(node.profile.get("owner_ref", "")),
                    str(node.profile.get("summary") or node.summary or node.name), str(status),
                ))
    return ChapterExtractionInput(
        project_id=project_id,
        chapter_number=chapter_number,
        entities=tuple(entities),
        locations=tuple(
            LocationRow(node.id, node.name, node.region_id, node.subworld_id)
            for node in sorted(runtime.map.nodes_by_id.values(), key=lambda node: node.id)
        ),
        calendar_units=tuple(unit.name for unit in calendar.units) if calendar else (),
        time_aliases=tuple(alias.name for alias in lexicon.time_unit_aliases),
        fulfillment_targets=_fulfillment_targets(session, project_id, plan),
        intents=tuple(intents),
        secrets=tuple(
            SecretRow(f"{FACT_REF_PREFIX}{fact.id}", fact.proposition, fact.sensitivity_level)
            for fact in sorted(runtime.world.facts_by_id.values(), key=lambda fact: fact.id)
            if fact.sensitivity_level in {"hidden", "secret"}
        ),
        travel_modes=tuple(mode.id for mode in lexicon.travel_modes),
    )
