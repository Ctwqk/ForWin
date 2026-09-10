"""Compare an explicitly captured chapter-plan revision before replacing its overlay."""

from sqlalchemy import select
from sqlalchemy.orm import object_session

from forwin.candidate_drafts import candidate_plan_revision
from forwin.models.project import ChapterPlan


class PlanRevisionConflict(ValueError):
    pass


def require_clean_plan_inputs(session, chapters):
    for chapter in chapters:
        if (
            object_session(chapter) is not session
            or chapter in session.new
            or chapter in session.deleted
        ):
            raise PlanRevisionConflict(
                "chapter plan must be persisted in the owner session"
            )
        if session.is_modified(chapter):
            raise PlanRevisionConflict(
                "chapter plan has unflushed changes; preserve caller work"
            )


def lock_expected_plan(session, *, chapter_plan, expected_plan_revision):
    if not expected_plan_revision:
        raise PlanRevisionConflict("explicit expected plan revision required")
    require_clean_plan_inputs(session, [chapter_plan])
    # Lazy import avoids turning the persistence module into a Canon import cycle.
    from forwin.canon.projection_lock import lock_projection_project

    with session.no_autoflush:
        lock_projection_project(session, chapter_plan.project_id)
        current = session.scalar(
            select(ChapterPlan)
            .where(
                ChapterPlan.id == chapter_plan.id,
                ChapterPlan.project_id == chapter_plan.project_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    if current is None or candidate_plan_revision(current) != expected_plan_revision:
        raise PlanRevisionConflict("chapter plan revision changed before persistence")
    return current
