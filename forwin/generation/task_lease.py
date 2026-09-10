from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from forwin.models.task import GenerationTask


@dataclass(frozen=True)
class GenerationTaskClaimResult:
    task: GenerationTask
    claim_kind: Literal["queued", "expired_running", "capacity_wait"]
    lease_epoch: int = 0
    previous_lease_owner: str = ""
    previous_lease_expires_at: datetime | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def claim_generation_task(
    session: Session,
    *,
    worker_id: str,
    lease_seconds: int = 300,
) -> GenerationTaskClaimResult | None:
    now = utcnow()
    expires = now + timedelta(seconds=max(30, int(lease_seconds or 300)))
    row = (
        session.execute(
            select(GenerationTask)
            .where(
                GenerationTask.deleted_at.is_(None),
                GenerationTask.task_kind == "generation",
                or_(
                    and_(
                        GenerationTask.status == "queued",
                        GenerationTask.cancel_requested.is_(False),
                        GenerationTask.pause_requested.is_(False),
                    ),
                    and_(
                        GenerationTask.status.in_(["running", "capacity_wait"]),
                        or_(
                            GenerationTask.lease_expires_at.is_(None),
                            GenerationTask.lease_expires_at < now,
                        ),
                    ),
                ),
            )
            .order_by(GenerationTask.created_at.asc(), GenerationTask.id.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        .scalars()
        .first()
    )
    if row is None:
        return None
    previous_status = str(row.status or "")
    previous_lease_owner = str(row.lease_owner or "")
    previous_lease_expires_at = row.lease_expires_at
    claim_kind: Literal["queued", "expired_running", "capacity_wait"] = (
        "expired_running"
        if previous_status == "running"
        else "capacity_wait"
        if previous_status == "capacity_wait"
        else "queued"
    )
    row.status = "running"
    row.current_stage = "running"
    row.lease_owner = str(worker_id or "").strip()
    row.lease_epoch = max(0, int(row.lease_epoch or 0)) + 1
    row.lease_expires_at = expires
    row.heartbeat_at = now
    row.started_at = row.started_at or now
    row.finished_at = None
    session.add(row)
    return GenerationTaskClaimResult(
        task=row,
        claim_kind=claim_kind,
        lease_epoch=int(row.lease_epoch or 0),
        previous_lease_owner=previous_lease_owner
        if claim_kind == "expired_running"
        else "",
        previous_lease_expires_at=previous_lease_expires_at
        if claim_kind == "expired_running"
        else None,
    )


def heartbeat_generation_task(
    session: Session,
    *,
    task_id: str,
    worker_id: str,
    lease_epoch: int | None = None,
    lease_seconds: int = 300,
) -> bool:
    now = utcnow()
    row = session.get(GenerationTask, task_id)
    if (
        row is None
        or row.lease_owner != worker_id
        or (lease_epoch is not None and int(row.lease_epoch or 0) != int(lease_epoch))
        or row.status != "running"
    ):
        return False
    row.heartbeat_at = now
    row.lease_expires_at = now + timedelta(seconds=max(30, int(lease_seconds or 300)))
    session.add(row)
    return True


def generation_task_resume_from_chapter(task: GenerationTask) -> int:
    explicit = int(getattr(task, "resume_from_chapter", 0) or 0)
    completed = _json_ints(getattr(task, "completed_chapters_json", "[]"))
    # An explicit starting/wait point is consumed once that chapter completes.
    # Keep pending failed/paused work eligible instead of replaying old progress.
    if explicit > 0 and explicit not in completed:
        return explicit
    failed = _json_ints(getattr(task, "failed_chapters_json", "[]"))
    paused = _json_ints(getattr(task, "paused_chapters_json", "[]"))
    if failed:
        return min(failed)
    if paused:
        return min(paused)
    return max(completed, default=0) + 1


def _json_ints(value: str) -> list[int]:
    try:
        raw = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    result: list[int] = []
    for item in raw:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result
