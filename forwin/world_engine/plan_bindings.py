"""Read onstage intent bindings from chapter plans and their active band."""

from __future__ import annotations

import json
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session


def _intent_bindings(plans: Sequence) -> frozenset[str]:
    return frozenset(
        binding["ref"] for plan in plans for binding in json.loads(plan.world_bindings_json or "[]")
        if isinstance(binding, dict) and binding.get("kind") == "intent" and isinstance(binding.get("ref"), str)
    )


def onstage_plan_bindings(
    session: Session, project_id: str, chapter_number: int,
) -> tuple[frozenset[str], bool, frozenset[str]]:
    """Current bindings, whether the band ends, and every intent bound anywhere in that band."""
    from forwin.models.planning_v6 import BandPlanRow
    from forwin.models.project import ArcPlanVersion, ChapterPlan
    from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository

    plans = list(session.scalars(select(ChapterPlan).where(
        ChapterPlan.project_id == project_id, ChapterPlan.chapter_number == chapter_number,
    )))
    current = _intent_bindings(plans)
    band = session.scalar(select(BandPlanRow).join(ArcPlanVersion, ArcPlanVersion.id == BandPlanRow.arc_plan_id).where(
        BandPlanRow.project_id == project_id, BandPlanRow.status == "active",
        ArcPlanVersion.status != "superseded",
        BandPlanRow.chapter_start <= chapter_number, BandPlanRow.chapter_end >= chapter_number,
    ).order_by(BandPlanRow.version.desc()).limit(1))
    if band is None:
        repo = ArcBeatSheetRepository(session)
        band = next((span for sheet in repo.active_for_project(project_id) for span in repo.load(sheet).band_breakdown
                     if span.chapter_start <= chapter_number <= span.chapter_end), None)
    if band is None:
        return current, False, frozenset()
    band_plans = list(session.scalars(select(ChapterPlan).where(
        ChapterPlan.project_id == project_id,
        ChapterPlan.chapter_number >= band.chapter_start, ChapterPlan.chapter_number <= band.chapter_end,
    )))
    return current, chapter_number == band.chapter_end, _intent_bindings(band_plans)
