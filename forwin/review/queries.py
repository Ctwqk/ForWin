"""Draft/review queries shared by repair and Canon preparation."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.project import ChapterPlan


def latest_draft_and_review_for_chapter(
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
) -> tuple[ChapterDraft | None, ChapterReview | None]:
    chapter_plan = session.execute(
        select(ChapterPlan).where(
            ChapterPlan.project_id == project_id,
            ChapterPlan.chapter_number == chapter_number,
        )
    ).scalar_one_or_none()
    if chapter_plan is None:
        return None, None
    latest_draft = session.execute(
        select(ChapterDraft)
        .where(ChapterDraft.chapter_plan_id == chapter_plan.id)
        .order_by(ChapterDraft.version.desc(), ChapterDraft.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest_draft is None:
        return None, None
    latest_review = session.execute(
        select(ChapterReview)
        .where(ChapterReview.draft_id == latest_draft.id)
        .order_by(ChapterReview.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return latest_draft, latest_review
