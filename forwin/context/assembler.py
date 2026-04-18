"""Context assembler - builds ChapterContextPack from current state."""
from __future__ import annotations
import json
import logging

from forwin.protocol.context import (
    ArcEnvelopeView,
    AudienceHintView,
    ChapterContextPack,
    EntitySnapshot,
    NPCIntentView,
    RelationSnapshot,
    PlotThreadSnapshot,
    TimelineSnapshot,
    WorldPressureView,
)

logger = logging.getLogger(__name__)


def assemble_context(
    repo,  # StateRepository
    project_id: str,
    chapter_plan,  # ChapterPlan ORM object
) -> ChapterContextPack:
    """Build a ChapterContextPack for the writer.

    Args:
        repo: StateRepository instance
        project_id: current project ID
        chapter_plan: ChapterPlan ORM object with chapter_number, title, one_line, goals_json

    Returns:
        ChapterContextPack ready for the writer
    """
    # 1. Get project
    project = repo.get_project(project_id)

    # 2. Get active entities with latest states
    entities = repo.get_active_entities(project_id)

    # 3. Get active relations
    relations = repo.get_active_relations(project_id)

    # 4. Get active plot threads with recent beats
    threads = repo.get_active_threads(project_id)

    # 5. Get previous chapter summaries (last 3)
    summaries = repo.get_chapter_summaries(project_id, chapter_plan.chapter_number)

    # 6. Get current timeline
    timeline = repo.get_current_timeline(project_id)

    # 6.5 Get latest NPC intents / world pressure
    npc_intents_getter = getattr(repo, "get_recent_npc_intents", None)
    npc_intents = (
        npc_intents_getter(project_id, before_chapter=chapter_plan.chapter_number)
        if callable(npc_intents_getter)
        else []
    )
    world_pressure_getter = getattr(repo, "get_latest_world_pressure", None)
    world_pressure = (
        world_pressure_getter(project_id, before_chapter=chapter_plan.chapter_number)
        if callable(world_pressure_getter)
        else None
    )
    arc_envelope_getter = getattr(repo, "get_active_arc_envelope", None)
    arc_envelope_row = (
        arc_envelope_getter(project_id)
        if callable(arc_envelope_getter)
        else None
    )
    reader_promise_getter = getattr(repo, "get_reader_promise", None)
    reader_promise = (
        reader_promise_getter(project_id)
        if callable(reader_promise_getter)
        else None
    )
    arc_payoff_map_getter = getattr(repo, "get_arc_payoff_map", None)
    arc_payoff_map = (
        arc_payoff_map_getter(project_id)
        if callable(arc_payoff_map_getter)
        else None
    )
    band_schedule_getter = getattr(repo, "get_band_experience_plan_for_chapter", None)
    band_schedule = (
        band_schedule_getter(project_id, chapter_plan.chapter_number)
        if callable(band_schedule_getter)
        else None
    )
    chapter_experience_getter = getattr(repo, "get_chapter_experience_plan", None)
    chapter_experience_plan = (
        chapter_experience_getter(project_id, chapter_plan.chapter_number)
        if callable(chapter_experience_getter)
        else None
    )
    audience_hints_getter = getattr(repo, "get_audience_hints", None)
    audience_hints_raw = (
        audience_hints_getter(project_id, before_chapter=chapter_plan.chapter_number)
        if callable(audience_hints_getter)
        else None
    )
    chapter_task_contract_getter = getattr(repo, "get_chapter_task_contract", None)
    chapter_task_contract = (
        chapter_task_contract_getter(project_id, chapter_plan.chapter_number)
        if callable(chapter_task_contract_getter)
        else []
    )
    band_task_contract_getter = getattr(repo, "get_band_task_contract_for_chapter", None)
    band_task_contract = (
        band_task_contract_getter(project_id, chapter_plan.chapter_number)
        if callable(band_task_contract_getter)
        else []
    )
    constraints_enabled_getter = getattr(repo, "future_constraints_enabled", None)
    constraints_enabled = (
        bool(constraints_enabled_getter(project_id))
        if callable(constraints_enabled_getter)
        else True
    )
    active_constraints_getter = getattr(repo, "list_active_narrative_constraints", None)
    active_constraints = (
        active_constraints_getter(project_id, chapter_number=chapter_plan.chapter_number)
        if constraints_enabled and callable(active_constraints_getter)
        else []
    )
    next_band_summary_getter = getattr(repo, "get_next_band_summary", None)
    next_band_summary = (
        next_band_summary_getter(project_id, chapter_plan.chapter_number)
        if callable(next_band_summary_getter)
        else None
    )

    # 7. Parse chapter goals from goals_json
    try:
        goals = json.loads(chapter_plan.goals_json) if chapter_plan.goals_json else []
    except json.JSONDecodeError:
        goals = []

    # 8. Build and return pack
    return ChapterContextPack(
        project_id=project_id,
        project_title=project.title,
        premise=project.premise,
        genre=project.genre,
        setting_summary=project.setting_summary,
        chapter_number=chapter_plan.chapter_number,
        chapter_plan_title=chapter_plan.title,
        chapter_plan_one_line=chapter_plan.one_line,
        chapter_goals=goals,
        previous_chapter_summaries=summaries,
        active_entities=entities,
        active_relations=relations,
        active_threads=threads,
        timeline=timeline,
        npc_intents=npc_intents,
        world_pressure=world_pressure,
        reader_feedback=None,
        current_arc_envelope=(
            ArcEnvelopeView(
                source_policy_tier=arc_envelope_row.source_policy_tier,
                base_target_size=arc_envelope_row.base_target_size,
                base_soft_min=arc_envelope_row.base_soft_min,
                base_soft_max=arc_envelope_row.base_soft_max,
                resolved_target_size=arc_envelope_row.resolved_target_size,
                resolved_soft_min=arc_envelope_row.resolved_soft_min,
                resolved_soft_max=arc_envelope_row.resolved_soft_max,
                detailed_band_size=arc_envelope_row.detailed_band_size,
                frozen_zone_size=arc_envelope_row.frozen_zone_size,
                current_projected_size=arc_envelope_row.current_projected_size,
                current_confidence=arc_envelope_row.current_confidence,
            )
            if arc_envelope_row is not None
            else None
        ),
        audience_hints=(
            AudienceHintView(
                pacing_hints=audience_hints_raw.pacing_hints,
                clarity_hints=audience_hints_raw.clarity_hints,
                character_heat_changes=audience_hints_raw.character_heat_changes,
                risk_flags=audience_hints_raw.risk_flags,
            )
            if audience_hints_raw is not None
            else None
        ),
        reader_promise=reader_promise,
        arc_payoff_map=arc_payoff_map,
        band_delight_schedule=band_schedule,
        chapter_experience_plan=chapter_experience_plan,
        chapter_task_contract=chapter_task_contract,
        band_task_contract=band_task_contract,
        active_future_constraints=active_constraints,
        next_band_summary=next_band_summary,
    )
