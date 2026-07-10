from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from forwin.models.phase import BandExperiencePlan
    from forwin.models.project import ArcPlanVersion, ChapterPlan


class PlanningQuery:
    """Read-only access to authoritative runtime plans."""

    def active_arc(self, session: Session, project_id: str) -> ArcPlanVersion | None:
        from forwin.models.project import ArcPlanVersion

        return session.execute(
            select(ArcPlanVersion)
            .where(
                ArcPlanVersion.project_id == project_id,
                ArcPlanVersion.status == "active",
            )
            .order_by(ArcPlanVersion.version.desc())
            .limit(1)
        ).scalar_one_or_none()

    def arc_chapters(self, session: Session, arc_id: str) -> list[ChapterPlan]:
        from forwin.models.project import ChapterPlan

        return list(
            session.execute(
                select(ChapterPlan)
                .where(ChapterPlan.arc_plan_id == arc_id)
                .order_by(ChapterPlan.chapter_number.asc())
            ).scalars().all()
        )

    def future_chapters(
        self,
        session: Session,
        *,
        project_id: str,
        current_chapter: int,
        include_current: bool = False,
    ) -> list[ChapterPlan]:
        from forwin.models.project import ChapterPlan

        lower_bound = int(current_chapter)
        predicate = (
            ChapterPlan.chapter_number >= lower_bound
            if include_current
            else ChapterPlan.chapter_number > lower_bound
        )
        return list(
            session.execute(
                select(ChapterPlan)
                .where(
                    ChapterPlan.project_id == project_id,
                    predicate,
                    ChapterPlan.status.in_(("planned", "failed")),
                )
                .order_by(ChapterPlan.chapter_number.asc())
            ).scalars().all()
        )

    def future_bands(
        self,
        session: Session,
        *,
        project_id: str,
        current_chapter: int,
    ) -> list[BandExperiencePlan]:
        from forwin.models.phase import BandExperiencePlan

        return list(
            session.execute(
                select(BandExperiencePlan)
                .where(
                    BandExperiencePlan.project_id == project_id,
                    BandExperiencePlan.chapter_end > int(current_chapter),
                )
                .order_by(
                    BandExperiencePlan.chapter_start.asc(),
                    BandExperiencePlan.chapter_end.asc(),
                )
            ).scalars().all()
        )


__all__ = ["PlanningQuery"]
