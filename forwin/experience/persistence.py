from __future__ import annotations

import json

from sqlalchemy.orm import Session

from forwin.experience.types import ArcExperienceBundle
from forwin.models.phase import ArcStructureDraft
from forwin.models.project import ChapterPlan
from forwin.protocol.experience import BandDelightSchedule, ChapterExperiencePlan
from forwin.state.updater import StateUpdater


class ExperiencePersistence:
    def persist_arc_experience(
        self,
        *,
        structure_row: ArcStructureDraft,
        arc_experience: ArcExperienceBundle,
    ) -> None:
        structure_row.reader_promise_json = json.dumps(
            arc_experience.reader_promise.model_dump(mode="json"),
            ensure_ascii=False,
        )
        structure_row.arc_payoff_map_json = json.dumps(
            arc_experience.arc_payoff_map.model_dump(mode="json"),
            ensure_ascii=False,
        )

    def save_band_experience_plan(
        self,
        *,
        session: Session,
        project_id: str,
        arc_id: str,
        schedule: BandDelightSchedule,
    ):
        return StateUpdater(session).save_band_experience_plan(
            project_id=project_id,
            arc_id=arc_id,
            schedule=schedule,
        )

    def save_trope_usage_records(
        self,
        *,
        session: Session,
        project_id: str,
        arc_id: str,
        schedule: BandDelightSchedule,
    ) -> None:
        from forwin.experience.trope_cooldown import save_trope_usage

        for item in schedule.scheduled_rewards:
            template_id = str(item.template_id or "").strip()
            if not template_id:
                continue
            save_trope_usage(
                session,
                project_id=project_id,
                arc_id=arc_id,
                band_id=schedule.band_id,
                chapter_number=int(item.chapter_hint or 0),
                template_id=template_id,
                category=str(item.category or ""),
            )

    def save_chapter_experience_plan(
        self,
        *,
        chapter_plan: ChapterPlan,
        experience_plan: ChapterExperiencePlan,
    ) -> None:
        chapter_plan.experience_plan_json = json.dumps(
            experience_plan.model_dump(mode="json"),
            ensure_ascii=False,
        )
