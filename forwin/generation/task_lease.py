from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from forwin.models.task import GenerationTask


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def claim_generation_task(
    session: Session,
    *,
    worker_id: str,
    lease_seconds: int = 300,
) -> GenerationTask | None:
    now = utcnow()
    expires = now + timedelta(seconds=max(30, int(lease_seconds or 300)))
    row = (
        session.execute(
            select(GenerationTask)
            .where(
                GenerationTask.deleted_at.is_(None),
                GenerationTask.task_kind == "generation",
                or_(
                    GenerationTask.status == "queued",
                    GenerationTask.status == "starting",
                    (
                        (GenerationTask.status == "running")
                        & (
                            GenerationTask.lease_expires_at.is_(None)
                            | (GenerationTask.lease_expires_at < now)
                        )
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
    row.status = "running"
    row.current_stage = "running"
    row.lease_owner = str(worker_id or "").strip()
    row.lease_expires_at = expires
    row.heartbeat_at = now
    row.started_at = row.started_at or now
    row.finished_at = None
    session.add(row)
    return row


def heartbeat_generation_task(
    session: Session,
    *,
    task_id: str,
    worker_id: str,
    lease_seconds: int = 300,
) -> bool:
    now = utcnow()
    row = session.get(GenerationTask, task_id)
    if row is None or row.lease_owner != worker_id or row.status != "running":
        return False
    row.heartbeat_at = now
    row.lease_expires_at = now + timedelta(seconds=max(30, int(lease_seconds or 300)))
    session.add(row)
    return True


def generation_task_resume_from_chapter(task: GenerationTask) -> int:
    explicit = int(getattr(task, "resume_from_chapter", 0) or 0)
    if explicit > 0:
        return explicit
    completed = _json_ints(getattr(task, "completed_chapters_json", "[]"))
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
