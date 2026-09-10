from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from forwin.api_schema import (
    PostCanonMaintenanceChapterInfo,
    PostCanonMaintenanceRunInfo,
    PostCanonMaintenanceStatusResponse,
)
from forwin.canon.identity import active_commit_predicate
from forwin.http.request_support import require_project
from forwin.maintenance.events import POST_CANON_STEP_NAMES
from forwin.maintenance.state import (
    post_canon_barrier_blockers,
    post_canon_control_result,
    post_canon_order_controls,
    post_canon_phase3_complete,
)
from forwin.models.canon import CanonCommitRecord
from forwin.models.maintenance import PostCanonMaintenanceRun
from forwin.models.planning_control import BandCheckpoint
from forwin.runtime.policy_store import ProjectPolicyStore


def build_handlers(*, get_session: Callable[[], Any]) -> dict[str, Callable[..., Any]]:
    def get_post_canon_maintenance_status(
        project_id: str,
        chapter_number: int = 0,
    ) -> dict[str, Any]:
        with get_session() as session:
            project = require_project(session, project_id)
            policy = ProjectPolicyStore(session).load(project).policy
            query = select(CanonCommitRecord).where(
                CanonCommitRecord.project_id == project_id,
                CanonCommitRecord.status == "committed",
                    active_commit_predicate(),
            )
            if int(chapter_number or 0) > 0:
                query = query.where(
                    CanonCommitRecord.chapter_number == int(chapter_number)
                )
            commits = list(
                session.execute(
                    query.order_by(CanonCommitRecord.chapter_number.asc())
                ).scalars()
            )
            commit_ids = [row.id for row in commits]
            runs = (
                list(
                    session.execute(
                        select(PostCanonMaintenanceRun).where(
                            PostCanonMaintenanceRun.canon_commit_id.in_(commit_ids)
                        )
                    ).scalars()
                )
                if commit_ids
                else []
            )
            runs_by_commit: dict[str, list[PostCanonMaintenanceRun]] = {}
            for row in runs:
                runs_by_commit.setdefault(row.canon_commit_id, []).append(row)

            chapters = []
            for commit in commits:
                chapter_rows = runs_by_commit.get(commit.id, [])
                chapters.append(
                    _chapter_info(
                        commit,
                        chapter_rows,
                        band_checkpoint_action=policy.pause.band_checkpoint_action,
                        checkpoint_status=_live_checkpoint_status(
                            session,
                            chapter_rows,
                        ),
                        session=session,
                    )
                )
            response = PostCanonMaintenanceStatusResponse(
                project_id=project_id,
                ready=bool(chapters) and all(item.ready for item in chapters),
                chapters=chapters,
            )
            return response.model_dump(mode="python")

    return {
        "get_post_canon_maintenance_status": get_post_canon_maintenance_status,
    }


def _chapter_info(
    commit: CanonCommitRecord,
    rows: list[PostCanonMaintenanceRun],
    *,
    band_checkpoint_action: str = "pause_on_warn",
    checkpoint_status: str | None = None,
    session: Any | None = None,
    now: datetime | None = None,
) -> PostCanonMaintenanceChapterInfo:
    timestamp = now or datetime.now(timezone.utc)
    by_step = {row.step_name: row for row in rows}
    phase3_complete = post_canon_phase3_complete(rows)
    controls = post_canon_order_controls(rows)
    controls_status = str(controls.get("status") or "pending")
    controls_complete = controls_status == "succeeded"
    control_result = post_canon_control_result(rows)
    blockers = post_canon_barrier_blockers(
        rows,
        session=session,
        checkpoint_status=(
            str(checkpoint_status or "").strip()
            if checkpoint_status is not None
            else None
        ),
        band_checkpoint_action=band_checkpoint_action,
    )
    resolved_checkpoint_status = (
        str(checkpoint_status or "").strip()
        if checkpoint_status is not None
        else _checkpoint_status_from_result(control_result)
    )
    blockers = list(dict.fromkeys(blockers))
    ready = phase3_complete and controls_complete and not blockers
    statuses = {row.status for row in rows}
    ordered_rows = [by_step[name] for name in POST_CANON_STEP_NAMES if name in by_step]
    ordered_rows.extend(
        sorted(
            (row for row in rows if row.step_name not in POST_CANON_STEP_NAMES),
            key=lambda row: (row.step_name, row.id),
        )
    )
    run_infos = [_run_info(row, now=timestamp) for row in ordered_rows]
    has_live_run = any(item.lease_active for item in run_infos)
    has_stale_run = any(item.reclaimable for item in run_infos)
    if ready:
        status = "ready"
    elif blockers:
        status = "blocked"
    elif "failed" in statuses or controls_status == "failed":
        status = "failed"
    elif has_live_run:
        status = "running"
    elif has_stale_run:
        status = "stale"
    else:
        status = "pending"

    return PostCanonMaintenanceChapterInfo(
        canon_commit_id=commit.id,
        candidate_id=commit.candidate_id,
        chapter_number=int(commit.chapter_number or 0),
        status=status,
        phase3_complete=phase3_complete,
        controls_complete=controls_complete,
        order_controls_status=controls_status,
        checkpoint_status=resolved_checkpoint_status,
        ready=ready,
        blocking_reasons=blockers,
        runs=run_infos,
    )


def _run_info(
    row: PostCanonMaintenanceRun,
    *,
    now: datetime,
) -> PostCanonMaintenanceRunInfo:
    running = row.status == "running"
    lease_active = bool(
        running
        and row.lease_expires_at is not None
        and _datetime_after(row.lease_expires_at, now)
    )
    return PostCanonMaintenanceRunInfo(
        id=row.id,
        step_name=row.step_name,
        status=row.status,
        attempts=int(row.attempts or 0),
        worker_id=row.worker_id,
        lease_epoch=int(row.lease_epoch or 0),
        lease_expires_at=row.lease_expires_at,
        lease_active=lease_active,
        reclaimable=running and not lease_active,
        heartbeat_at=row.heartbeat_at,
        last_error=row.last_error,
        result=_load_object(row.result_json),
        started_at=row.started_at,
        completed_at=row.completed_at,
        updated_at=row.updated_at,
    )


def _live_checkpoint_status(
    session: Any,
    rows: list[PostCanonMaintenanceRun],
) -> str | None:
    result = post_canon_control_result(rows)
    checkpoint = result.get("checkpoint")
    if not isinstance(checkpoint, dict):
        return None
    checkpoint_id = str(checkpoint.get("id") or "").strip()
    if checkpoint_id:
        current = session.get(BandCheckpoint, checkpoint_id)
        if current is not None:
            return str(current.status or "").strip()
    return str(checkpoint.get("status") or "").strip()


def _checkpoint_status_from_result(result: dict[str, Any]) -> str:
    checkpoint = result.get("checkpoint")
    if not isinstance(checkpoint, dict):
        return ""
    return str(checkpoint.get("status") or "").strip()


def _datetime_after(left: datetime, right: datetime) -> bool:
    if left.tzinfo is None and right.tzinfo is not None:
        right = right.replace(tzinfo=None)
    elif left.tzinfo is not None and right.tzinfo is None:
        right = right.replace(tzinfo=left.tzinfo)
    return left > right


def _load_object(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}


__all__ = ["build_handlers"]
