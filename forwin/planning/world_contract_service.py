from __future__ import annotations

from sqlalchemy.orm import Session

from forwin.models import ArcPlanVersion, ChapterPlan, Project
from forwin.planning.band_window import BandWindowResolver
from forwin.planning.world_contracts import (
    ArcWorldContract,
    BandWorldContract,
    ChapterWorldDeltaIntent,
    WorldContractRepository,
)


def _load_goals_json(raw: str) -> list[str]:
    try:
        payload = __import__("json").loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(item) for item in payload if str(item).strip()] if isinstance(payload, list) else []


class WorldContractPlanningService:
    def ensure_for_arc_band(
        self,
        *,
        session: Session,
        project_id: str,
        arc_id: str,
        chapter_plans: list[ChapterPlan],
        activation_chapter: int,
        detailed_band_size: int,
    ) -> None:
        if not chapter_plans:
            return
        project = session.get(Project, project_id)
        arc = session.get(ArcPlanVersion, arc_id)
        if project is None or arc is None:
            return

        ordered_plans = sorted(chapter_plans, key=lambda plan: plan.chapter_number)
        primary_line_ids = ["primary_visible_line"]

        repo = WorldContractRepository(session)
        repo.save_arc_contract(
            ArcWorldContract(
                contract_id=f"arc_world_contract_{arc.id}",
                project_id=project_id,
                arc_id=arc_id,
                arc_number=arc.arc_number,
                title=arc.arc_synopsis,
                primary_world_line_ids=primary_line_ids,
                arc_exit_objective_state=arc.arc_synopsis,
                arc_exit_reader_state="aware",
            )
        )

        window = BandWindowResolver().resolve(
            chapter_plans=ordered_plans,
            activation_chapter=activation_chapter,
            detailed_band_size=detailed_band_size,
        )
        repo.save_band_contract(
            BandWorldContract(
                contract_id=f"band_world_contract_{arc.id}_{window.chapter_start}_{window.chapter_end}",
                project_id=project_id,
                arc_id=arc_id,
                band_id=window.band_id,
                chapter_start=window.chapter_start,
                chapter_end=window.chapter_end,
                foreground_world_line_ids=primary_line_ids,
                band_exit_reader_state="aware",
                band_exit_hidden_line_state="",
            )
        )

        for plan in window.active_band:
            repo.save_chapter_intent(
                ChapterWorldDeltaIntent(
                    intent_id=f"chapter_{plan.chapter_number}_world_intent",
                    project_id=project_id,
                    chapter_plan_id=plan.id,
                    chapter_number=plan.chapter_number,
                    visible_delta_intents=_load_goals_json(plan.goals_json)[:1],
                )
            )
