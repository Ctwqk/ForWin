from __future__ import annotations

from datetime import datetime
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.api_schema import (
    ScenarioRehearsalDetail,
)
from forwin.models.world_v4 import ScenarioRehearsalRunRow
from .common import (
    _json_list_strings,
    _json_object,
)


DisplayDatetime = Callable[[datetime | None], str]
_GENESIS_STAGE_ORDER = (
    "brief",
    "world",
    "map",
    "story_engine",
    "book_blueprint",
    "bootstrap",
)
_PROJECT_DETAIL_CHAPTER_PREVIEW_LIMIT = 60
_PROJECT_SUMMARY_CHAPTER_PREVIEW_LIMIT = 3


def latest_scenario_rehearsal_run(
    session: Session,
    project_id: str,
    *,
    arc_id: str | None = None,
) -> ScenarioRehearsalRunRow | None:
    stmt = select(ScenarioRehearsalRunRow).where(
        ScenarioRehearsalRunRow.project_id == project_id
    )
    if arc_id:
        stmt = stmt.where(ScenarioRehearsalRunRow.arc_id == arc_id)
    return session.execute(
        stmt.order_by(
            ScenarioRehearsalRunRow.created_at.desc(),
            ScenarioRehearsalRunRow.id.desc(),
        ).limit(1)
    ).scalar_one_or_none()


def build_scenario_rehearsal_detail(
    *,
    project_id: str,
    latest: ScenarioRehearsalRunRow,
    display_datetime: DisplayDatetime,
) -> ScenarioRehearsalDetail:
    chapter_numbers = _json_list_strings(latest.chapter_numbers_json)
    trigger_reasons = _json_list_strings(latest.trigger_reasons_json)
    return ScenarioRehearsalDetail(
        project_id=project_id,
        arc_id=str(latest.arc_id or ""),
        band_id=str(latest.band_id or ""),
        rehearsal_scope=str(latest.rehearsal_scope or "band"),
        chapter_numbers=[
            int(item)
            for item in chapter_numbers
            if str(item).strip().lstrip("-").isdigit()
        ],
        trigger_reasons=trigger_reasons,
        recommendation=str(latest.recommendation or "pass"),
        risk_count=int(latest.risk_count or 0),
        blocker_count=int(latest.blocker_count or 0),
        required_patch_count=int(latest.required_patch_count or 0),
        resolution_status=str(
            _json_object(latest.report_json).get("resolution_status") or ""
        ),
        patch_attempt_count=int(
            _json_object(latest.report_json).get("patch_attempt_count") or 0
        ),
        checkpoint_id=str(_json_object(latest.report_json).get("checkpoint_id") or ""),
        replan_event_id=str(
            _json_object(latest.report_json).get("replan_event_id") or ""
        ),
        report=_json_object(latest.report_json),
        created_at=display_datetime(latest.created_at),
    )


__all__ = [
    "latest_scenario_rehearsal_run",
    "build_scenario_rehearsal_detail",
]
