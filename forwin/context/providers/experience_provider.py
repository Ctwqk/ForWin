from __future__ import annotations

import json
import logging

from forwin.context.request import ContextDraft, ContextRequest
from forwin.planning.world_contracts import WorldContractRepository
from forwin.protocol.context import ArcEnvelopeView
from forwin.protocol.world_model import WorldContextPack
from forwin.world_model.retriever import WorldModelRetriever

logger = logging.getLogger(__name__)


class ExperienceContextProvider:
    name = "experience"

    def contribute(self, request: ContextRequest, draft: ContextDraft) -> None:
        repo = request.repo
        chapter_plan = request.chapter_plan
        project_id = request.project_id
        arc_envelope_getter = getattr(repo, "get_active_arc_envelope", None)
        reader_promise_getter = getattr(repo, "get_reader_promise", None)
        arc_payoff_map_getter = getattr(repo, "get_arc_payoff_map", None)
        band_schedule_getter = getattr(repo, "get_band_experience_plan_for_chapter", None)
        chapter_experience_getter = getattr(repo, "get_chapter_experience_plan", None)
        chapter_task_contract_getter = getattr(repo, "get_chapter_task_contract", None)
        band_task_contract_getter = getattr(repo, "get_band_task_contract_for_chapter", None)
        constraints_enabled_getter = getattr(repo, "future_constraints_enabled", None)
        constraints_enabled = (
            bool(constraints_enabled_getter(project_id))
            if callable(constraints_enabled_getter)
            else True
        )
        active_constraints_getter = getattr(repo, "list_active_narrative_constraints", None)
        next_band_summary_getter = getattr(repo, "get_next_band_summary", None)

        arc_envelope_row = arc_envelope_getter(project_id) if callable(arc_envelope_getter) else None
        chapter_experience_plan = (
            chapter_experience_getter(project_id, chapter_plan.chapter_number)
            if callable(chapter_experience_getter)
            else None
        )
        try:
            goals = json.loads(chapter_plan.goals_json) if chapter_plan.goals_json else []
        except json.JSONDecodeError:
            goals = []

        repo_session = getattr(repo, "session", None)
        chapter_world_delta_intent = None
        arc_world_contract = None
        band_world_contract = None
        if repo_session is not None:
            world_contract_repo = WorldContractRepository(repo_session)
            arc_world_contract = world_contract_repo.get_arc_contract(
                project_id,
                chapter_plan.arc_plan_id,
            )
            band_world_contract = world_contract_repo.get_band_contract_for_chapter(
                project_id,
                chapter_plan.chapter_number,
            )
            chapter_world_delta_intent = world_contract_repo.get_chapter_intent(
                project_id,
                chapter_plan.chapter_number,
            )

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
                world_context = WorldModelRetriever(repo_session).build_context(
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
                "arc_envelope_row": arc_envelope_row,
                "current_arc_envelope": (
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
                "reader_promise": reader_promise_getter(project_id) if callable(reader_promise_getter) else None,
                "arc_payoff_map": arc_payoff_map_getter(project_id) if callable(arc_payoff_map_getter) else None,
                "band_schedule": (
                    band_schedule_getter(project_id, chapter_plan.chapter_number)
                    if callable(band_schedule_getter)
                    else None
                ),
                "chapter_experience_plan": chapter_experience_plan,
                "chapter_task_contract": (
                    chapter_task_contract_getter(project_id, chapter_plan.chapter_number)
                    if callable(chapter_task_contract_getter)
                    else []
                ),
                "band_task_contract": (
                    band_task_contract_getter(project_id, chapter_plan.chapter_number)
                    if callable(band_task_contract_getter)
                    else []
                ),
                "active_constraints": (
                    active_constraints_getter(project_id, chapter_number=chapter_plan.chapter_number)
                    if constraints_enabled and callable(active_constraints_getter)
                    else []
                ),
                "next_band_summary": (
                    next_band_summary_getter(project_id, chapter_plan.chapter_number)
                    if callable(next_band_summary_getter)
                    else None
                ),
                "world_context": world_context,
                "arc_world_contract": arc_world_contract,
                "band_world_contract": band_world_contract,
                "chapter_world_delta_intent": chapter_world_delta_intent,
            }
        )
