"""Chapter plans as the band planner leaves them, for tests that start from planned chapters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select

from forwin.models.planning_v6 import BandPlanRow
from forwin.models.project import ArcPlanVersion, ChapterPlan
from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
from forwin.state.updater import StateUpdater


def stub_plan(chapter_number: int) -> dict[str, Any]:
    """Structure only, no story content."""
    return {
        "title": f"stub chapter {chapter_number}",
        "one_line": f"stub outline {chapter_number}",
        "goals": [f"stub goal {chapter_number}"],
    }


def write_band_plan(session, project_id: str, plans: Mapping[int, Mapping[str, Any]]) -> list[ChapterPlan]:
    """Plan rows for ``plans`` (chapter number -> title, one_line, goals) in one band plan of the active arc.

    The band plan hangs off the arc's active beat sheet, which start-writing wrote, so run control writes the
    rows as they are instead of planning their band again (Ruling P4a-R27).
    """
    numbers: Sequence[int] = sorted(plans)
    arc = session.scalar(select(ArcPlanVersion).where(
        ArcPlanVersion.project_id == project_id, ArcPlanVersion.status == "active",
        ArcPlanVersion.chapter_start <= numbers[0], ArcPlanVersion.chapter_end >= numbers[-1],
    ))
    sheets = ArcBeatSheetRepository(session)
    sheet = sheets.active_for_arc(arc.id)
    band = next(item for item in sheets.load(sheet).band_breakdown
                if item.chapter_start <= numbers[0] <= item.chapter_end)
    band_plan = BandPlanRow(
        project_id=project_id, arc_plan_id=arc.id, beat_sheet_id=sheet.id, band_index=band.band_index,
        chapter_start=band.chapter_start, chapter_end=band.chapter_end,
    )
    session.add(band_plan)
    session.flush()
    updater = StateUpdater(session)
    rows = []
    for number in numbers:
        plan = plans[number]
        row = updater.create_chapter_plan(project_id, arc.id, number, plan["title"], plan["one_line"], plan["goals"])
        row.band_plan_id = band_plan.id
        rows.append(row)
    session.flush()
    return rows


__all__ = ["stub_plan", "write_band_plan"]
