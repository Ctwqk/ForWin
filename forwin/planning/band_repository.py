from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.phase import BandExperiencePlan


class BandPlanRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_latest(
        self,
        *,
        project_id: str,
        arc_id: str,
        band_id: str,
    ) -> BandExperiencePlan | None:
        return self.session.execute(
            select(BandExperiencePlan)
            .where(
                BandExperiencePlan.project_id == project_id,
                BandExperiencePlan.arc_id == arc_id,
                BandExperiencePlan.band_id == band_id,
            )
            .order_by(BandExperiencePlan.created_at.desc(), BandExperiencePlan.id.desc())
            .limit(1)
        ).scalar_one_or_none()
