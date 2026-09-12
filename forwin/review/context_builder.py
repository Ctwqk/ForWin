from __future__ import annotations

import logging

from forwin.book_state.query import BookStateQuery
from forwin.map.repository import MapRepository
from forwin.protocol.context import ChapterContextPack, LintSignal, ReviewContextPack
from forwin.review.query import ReviewQuery
from forwin.retrieval.source_identity import CanonReadBaseline

logger = logging.getLogger(__name__)


def build_review_context_pack(
    *,
    repo=None,
    context: ChapterContextPack,
    lint_signals: list[LintSignal] | None = None,
    deterministic_quality_report: dict | None = None,
) -> ReviewContextPack:
    band = context.band_delight_schedule
    active_entities = list(context.active_entities)
    session = getattr(repo, "session", None) if repo is not None else None
    as_of_chapter = max(int(context.chapter_number) - 1, 0)
    baseline = getattr(context, "canon_read_baseline", None)
    if session is not None and baseline is None:
        baseline = CanonReadBaseline.capture(session, context.project_id, as_of_chapter=as_of_chapter)
    book_state = BookStateQuery(session, baseline=baseline) if session is not None else None
    review_query = ReviewQuery(session) if session is not None else None
    # A transported pack is not a reusable read certificate. With a repository,
    # reload cognition under the same fence as the other accepted review reads.
    accepted_cognition = (
        book_state.accepted_cognition(context.project_id, as_of_chapter=as_of_chapter)
        if book_state is not None else list(context.accepted_cognition)
    )
    active_rules = (
        book_state.active_entities(
            context.project_id,
            as_of_chapter=as_of_chapter,
            kinds={"rule"},
        )
        if book_state is not None
        else []
    )
    active_threads = list(context.active_threads)
    recent_canon_events = (
        book_state.recent_events(
            context.project_id,
            before_chapter=context.chapter_number,
            entity_names=[item.name for item in active_entities],
            thread_names=[item.name for item in active_threads],
            limit=5,
        )
        if book_state is not None
        else []
    )
    recent_rule_events = (
        book_state.recent_events(
            context.project_id,
            before_chapter=context.chapter_number,
            entity_names=[item.name for item in active_rules],
            thread_names=[],
            limit=5,
        )
        if book_state is not None and active_rules
        else []
    )
    recent_review_notes = (
        review_query.recent_notes(
            context.project_id,
            before_chapter=context.chapter_number,
            band_start=band.chapter_start if band is not None else None,
            band_end=band.chapter_end if band is not None else None,
            limit=5,
        )
        if review_query is not None
        else []
    )
    map_context = dict(context.map_context)
    map_context.update(_build_reviewer_only_map_context(repo=repo, context=context))
    canon_quality_context = getattr(context, "canon_quality_context", {}) or {}
    active_narrative_obligations = getattr(context, "active_narrative_obligations", []) or []
    if not active_narrative_obligations and isinstance(canon_quality_context, dict):
        active_narrative_obligations = [
            item
            for item in canon_quality_context.get("active_narrative_obligations", [])
            if isinstance(item, dict)
        ]
    future_plan_audit_summary = getattr(context, "future_plan_audit_summary", {}) or {}
    if not future_plan_audit_summary and isinstance(canon_quality_context, dict):
        future_plan_audit_summary = canon_quality_context.get("future_plan_audit_summary", {}) or {}
    canon_invariants = []
    if isinstance(canon_quality_context, dict):
        canon_invariants = [
            item
            for item in canon_quality_context.get("invariant_constraints", []) or []
            if isinstance(item, dict)
        ]
    return ReviewContextPack(
        project_id=context.project_id,
        project_title=context.project_title,
        chapter_number=context.chapter_number,
        chapter_plan_title=context.chapter_plan_title,
        chapter_plan_one_line=context.chapter_plan_one_line,
        chapter_goals=list(context.chapter_goals),
        previous_chapter_summaries=list(context.previous_chapter_summaries),
        genesis_context_refs=dict(context.genesis_context_refs),
        genesis_world_overview=context.genesis_world_overview,
        genesis_map_overview=context.genesis_map_overview,
        genesis_story_engine_summary=context.genesis_story_engine_summary,
        genesis_reference_facts=list(context.genesis_reference_facts),
        genesis_reference_omitted_count=context.genesis_reference_omitted_count,
        must_not_reveal=list(context.must_not_reveal),
        planned_reveal_ladder=list(context.planned_reveal_ladder),
        accepted_cognition=accepted_cognition,
        planned_reader_cognition_state=context.planned_reader_cognition_state,
        character_cognition_states=dict(context.character_cognition_states),
        observer_visibility_states=dict(context.observer_visibility_states),
        fair_misdirection_requirements=list(context.fair_misdirection_requirements),
        chapter_world_delta_intent=context.chapter_world_delta_intent,
        active_entities=active_entities,
        active_rules=active_rules,
        active_threads=active_threads,
        timeline=context.timeline,
        world_pressure=context.world_pressure,
        reader_feedback=None,
        audience_hints=None,
        reader_promise=context.reader_promise,
        arc_payoff_map=context.arc_payoff_map,
        band_delight_schedule=band,
        band_task_contract=list(context.band_task_contract),
        chapter_experience_plan=context.chapter_experience_plan,
        chapter_task_contract=list(context.chapter_task_contract),
        active_future_constraints=list(context.active_future_constraints),
        next_band_summary=context.next_band_summary,
        world_context=context.world_context,
        map_context=map_context,
        recent_canon_events=recent_canon_events,
        recent_rule_events=recent_rule_events,
        recent_review_notes=recent_review_notes,
        lint_signals=list(lint_signals or []),
        active_personality_contexts=list(context.active_personality_contexts),
        deterministic_quality_report=dict(deterministic_quality_report or {}),
        canon_invariants=canon_invariants,
        active_narrative_obligations=list(active_narrative_obligations),
        future_plan_audit_summary=dict(future_plan_audit_summary) if isinstance(future_plan_audit_summary, dict) else {},
    )


def _build_reviewer_only_map_context(*, repo, context: ChapterContextPack) -> dict:
    session = getattr(repo, "session", None) if repo is not None else None
    if session is None:
        return {}
    payload: dict = {}
    try:
        map_repo = MapRepository(session)
        nodes = map_repo.list_map_nodes(context.project_id)
        edges = map_repo.list_map_edges(context.project_id)
        if nodes or edges:
            payload["objective_review_graph"] = {
                "available": True,
                "node_count": len(nodes),
                "edge_count": len(edges),
                "map_nodes": [node.model_dump(mode="json") for node in nodes],
                "map_edges": [edge.model_dump(mode="json") for edge in edges],
            }
    except Exception:
        logger.warning("Failed to build reviewer objective map context.", exc_info=True)
    return payload
