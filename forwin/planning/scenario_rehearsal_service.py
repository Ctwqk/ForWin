from __future__ import annotations

from typing import Any, Callable

from sqlalchemy.orm import Session

from forwin.models.project import ChapterPlan
from forwin.planning.scenario_rehearsal_resolution import (
    ScenarioRehearsalCoordinator,
    ScenarioRehearsalOutcome,
)


class ScenarioRehearsalService:
    def __init__(
        self,
        *,
        director: Any | None = None,
        progress_callback: Callable[..., None] | None = None,
    ) -> None:
        self.director = director
        self.progress_callback = progress_callback

    def run_for_band(
        self,
        *,
        session: Session,
        project_id: str,
        arc_id: str,
        band_id: str,
        chapter_plans: list[ChapterPlan],
    ) -> ScenarioRehearsalOutcome:
        if self.progress_callback is not None:
            self.progress_callback(
                stage="running_scenario_rehearsal",
                project_id=project_id,
                current_chapter=chapter_plans[0].chapter_number if chapter_plans else 0,
                message="",
            )
        outcome = ScenarioRehearsalCoordinator(session, director=self.director).run_for_band(
            project_id=project_id,
            arc_id=arc_id,
            band_id=band_id,
            chapter_numbers=[plan.chapter_number for plan in chapter_plans],
        )
        if self.progress_callback is None:
            return outcome
        if outcome.status in {"manual_patch_required", "replan_required"}:
            self.progress_callback(
                stage="scenario_rehearsal_patch_required",
                project_id=project_id,
                current_chapter=chapter_plans[0].chapter_number if chapter_plans else 0,
                message="Scenario rehearsal 要求计划补丁或重排。",
            )
        elif outcome.status == "blocked":
            self.progress_callback(
                stage="scenario_rehearsal_blocked",
                project_id=project_id,
                current_chapter=chapter_plans[0].chapter_number if chapter_plans else 0,
                message="Scenario rehearsal 阻断当前计划。",
            )
        return outcome
