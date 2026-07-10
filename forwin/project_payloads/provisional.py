from __future__ import annotations

import json
from datetime import datetime
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.api_schema import (
    ProvisionalBandDetail,
    ProvisionalChapterLedgerInfo,
)
from forwin.models.phase import (
    ProvisionalBandExecution,
    ProvisionalChapterLedger,
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
from .common import (
    _json_object,
    _load_json_list,
)


def latest_provisional_band_execution(
    session: Session,
    project_id: str,
    *,
    arc_id: str | None = None,
) -> ProvisionalBandExecution | None:
    stmt = select(ProvisionalBandExecution).where(
        ProvisionalBandExecution.project_id == project_id
    )
    if arc_id:
        stmt = stmt.where(ProvisionalBandExecution.arc_id == arc_id)
    return session.execute(
        stmt.order_by(ProvisionalBandExecution.created_at.desc()).limit(1)
    ).scalar_one_or_none()


def build_provisional_band_detail(
    *,
    session: Session,
    project_id: str,
    latest: ProvisionalBandExecution,
    display_datetime: DisplayDatetime,
) -> ProvisionalBandDetail:
    ledgers = list(
        session.execute(
            select(ProvisionalChapterLedger)
            .where(
                ProvisionalChapterLedger.project_id == project_id,
                ProvisionalChapterLedger.arc_id == latest.arc_id,
                ProvisionalChapterLedger.band_id == latest.band_id,
            )
            .order_by(
                ProvisionalChapterLedger.chapter_number.asc(),
                ProvisionalChapterLedger.created_at.asc(),
            )
        )
        .scalars()
        .all()
    )
    chapter_numbers = []
    try:
        chapter_numbers = json.loads(latest.chapter_numbers_json or "[]") or []
    except (json.JSONDecodeError, TypeError):
        chapter_numbers = []

    return ProvisionalBandDetail(
        project_id=project_id,
        arc_id=latest.arc_id,
        band_id=latest.band_id,
        aggregate_verdict=latest.aggregate_verdict,
        preview_char_count=latest.preview_char_count,
        issue_count=latest.issue_count,
        failure_count=latest.failure_count,
        artifact_path=latest.artifact_path,
        chapter_numbers=[
            int(item) for item in chapter_numbers if isinstance(item, int)
        ],
        created_at=display_datetime(latest.created_at),
        chapters=[
            ProvisionalChapterLedgerInfo(
                chapter_number=row.chapter_number,
                title=row.title,
                summary=row.summary,
                verdict=row.verdict,
                char_count=row.char_count,
                artifact_meta_path=row.artifact_meta_path,
                draft_blob_path=row.draft_blob_path,
                current_time_label=row.current_time_label,
                projected_time_label=row.projected_time_label,
                state_changes=_load_json_list(row.state_changes_json),
                events=_load_json_list(row.events_json),
                thread_beats=_load_json_list(row.thread_beats_json),
                time_advance=_json_object(row.time_advance_json),
                issues=_load_json_list(row.issues_json),
                error=row.error_text,
                created_at=display_datetime(row.created_at),
            )
            for row in ledgers
        ],
    )


__all__ = [
    "latest_provisional_band_execution",
    "build_provisional_band_detail",
]
