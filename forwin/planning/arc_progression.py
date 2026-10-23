"""Move a book on to its next arc when the active one is written to its end, then give that arc its beat sheet."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.project import ArcPlanVersion, Project
from forwin.planning.beat_sheet.service import ArcBeatSheetService
from forwin.runtime.policy import RuntimePolicy


class ArcProgressionService:
    """Arc activation for run control and the post-Canon plan step, which may call it for the same chapter at once.

    The arc flip runs under the project row lock and is committed before the beat sheet's model call; the beat
    sheet is committed too, so the caller holds no lock afterwards. Both commits commit the caller's session.
    """

    def __init__(self, beat_sheets: ArcBeatSheetService) -> None:
        self.beat_sheets = beat_sheets

    def activate_next_arc_if_needed(
        self, session: Session, *, project_id: str, after_chapter: int, policy: RuntimePolicy,
    ) -> ArcPlanVersion | None:
        """The arc that ``after_chapter + 1`` opens, active and with a beat sheet; None when no arc opens there.

        When ``after_chapter`` ends the active arc, that arc becomes completed and the planned arc with the lowest
        ``chapter_start`` becomes active. An arc a concurrent caller already activated is returned the same way,
        its beat sheet ensured, so a second call changes nothing. The arc's first band is not planned here.
        """
        arc = self._activate(session, project_id=project_id, after_chapter=after_chapter)
        session.commit()
        if arc is None:
            return None
        self.beat_sheets.ensure_for_arc(session, project_id=project_id, arc_plan=arc, policy=policy)
        session.commit()
        return arc

    def ensure_beat_sheet(
        self, session: Session, *, project_id: str, chapter_number: int, policy: RuntimePolicy,
    ) -> ArcPlanVersion | None:
        """The active arc holding the chapter, with a beat sheet; None when no active arc holds it.

        An arc activated before beat sheets existed, or by an activation that stopped between its flip and the
        sheet, gets its sheet here. Commits the caller's session.
        """
        arc = session.scalar(
            select(ArcPlanVersion)
            .where(
                ArcPlanVersion.project_id == project_id, ArcPlanVersion.status == "active",
                ArcPlanVersion.chapter_start <= chapter_number, ArcPlanVersion.chapter_end >= chapter_number,
            )
            .order_by(ArcPlanVersion.chapter_start.desc(), ArcPlanVersion.id)
            .limit(1)
        )
        if arc is None:
            return None
        self.beat_sheets.ensure_for_arc(session, project_id=project_id, arc_plan=arc, policy=policy)
        session.commit()
        return arc

    @staticmethod
    def _activate(session: Session, *, project_id: str, after_chapter: int) -> ArcPlanVersion | None:
        session.execute(select(Project).where(Project.id == project_id).with_for_update(key_share=True)).scalar_one()
        arcs = list(session.scalars(
            select(ArcPlanVersion)
            .where(ArcPlanVersion.project_id == project_id, ArcPlanVersion.status.in_(("active", "planned")))
            .order_by(ArcPlanVersion.chapter_start, ArcPlanVersion.id)
            .execution_options(populate_existing=True)
        ))
        active = [arc for arc in arcs if arc.status == "active"]
        current = max(active, key=lambda arc: arc.chapter_start, default=None)
        if current is None:
            return None
        if current.chapter_start == after_chapter + 1:
            return current
        if current.chapter_end != after_chapter:
            return None
        following = next((arc for arc in arcs if arc.status == "planned"), None)
        if following is None:
            return None
        for arc in active:
            arc.status = "completed"
        following.status = "active"
        session.flush()
        return following


__all__ = ["ArcProgressionService"]
