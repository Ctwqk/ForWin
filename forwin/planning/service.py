from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .query import PlanningQuery

if TYPE_CHECKING:
    from forwin.director.arc_director import ArcDirector
    from forwin.experience.arc_experience_planner import ArcExperiencePlanningService
    from forwin.experience.band_scheduler import BandExperienceScheduler
    from forwin.experience.chapter_planner import ChapterExperiencePlanner
    from forwin.experience.persistence import ExperiencePersistence
    from forwin.experience.service import ExperiencePlanningService
    from forwin.subworld_manager import SubWorldManager

    from .arc_activation_service import ArcActivationService
    from .arc_envelope_resolver import ArcEnvelopeResolver
    from .arc_structure_service import ArcStructurePlanningService
    from .band_plan_service import BandPlanService
    from .provisional_preview_service import ProvisionalPreviewService
    from .scenario_rehearsal_service import ScenarioRehearsalService
    from .world_contract_service import WorldContractPlanningService


@dataclass(slots=True)
class PlanningService:
    """Composition facade for runtime plan reads and mutations."""

    query: PlanningQuery
    arc_activation: ArcActivationService
    arc_envelope_resolver: ArcEnvelopeResolver
    arc_structure: ArcStructurePlanningService
    arc_experience: ArcExperiencePlanningService
    experience: ExperiencePlanningService
    experience_persistence: ExperiencePersistence
    band_scheduler: BandExperienceScheduler
    chapter_planner: ChapterExperiencePlanner
    band_plan: BandPlanService
    world_contracts: WorldContractPlanningService
    scenario_rehearsal: ScenarioRehearsalService
    provisional_preview: ProvisionalPreviewService

    @classmethod
    def build_default(
        cls,
        *,
        director: ArcDirector | None = None,
        provisional_executor: Any | None = None,
        subworld_manager: SubWorldManager | None = None,
        provisional_preview_enabled: bool = False,
        scenario_progress_callback: Any | None = None,
        trope_cost_ceiling: int = 3,
    ) -> "PlanningService":
        from forwin.experience.arc_experience_planner import ArcExperiencePlanningService
        from forwin.experience.band_scheduler import BandExperienceScheduler
        from forwin.experience.chapter_planner import ChapterExperiencePlanner
        from forwin.experience.persistence import ExperiencePersistence
        from forwin.experience.service import ExperiencePlanningService
        from forwin.subworld_manager import SubWorldManager

        from .arc_activation_service import ArcActivationService
        from .arc_envelope_resolver import ArcEnvelopeResolver
        from .arc_structure_service import ArcStructurePlanningService
        from .band_plan_service import BandPlanService
        from .provisional_preview_service import ProvisionalPreviewService
        from .scenario_rehearsal_service import ScenarioRehearsalService
        from .world_contract_service import WorldContractPlanningService

        resolved_subworld_manager = subworld_manager or SubWorldManager(
            director=director
        )
        world_contracts = WorldContractPlanningService()
        experience = ExperiencePlanningService()
        persistence = ExperiencePersistence()
        band_scheduler = BandExperienceScheduler()
        chapter_planner = ChapterExperiencePlanner()
        return cls(
            query=PlanningQuery(),
            arc_activation=ArcActivationService(),
            arc_envelope_resolver=ArcEnvelopeResolver(director=director),
            arc_structure=ArcStructurePlanningService(director=director),
            arc_experience=ArcExperiencePlanningService(),
            experience=experience,
            experience_persistence=persistence,
            band_scheduler=band_scheduler,
            chapter_planner=chapter_planner,
            band_plan=BandPlanService(
                subworld_manager=resolved_subworld_manager,
                world_contract_service=world_contracts,
                experience_service=experience,
                scheduler=band_scheduler,
                chapter_planner=chapter_planner,
                persistence=persistence,
                trope_cost_ceiling=trope_cost_ceiling,
            ),
            world_contracts=world_contracts,
            scenario_rehearsal=ScenarioRehearsalService(
                director=director,
                progress_callback=scenario_progress_callback,
            ),
            provisional_preview=ProvisionalPreviewService(
                provisional_executor=provisional_executor,
                provisional_preview_enabled=provisional_preview_enabled,
            ),
        )


__all__ = ["PlanningService"]
