from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forwin.models import ArcPlanVersion, ChapterPlan


class ArcActivationService:
    def activate_for_chapter(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
    ) -> ArcPlanVersion | None:
        if int(chapter_number or 0) <= 0:
            return None
        chapter_plan = session.execute(
            select(ChapterPlan)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number == int(chapter_number),
            )
            .limit(1)
        ).scalar_one_or_none()
        if chapter_plan is None:
            return None
        target_arc = session.get(ArcPlanVersion, chapter_plan.arc_plan_id)
        if target_arc is None or target_arc.status == "active":
            return target_arc
        active_rows = session.execute(
            select(ArcPlanVersion)
            .where(
                ArcPlanVersion.project_id == project_id,
                ArcPlanVersion.status == "active",
            )
        ).scalars().all()
        for row in active_rows:
            if row.id == target_arc.id:
                continue
            max_chapter = session.execute(
                select(func.max(ChapterPlan.chapter_number)).where(ChapterPlan.arc_plan_id == row.id)
            ).scalar_one()
            row.status = "completed" if int(max_chapter or 0) < int(chapter_number) else "planned"
            session.add(row)
        target_arc.status = "active"
        session.add(target_arc)
        session.flush()
        return target_arc
