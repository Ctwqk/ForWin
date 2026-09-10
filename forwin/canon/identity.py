"""Current acceptance is selected by the stable chapter's pointer, never recency."""

from sqlalchemy import select

from forwin.models.canon import CanonCommitRecord
from forwin.models.project import ChapterPlan


def active_commit_predicate():
    return CanonCommitRecord.id.in_(
        select(ChapterPlan.active_commit_id).where(
            ChapterPlan.active_commit_id.is_not(None)
        )
    )


def is_active_commit(session, commit: CanonCommitRecord) -> bool:
    chapter = session.get(ChapterPlan, commit.chapter_plan_id)
    return bool(
        chapter
        and chapter.active_commit_id == commit.id
        and chapter.project_id == commit.project_id
        and chapter.chapter_number == commit.chapter_number
    )


def effective_snapshot_predicate(snapshot_id, commit_snapshot_column):
    """Unowned (Genesis/world-edit/provisional) or active acceptance snapshots."""
    owned = select(commit_snapshot_column).where(commit_snapshot_column != "")
    effective = owned.where(active_commit_predicate())
    return snapshot_id.not_in(owned) | snapshot_id.in_(effective)
