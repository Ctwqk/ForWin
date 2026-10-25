"""Validated beat-sheet reads shared by planning and book state."""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.book_state.beat_sheet_types import ArcBeatSheetData
from forwin.models.planning_v6 import ArcBeatSheetRow
from forwin.models.project import ArcPlanVersion


def active_beat_sheets_for_project(session: Session, project_id: str) -> list[ArcBeatSheetRow]:
    """The active sheet of every arc that has one, in arc order; superseded arc plans have none."""
    return list(session.scalars(
        select(ArcBeatSheetRow)
        .join(ArcPlanVersion, ArcPlanVersion.id == ArcBeatSheetRow.arc_plan_id)
        .where(
            ArcBeatSheetRow.project_id == project_id, ArcBeatSheetRow.status == "active",
            ArcPlanVersion.status != "superseded",
        )
        .order_by(ArcPlanVersion.chapter_start, ArcBeatSheetRow.arc_plan_id)
    ))


def load_beat_sheet(row: ArcBeatSheetRow) -> ArcBeatSheetData:
    return ArcBeatSheetData.model_validate({
        "protagonist_ref": row.protagonist_ref,
        "phases": json.loads(row.phases_json),
        "milestones": json.loads(row.milestones_json),
        "protections": json.loads(row.protections_json),
        "reveal_targets": json.loads(row.reveal_targets_json),
        "exit_state": json.loads(row.exit_state_json),
        "band_breakdown": json.loads(row.band_breakdown_json),
        "reader_promise": json.loads(row.reader_promise_json),
        "arc_payoff_map": json.loads(row.arc_payoff_map_json),
        "setting_brief": row.setting_brief,
    })
