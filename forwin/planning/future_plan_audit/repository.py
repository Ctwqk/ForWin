from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.base import new_id
from forwin.models.narrative_obligation import FuturePlanAuditRunRow

from .helpers import _json, _loads
from .models import FuturePlanAuditIssue, FuturePlanAuditRun


class FuturePlanAuditRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save_run(self, run: FuturePlanAuditRun) -> FuturePlanAuditRun:
        item = run.model_copy(update={"id": run.id or new_id()})
        row = FuturePlanAuditRunRow(
            id=item.id,
            project_id=item.project_id,
            current_chapter_number=item.current_chapter,
            trigger_stage=item.trigger_stage,
            inspected_chapters_json=_json(item.inspected_chapters),
            status=item.status,
            issues_json=_json([issue.model_dump(mode="json") for issue in item.issues]),
            applied_plan_patch_ids_json=_json(item.applied_plan_patch_ids),
            blocking_reasons_json=_json(item.blocking_reasons),
            metadata_json=_json(item.metadata),
            created_at=datetime.now(UTC),
        )
        self.session.add(row)
        self.session.flush()
        return self._from_row(row)

    def list_recent(self, project_id: str, *, limit: int = 5) -> list[FuturePlanAuditRun]:
        rows = self.session.execute(
            select(FuturePlanAuditRunRow)
            .where(FuturePlanAuditRunRow.project_id == project_id)
            .order_by(FuturePlanAuditRunRow.created_at.desc())
            .limit(max(1, int(limit or 1)))
        ).scalars().all()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row: FuturePlanAuditRunRow) -> FuturePlanAuditRun:
        return FuturePlanAuditRun(
            id=row.id,
            project_id=row.project_id,
            current_chapter=int(row.current_chapter_number or 0),
            trigger_stage=row.trigger_stage,
            inspected_chapters=[int(item) for item in _loads(row.inspected_chapters_json, [])],
            status=row.status,  # type: ignore[arg-type]
            issues=[
                FuturePlanAuditIssue.model_validate(item)
                for item in _loads(row.issues_json, [])
                if isinstance(item, dict)
            ],
            applied_plan_patch_ids=_loads(row.applied_plan_patch_ids_json, []),
            blocking_reasons=_loads(row.blocking_reasons_json, []),
            metadata=_loads(row.metadata_json, {}),
        )


__all__ = ["FuturePlanAuditRepository"]
