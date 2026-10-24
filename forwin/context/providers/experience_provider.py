from __future__ import annotations

import json
import logging

from forwin.context.request import ContextDraft, ContextRequest
from forwin.experience.projection import writer_payoff_map
from forwin.knowledge_system.context import KnowledgeContextQuery
from forwin.protocol.world_model import WorldContextPack

logger = logging.getLogger(__name__)


class ExperienceContextProvider:
    name = "experience"

    def contribute(self, request: ContextRequest, draft: ContextDraft) -> None:
        repo = request.repo
        chapter_plan = request.chapter_plan
        arc_plan_id = getattr(chapter_plan, "arc_plan_id", None)
        project_id = request.project_id
        reader_promise_getter = getattr(repo, "get_reader_promise", None)
        arc_payoff_map_getter = getattr(repo, "get_arc_payoff_map", None)
        setting_brief_getter = getattr(repo, "get_arc_setting_brief", None)
        band_schedule_getter = getattr(
            repo, "get_band_experience_plan_for_chapter", None
        )
        chapter_experience_getter = getattr(repo, "get_chapter_experience_plan", None)
        chapter_task_contract_getter = getattr(repo, "get_chapter_task_contract", None)
        band_task_contract_getter = getattr(
            repo, "get_band_task_contract_for_chapter", None
        )
        constraints_enabled_getter = getattr(repo, "future_constraints_enabled", None)
        constraints_enabled = (
            bool(constraints_enabled_getter(project_id))
            if callable(constraints_enabled_getter)
            else True
        )
        active_constraints_getter = getattr(
            repo, "list_active_narrative_constraints", None
        )
        next_band_summary_getter = getattr(repo, "get_next_band_summary", None)

        chapter_experience_plan = (
            chapter_experience_getter(project_id, chapter_plan.chapter_number)
            if callable(chapter_experience_getter)
            else None
        )
        try:
            goals = (
                json.loads(chapter_plan.goals_json) if chapter_plan.goals_json else []
            )
        except json.JSONDecodeError:
            goals = []

        repo_session = getattr(repo, "session", None)
        world_context = WorldContextPack()
        if repo_session is not None:
            try:
                query_terms = [
                    chapter_plan.title,
                    chapter_plan.one_line,
                    *goals,
                    *(entity.name for entity in draft.data.get("entities", [])[:8]),
                    *(thread.name for thread in draft.data.get("threads", [])[:4]),
                ]
                world_context = KnowledgeContextQuery(repo_session).build(
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                    query_terms=query_terms,
                    max_pages=6,
                )
            except Exception:
                logger.warning("Failed to assemble world model context.", exc_info=True)

        draft.data.update(
            {
                "goals": goals,
                "reader_promise": reader_promise_getter(project_id, arc_plan_id=arc_plan_id)
                if callable(reader_promise_getter)
                else None,
                "arc_payoff_map": writer_payoff_map(
                    arc_payoff_map_getter(project_id, arc_plan_id=arc_plan_id), chapter_plan.chapter_number,
                )
                if callable(arc_payoff_map_getter)
                else None,
                "arc_setting_brief": setting_brief_getter(project_id, arc_plan_id=arc_plan_id)
                if callable(setting_brief_getter)
                else "",
                "band_schedule": (
                    band_schedule_getter(project_id, chapter_plan.chapter_number)
                    if callable(band_schedule_getter)
                    else None
                ),
                "chapter_experience_plan": chapter_experience_plan,
                "chapter_task_contract": (
                    chapter_task_contract_getter(
                        project_id, chapter_plan.chapter_number
                    )
                    if callable(chapter_task_contract_getter)
                    else []
                ),
                "band_task_contract": (
                    band_task_contract_getter(project_id, chapter_plan.chapter_number)
                    if callable(band_task_contract_getter)
                    else []
                ),
                "active_constraints": (
                    active_constraints_getter(
                        project_id, chapter_number=chapter_plan.chapter_number
                    )
                    if constraints_enabled and callable(active_constraints_getter)
                    else []
                ),
                "next_band_summary": (
                    next_band_summary_getter(project_id, chapter_plan.chapter_number)
                    if callable(next_band_summary_getter)
                    else None
                ),
                "world_context": world_context,
            }
        )
