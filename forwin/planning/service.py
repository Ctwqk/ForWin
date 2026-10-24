from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .query import PlanningQuery

if TYPE_CHECKING:
    from forwin.experience.band_scheduler import BandExperienceScheduler
    from forwin.experience.chapter_planner import ChapterExperiencePlanner
    from forwin.experience.persistence import ExperiencePersistence
    from forwin.experience.service import ExperiencePlanningService


@dataclass(slots=True)
class PlanningService:
    """Composition facade for runtime plan reads and mutations."""

    query: PlanningQuery
    experience: ExperiencePlanningService
    experience_persistence: ExperiencePersistence
    band_scheduler: BandExperienceScheduler
    chapter_planner: ChapterExperiencePlanner

    @classmethod
    def build_default(cls) -> "PlanningService":
        from forwin.experience.band_scheduler import BandExperienceScheduler
        from forwin.experience.chapter_planner import ChapterExperiencePlanner
        from forwin.experience.persistence import ExperiencePersistence
        from forwin.experience.service import ExperiencePlanningService

        experience = ExperiencePlanningService()
        persistence = ExperiencePersistence()
        band_scheduler = BandExperienceScheduler()
        chapter_planner = ChapterExperiencePlanner()
        return cls(
            query=PlanningQuery(),
            experience=experience,
            experience_persistence=persistence,
            band_scheduler=band_scheduler,
            chapter_planner=chapter_planner,
        )


__all__ = ["PlanningService"]
