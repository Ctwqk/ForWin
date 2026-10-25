"""Versioned arc beat sheet rows: one active version per arc."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from forwin.book_state.beat_sheet_read import active_beat_sheets_for_project, load_beat_sheet
from forwin.models.planning_v6 import ArcBeatSheetRow
from forwin.planning.beat_sheet.types import BEAT_SHEET_SOURCES, ArcBeatSheetData


class ArcBeatSheetRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def active_for_arc(self, arc_plan_id: str) -> ArcBeatSheetRow | None:
        return self.session.scalar(
            select(ArcBeatSheetRow)
            .where(ArcBeatSheetRow.arc_plan_id == arc_plan_id, ArcBeatSheetRow.status == "active")
            .order_by(ArcBeatSheetRow.version.desc())
            .limit(1)
        )

    def active_for_project(self, project_id: str) -> list[ArcBeatSheetRow]:
        return active_beat_sheets_for_project(self.session, project_id)

    def create(
        self, project_id: str, arc_plan_id: str, data: ArcBeatSheetData, *, source: str, reason: str,
    ) -> ArcBeatSheetRow:
        """Add the arc's next version as the active one; the version it replaces becomes superseded."""
        if source not in BEAT_SHEET_SOURCES:
            raise ValueError(f"unknown beat sheet source: {source!r}")
        latest = self.session.scalar(
            select(func.max(ArcBeatSheetRow.version)).where(ArcBeatSheetRow.arc_plan_id == arc_plan_id)
        )
        self.session.execute(
            update(ArcBeatSheetRow)
            .where(ArcBeatSheetRow.arc_plan_id == arc_plan_id, ArcBeatSheetRow.status == "active")
            .values(status="superseded")
            .execution_options(synchronize_session="fetch")
        )
        row = ArcBeatSheetRow(
            project_id=project_id,
            arc_plan_id=arc_plan_id,
            version=int(latest or 0) + 1,
            status="active",
            source=source,
            protagonist_ref=data.protagonist_ref,
            phases_json=_dump(data.phases),
            milestones_json=_dump(data.milestones),
            protections_json=_dump(data.protections),
            reveal_targets_json=_dump(data.reveal_targets),
            exit_state_json=_dump(data.exit_state),
            band_breakdown_json=_dump(data.band_breakdown),
            reader_promise_json=_dump(data.reader_promise),
            arc_payoff_map_json=_dump(data.arc_payoff_map),
            setting_brief=data.setting_brief,
            revision_reason=reason,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def load(self, row: ArcBeatSheetRow) -> ArcBeatSheetData:
        return load_beat_sheet(row)


def _dump(value: Any) -> str:
    if isinstance(value, list):
        return json.dumps([item.model_dump(mode="json") for item in value], ensure_ascii=False)
    return json.dumps(value.model_dump(mode="json"), ensure_ascii=False)


__all__ = ["ArcBeatSheetRepository"]
