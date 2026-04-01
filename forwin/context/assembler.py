"""Context assembler - builds ChapterContextPack from current state."""
from __future__ import annotations
import json
import logging

from forwin.protocol.context import (
    ChapterContextPack,
    EntitySnapshot,
    RelationSnapshot,
    PlotThreadSnapshot,
    TimelineSnapshot,
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

    # 7. Parse chapter goals from goals_json
    try:
        goals = json.loads(chapter_plan.goals_json) if chapter_plan.goals_json else []
    except json.JSONDecodeError:
        goals = []

    # 8. Build and return pack
    return ChapterContextPack(
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
    )
