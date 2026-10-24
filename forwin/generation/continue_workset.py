from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forwin.models.project import ArcPlanVersion, ChapterPlan, Project


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContinueWorkset:
    """The chapters a continue run writes, in order, and why the list is what it is.

    ``reason`` is ``chapters_pending`` with chapters to write; otherwise ``project_completed``,
    ``pending_review_blocker``, ``pending_acceptance_blocker`` or ``project_not_found``.
    """

    chapter_numbers: tuple[int, ...]
    reason: str

    @property
    def requested_chapters(self) -> int:
        return len(self.chapter_numbers)


def build_continue_generation_workset(
    session: Session,
    project_id: str,
    *,
    max_chapters: int | None = None,
    resume_from_chapter: int | None = None,
    preloaded_plans: Sequence[ChapterPlan] | None = None,
) -> ContinueWorkset:
    """The book's unaccepted chapters in order, up to its last writable chapter.

    Whether a chapter has a plan row does not matter: run control plans each band just in time. A chapter
    waiting for review or acceptance blocks the run. ``resume_from_chapter`` is the lowest chapter to list;
    ``preloaded_plans`` are the project's plan rows when the caller has them.
    """
    if max_chapters is not None and int(max_chapters) < 1:
        raise ValueError("max_chapters must be positive when provided")
    project = session.get(Project, project_id)
    if project is None:
        return ContinueWorkset((), "project_not_found")
    if str(project.creation_status or "") == "completed":
        return ContinueWorkset((), "project_completed")
    plans = preloaded_plans if preloaded_plans is not None else session.scalars(
        select(ChapterPlan).where(ChapterPlan.project_id == project_id)
    ).all()
    statuses = {int(plan.chapter_number or 0): str(plan.status or "") for plan in plans}
    if "needs_review" in statuses.values():
        return ContinueWorkset((), "pending_review_blocker")
    if "drafted" in statuses.values():
        return ContinueWorkset((), "pending_acceptance_blocker")
    book = writable_chapter_ranges(session, [project_id]).get(project_id, range(0))
    first = max(book.start, int(resume_from_chapter or 0))
    while statuses.get(first) == "accepted":
        first += 1
    numbers = tuple(number for number in range(first, book.stop) if statuses.get(number) != "accepted")
    if max_chapters is not None:
        numbers = numbers[: int(max_chapters)]
    return ContinueWorkset(numbers, "chapters_pending" if numbers else "project_completed")


def writable_chapter_ranges(session: Session, project_ids: Sequence[str]) -> dict[str, range]:
    """Each project's chapters to write, from its first arc's ``chapter_start`` to
    ``min(target_total_chapters, its arcs' furthest chapter_end)``.

    No arc holds a chapter outside the range, so none could be planned. Superseded arcs do not count; a
    ``target_total_chapters`` of 0 leaves the arcs uncapped. A project whose arcs give an empty range
    (``chapter_end < chapter_start``, e.g. older rows with the default ``chapter_end = 0``) has nothing to write and is logged; the
    deploy preflight lists such projects for repair.
    """
    if not project_ids:
        return {}
    rows = session.execute(
        select(
            ArcPlanVersion.project_id,
            func.min(ArcPlanVersion.chapter_start),
            func.max(ArcPlanVersion.chapter_end),
            Project.target_total_chapters,
        )
        .join(Project, Project.id == ArcPlanVersion.project_id)
        .where(ArcPlanVersion.project_id.in_(list(project_ids)), ArcPlanVersion.status != "superseded")
        .group_by(ArcPlanVersion.project_id, Project.target_total_chapters)
    ).all()
    result: dict[str, range] = {}
    for project_id, arc_start, arc_end, target in rows:
        last = int(arc_end or 0)
        if int(target or 0) > 0:
            last = min(last, int(target))
        result[str(project_id)] = range(max(1, int(arc_start or 0)), last + 1)
    for project_id, chapters in result.items():
        if not chapters:
            logger.warning("project %s has an empty arc chapter range; nothing is writable until it is repaired",
                           project_id)
    return result


__all__ = ["ContinueWorkset", "build_continue_generation_workset", "writable_chapter_ranges"]
