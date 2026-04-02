"""Context assembler - builds ChapterContextPack from current state."""
from __future__ import annotations
import json
import logging

from forwin.protocol.context import (
    ArcEnvelopeView,
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
    reader_feedback_getter = getattr(repo, "get_recent_reader_feedback", None)
    reader_feedback = (
        reader_feedback_getter(project_id, before_chapter=chapter_plan.chapter_number)
        if callable(reader_feedback_getter)
        else None
    )
    arc_envelope_getter = getattr(repo, "get_active_arc_envelope", None)
    arc_envelope_row = (
        arc_envelope_getter(project_id)
        if callable(arc_envelope_getter)
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
        reader_feedback=reader_feedback,
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
    )
