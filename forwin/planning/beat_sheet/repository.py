"""Versioned arc beat sheet rows: one active version per arc."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from forwin.models.planning_v6 import ArcBeatSheetRow
from forwin.models.project import ArcPlanVersion
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
        """The active sheet of every arc that has one, in arc order."""
        return list(self.session.scalars(
            select(ArcBeatSheetRow)
            .join(ArcPlanVersion, ArcPlanVersion.id == ArcBeatSheetRow.arc_plan_id)
            .where(ArcBeatSheetRow.project_id == project_id, ArcBeatSheetRow.status == "active")
            .order_by(ArcPlanVersion.chapter_start, ArcBeatSheetRow.arc_plan_id)
        ))

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
            revision_reason=reason,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def load(self, row: ArcBeatSheetRow) -> ArcBeatSheetData:
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
        })


def _dump(value: Any) -> str:
    if isinstance(value, list):
        return json.dumps([item.model_dump(mode="json") for item in value], ensure_ascii=False)
    return json.dumps(value.model_dump(mode="json"), ensure_ascii=False)


__all__ = ["ArcBeatSheetRepository"]
