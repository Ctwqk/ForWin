from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from sqlalchemy import select

from forwin.maintenance.events import ORDER_CONTROLS_KEY, POST_CANON_STEP_NAMES


def post_canon_phase3_complete(rows: Iterable[Any]) -> bool:
    materialized = list(rows)
    if len(materialized) != len(POST_CANON_STEP_NAMES):
        return False
    by_step = {str(row.step_name): row for row in materialized}
    if set(by_step) != set(POST_CANON_STEP_NAMES):
        return False
    return all(
        str(by_step[name].status) == "succeeded" for name in POST_CANON_STEP_NAMES
    )


def post_canon_order_controls(rows: Iterable[Any]) -> dict[str, Any]:
    feedback = next(
        (row for row in rows if str(row.step_name) == "feedback"),
        None,
    )
    if feedback is None:
        return {}
    payload = _load_object(getattr(feedback, "result_json", "{}"))
    controls = payload.get(ORDER_CONTROLS_KEY)
    return dict(controls) if isinstance(controls, dict) else {}


def post_canon_control_result(rows: Iterable[Any]) -> dict[str, Any]:
    controls = post_canon_order_controls(rows)
    result = controls.get("result")
    return dict(result) if isinstance(result, dict) else {}


def post_canon_control_blockers(result: Mapping[str, Any]) -> list[str]:
    raw = result.get("blocking_reasons")
    if not isinstance(raw, list):
        future_audit = result.get("future_plan_audit")
        raw = (
            future_audit.get("blocking_reasons")
            if isinstance(future_audit, Mapping)
            else None
        )
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def post_canon_checkpoint_status(
    rows: Iterable[Any],
    *,
    session: Any | None = None,
    checkpoint_status: str | None = None,
) -> str:
    if checkpoint_status is not None:
        return str(checkpoint_status or "").strip()
    result = post_canon_control_result(rows)
    checkpoint = result.get("checkpoint")
    if not isinstance(checkpoint, Mapping):
        return ""
    checkpoint_id = str(checkpoint.get("id") or "").strip()
    if checkpoint_id and session is not None:
        from forwin.canon.projection_lock import lock_projection_project
        from forwin.models.planning_control import BandCheckpoint
        from forwin.review.plan_checks import BandCheckpointEvaluator

        current = session.get(BandCheckpoint, checkpoint_id)
        if current is not None:
            lock_projection_project(session, current.project_id)
            session.refresh(current)
            return BandCheckpointEvaluator(session).inspect(current).effective_status
        return "pending"
    return str(checkpoint.get("status") or "").strip()


def post_canon_checkpoint_blockers(
    rows: Iterable[Any],
    *,
    session: Any | None = None,
    checkpoint_status: str | None = None,
    band_checkpoint_action: str = "pause_on_warn",
) -> list[str]:
    status = post_canon_checkpoint_status(
        rows,
        session=session,
        checkpoint_status=checkpoint_status,
    )
    if not status or status in {"pass", "overridden"}:
        return []
    if status == "warn" and band_checkpoint_action == "continue":
        return []
    return [f"band_checkpoint_{status}"]


def post_canon_manual_checkpoint_blockers(
    rows: Iterable[Any],
    *,
    session: Any | None = None,
    project_id: str = "",
    chapter_number: int = 0,
) -> list[str]:
    materialized = list(rows)
    if session is None:
        return []
    if materialized:
        project_id = str(getattr(materialized[0], "project_id", "") or "").strip()
        chapter_number = int(getattr(materialized[0], "chapter_number", 0) or 0)
    if not project_id or chapter_number < 1:
        return []
    from forwin.models.planning_control import BandCheckpoint

    kinds = session.execute(
        select(BandCheckpoint.boundary_kind).where(
            BandCheckpoint.project_id == project_id,
            BandCheckpoint.trigger_source == "manual_boundary",
            BandCheckpoint.status == "pending",
            BandCheckpoint.boundary_kind.in_(("chapter_accepted", "band_end")),
            BandCheckpoint.boundary_chapter == chapter_number,
        )
    ).scalars()
    return [
        f"manual_checkpoint_{kind}_pending"
        for kind in sorted({str(item or "").strip() for item in kinds if item})
    ]


def post_canon_barrier_blockers(
    rows: Iterable[Any],
    *,
    session: Any | None = None,
    checkpoint_status: str | None = None,
    band_checkpoint_action: str = "pause_on_warn",
) -> list[str]:
    materialized = list(rows)
    blockers = post_canon_control_blockers(post_canon_control_result(materialized))
    blockers.extend(
        post_canon_checkpoint_blockers(
            materialized,
            session=session,
            checkpoint_status=checkpoint_status,
            band_checkpoint_action=band_checkpoint_action,
        )
    )
    blockers.extend(
        post_canon_manual_checkpoint_blockers(
            materialized,
            session=session,
        )
    )
    return list(dict.fromkeys(blockers))


def post_canon_barrier_ready(
    rows: Iterable[Any],
    *,
    session: Any | None = None,
    checkpoint_status: str | None = None,
    band_checkpoint_action: str = "pause_on_warn",
) -> bool:
    materialized = list(rows)
    if not post_canon_phase3_complete(materialized):
        return False
    controls = post_canon_order_controls(materialized)
    if controls.get("status") != "succeeded":
        return False
    return not post_canon_barrier_blockers(
        materialized,
        session=session,
        checkpoint_status=checkpoint_status,
        band_checkpoint_action=band_checkpoint_action,
    )


def post_canon_continuation_state(
    rows: Iterable[Any],
    *,
    session: Any,
    commit: Any,
    band_checkpoint_action: str = "pause_on_warn",
) -> Literal["ready", "waiting", "blocked", "invalid"]:
    """Separate durable maintenance work from semantic stops, before outbox ack."""
    from forwin.canon.outbox_events import (
        CANON_PHASE3_REQUESTED,
        canon_event_id,
        parse_canon_event_envelope,
    )
    from forwin.models.maintenance import PostCanonMaintenanceRun
    from forwin.models.outbox import OutboxEvent

    intent = session.scalar(
        select(OutboxEvent).where(
            OutboxEvent.event_id
            == canon_event_id(commit.idempotency_key, CANON_PHASE3_REQUESTED)
        )
    )
    materialized = list(rows)
    if intent is None or intent.status not in {"pending", "running"}:
        # An owner may have completed between the caller's row read and this
        # intent read. Re-read completion before declaring recovery unavailable.
        materialized = list(
            session.scalars(
                select(PostCanonMaintenanceRun)
                .where(PostCanonMaintenanceRun.canon_commit_id == commit.id)
                .execution_options(populate_existing=True)
            )
        )
    if post_canon_barrier_blockers(
        materialized,
        session=session,
        band_checkpoint_action=band_checkpoint_action,
    ) or post_canon_manual_checkpoint_blockers(
        [],
        session=session,
        project_id=commit.project_id,
        chapter_number=commit.chapter_number,
    ):
        return "blocked"
    names = [str(row.step_name) for row in materialized]
    if len(set(names)) != len(names) or set(names) - set(POST_CANON_STEP_NAMES):
        return "invalid"
    if any(
        row.status not in {"pending", "running", "failed", "succeeded"}
        for row in materialized
    ):
        return "invalid"
    for row in materialized:
        try:
            result = json.loads(row.result_json or "{}")
        except (ValueError, TypeError):
            return "invalid"
        if not isinstance(result, dict):
            return "invalid"
        if row.step_name == "feedback" and ORDER_CONTROLS_KEY in result:
            controls = result[ORDER_CONTROLS_KEY]
            if (
                isinstance(controls, dict)
                and "result" in controls
                and not isinstance(controls["result"], dict)
            ):
                return "invalid"
            if not isinstance(controls, dict) or controls.get("status") not in {
                "pending",
                "running",
                "failed",
                "succeeded",
                "blocked",
            }:
                return "invalid"
    controls = post_canon_order_controls(materialized)
    if controls.get("status") == "blocked":
        return "blocked"
    if (
        post_canon_phase3_complete(materialized)
        and controls.get("status") == "succeeded"
    ):
        return "ready"
    # Missing rows are normal before first delivery, but every wait needs a
    # live durable actor. Never resurrect historical acknowledged events here.
    if intent is None or intent.status not in {"pending", "running"}:
        return "invalid"
    try:
        parsed = parse_canon_event_envelope(
            event_type=intent.event_type,
            event_id=intent.event_id,
            aggregate_type=intent.aggregate_type,
            aggregate_id=intent.aggregate_id,
            payload=json.loads(intent.payload_json),
        )
    except (ValueError, TypeError):
        return "invalid"
    if (
        parsed.canon_commit_id != commit.id
        or parsed.project_id != commit.project_id
        or parsed.chapter_number != commit.chapter_number
        or parsed.candidate_id != commit.candidate_id
    ):
        return "invalid"
    return "waiting"


def _load_object(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}


__all__ = [
    "post_canon_barrier_blockers",
    "post_canon_barrier_ready",
    "post_canon_checkpoint_blockers",
    "post_canon_checkpoint_status",
    "post_canon_control_blockers",
    "post_canon_control_result",
    "post_canon_manual_checkpoint_blockers",
    "post_canon_order_controls",
    "post_canon_phase3_complete",
]
