from __future__ import annotations

from forwin.protocol.context import ChapterContextPack, LintSignal, ReviewContextPack


def build_review_context_pack(
    *,
    repo=None,
    context: ChapterContextPack,
    lint_signals: list[LintSignal] | None = None,
) -> ReviewContextPack:
    band = context.band_delight_schedule
    active_entities = list(context.active_entities)
    active_rules = (
        repo.get_active_rule_entities(context.project_id)
        if repo is not None and hasattr(repo, "get_active_rule_entities")
        else []
    )
    active_threads = list(context.active_threads)
    recent_canon_events = (
        repo.get_recent_canon_events(
            context.project_id,
            before_chapter=context.chapter_number,
            entity_names=[item.name for item in active_entities],
            thread_names=[item.name for item in active_threads],
            limit=5,
        )
        if repo is not None and hasattr(repo, "get_recent_canon_events")
        else []
    )
    recent_rule_events = (
        repo.get_recent_canon_events(
            context.project_id,
            before_chapter=context.chapter_number,
            entity_names=[item.name for item in active_rules],
            thread_names=[],
            limit=5,
        )
        if repo is not None and hasattr(repo, "get_recent_canon_events") and active_rules
        else []
    )
    recent_review_notes = (
        repo.get_recent_review_notes(
            context.project_id,
            before_chapter=context.chapter_number,
            band_start=band.chapter_start if band is not None else None,
            band_end=band.chapter_end if band is not None else None,
            limit=5,
        )
        if repo is not None and hasattr(repo, "get_recent_review_notes")
        else []
    )
    reader_feedback = context.reader_feedback
    if (
        reader_feedback is None
        and repo is not None
        and hasattr(repo, "get_recent_reader_feedback")
    ):
        reader_feedback = repo.get_recent_reader_feedback(
            context.project_id,
            before_chapter=context.chapter_number,
        )
    return ReviewContextPack(
        project_id=context.project_id,
        project_title=context.project_title,
        chapter_number=context.chapter_number,
        chapter_plan_title=context.chapter_plan_title,
        chapter_plan_one_line=context.chapter_plan_one_line,
        chapter_goals=list(context.chapter_goals),
        previous_chapter_summaries=list(context.previous_chapter_summaries),
        active_entities=active_entities,
        active_rules=active_rules,
        active_threads=active_threads,
        timeline=context.timeline,
        world_pressure=context.world_pressure,
        reader_feedback=reader_feedback,
        audience_hints=context.audience_hints,
        reader_promise=context.reader_promise,
        arc_payoff_map=context.arc_payoff_map,
        band_delight_schedule=band,
        band_task_contract=list(context.band_task_contract),
        chapter_experience_plan=context.chapter_experience_plan,
        chapter_task_contract=list(context.chapter_task_contract),
        active_future_constraints=list(context.active_future_constraints),
        next_band_summary=context.next_band_summary,
        recent_canon_events=recent_canon_events,
        recent_rule_events=recent_rule_events,
        recent_review_notes=recent_review_notes,
        lint_signals=list(lint_signals or []),
    )
