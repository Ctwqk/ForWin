from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from forwin.experience.band_scheduler import BandExperienceScheduler
from forwin.experience.chapter_planner import ChapterExperiencePlanner
from forwin.experience.persistence import ExperiencePersistence
from forwin.experience.service import ExperiencePlanningService
from forwin.experience.types import ArcExperienceBundle
from forwin.models.project import ChapterPlan, Project
from forwin.planning.band_plan.band_role import classify_band_role
from forwin.planning.band_plan.contract_templates import contract_for_role
from forwin.planning.arc_structure_service import ArcStructureDraftData
from forwin.planning.band_window import BandWindowResolver
from forwin.planning.world_contract_service import WorldContractPlanningService
from forwin.state.updater import StateUpdater


@dataclass(slots=True)
class BandPlanningRequest:
    project_id: str
    arc_id: str
    activation_chapter: int
    detailed_band_size: int
    chapter_plans: list[ChapterPlan]
    structure: ArcStructureDraftData
    arc_experience: ArcExperienceBundle


@dataclass(slots=True)
class BandPlanningResult:
    band_id: str
    chapter_start: int
    chapter_end: int
    schedule: object
    updated_chapter_numbers: list[int]


@dataclass(slots=True)
class BandPlanningBundle:
    request: BandPlanningRequest
    result: BandPlanningResult


class BandPlanService:
    def __init__(
        self,
        *,
        subworld_manager: Any | None = None,
        world_contract_service: WorldContractPlanningService | None = None,
        experience_service: ExperiencePlanningService | None = None,
        scheduler: BandExperienceScheduler | None = None,
        chapter_planner: ChapterExperiencePlanner | None = None,
        persistence: ExperiencePersistence | None = None,
        window_resolver: BandWindowResolver | None = None,
    ) -> None:
        if subworld_manager is None:
            from forwin.subworld_manager import SubWorldManager

            subworld_manager = SubWorldManager()
        self.subworld_manager = subworld_manager or SubWorldManager()
        self.world_contract_service = world_contract_service or WorldContractPlanningService()
        self.experience_service = experience_service or ExperiencePlanningService()
        self.scheduler = scheduler or BandExperienceScheduler()
        self.chapter_planner = chapter_planner or ChapterExperiencePlanner()
        self.persistence = persistence or ExperiencePersistence()
        self.window_resolver = window_resolver or BandWindowResolver()

    def ensure_current_band_plan(
        self,
        *,
        session: Session,
        request: BandPlanningRequest,
    ) -> BandPlanningResult:
        window = self.window_resolver.resolve(
            chapter_plans=request.chapter_plans,
            activation_chapter=request.activation_chapter,
            detailed_band_size=request.detailed_band_size,
        )
        calibration = self.experience_service.build_audience_calibration_profile(
            session=session,
            project_id=request.project_id,
        )
        schedule = self.scheduler.derive_band_delight_schedule(
            band_id=window.band_id,
            chapter_start=window.chapter_start,
            chapter_end=window.chapter_end,
            structure=request.structure,
            arc_experience=request.arc_experience,
            active_band=window.active_band,
            calibration=calibration,
        )
        role = classify_band_role(
            band_index=_band_index(window.chapter_start, request.detailed_band_size),
            total_bands=_total_bands(
                _target_total_from_project_or_plans(
                    session=session,
                    project_id=request.project_id,
                    chapter_plans=request.chapter_plans,
                ),
                request.detailed_band_size,
            ),
            target_total_chapters=_target_total_from_project_or_plans(
                session=session,
                project_id=request.project_id,
                chapter_plans=request.chapter_plans,
            ),
            last_chapter_of_band=window.chapter_end,
        )
        contract = contract_for_role(role.role)
        schedule = schedule.model_copy(
            update={
                "band_role": role.role.value,
                "band_role_reason": role.reason,
                "band_contract_template": contract.model_dump(mode="json"),
            }
        )
        activation_plan = self.subworld_manager.plan_band_activation(
            session=session,
            updater=StateUpdater(session),
            project_id=request.project_id,
            chapter_start=window.chapter_start,
            chapter_end=window.chapter_end,
            active_band=window.active_band,
        )
        schedule = schedule.model_copy(
            update={
                "active_subworld_ids": activation_plan.active_subworld_ids,
                "chapter_entry_targets": activation_plan.chapter_entry_targets,
            }
        )
        self.persistence.save_band_experience_plan(
            session=session,
            project_id=request.project_id,
            arc_id=request.arc_id,
            schedule=schedule,
        )
        updated_numbers: list[int] = []
        for plan in window.active_band:
            experience_plan = self.chapter_planner.derive_chapter_experience_plan(
                chapter_number=plan.chapter_number,
                structure=request.structure,
                arc_experience=request.arc_experience,
                schedule=schedule,
                chapter_plan=plan,
                calibration=calibration,
            )
            chapter_targets = [
                item for item in schedule.chapter_entry_targets if item.chapter_hint == plan.chapter_number
            ]
            experience_plan = experience_plan.model_copy(
                update={
                    "active_subworld_ids": list(schedule.active_subworld_ids),
                    "chapter_entry_targets": chapter_targets,
                    "entity_admission_rule": "strict_named_character",
                }
            )
            self.persistence.save_chapter_experience_plan(
                chapter_plan=plan,
                experience_plan=experience_plan,
            )
            session.add(plan)
            updated_numbers.append(int(plan.chapter_number or 0))
        self.world_contract_service.ensure_for_arc_band(
            session=session,
            project_id=request.project_id,
            arc_id=request.arc_id,
            chapter_plans=request.chapter_plans,
            activation_chapter=request.activation_chapter,
            detailed_band_size=request.detailed_band_size,
        )
        session.flush()
        return BandPlanningResult(
            band_id=window.band_id,
            chapter_start=window.chapter_start,
            chapter_end=window.chapter_end,
            schedule=schedule,
            updated_chapter_numbers=updated_numbers,
        )


def _target_total_from_project_or_plans(
    *,
    session: Session,
    project_id: str,
    chapter_plans: list[ChapterPlan],
) -> int:
    project = session.get(Project, project_id)
    if project is not None and int(project.target_total_chapters or 0) > 0:
        return int(project.target_total_chapters or 0)
    return max([int(plan.chapter_number or 0) for plan in chapter_plans] or [0])


def _band_index(chapter_start: int, band_size: int) -> int:
    size = max(1, int(band_size or 1))
    return max(0, (int(chapter_start or 1) - 1) // size)


def _total_bands(target_total_chapters: int, band_size: int) -> int:
    target = int(target_total_chapters or 0)
    if target <= 0:
        return 0
    size = max(1, int(band_size or 1))
    return (target + size - 1) // size
