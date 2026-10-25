"""Expire the open world conflicts a historical rewrite left without ground; no model is asked.

A rewrite of the latest chapter retires the tick that ran after it (``retired_tick_ids`` in the commit's
historical_rewrite result); the tick the chapter's world step runs in its place expires the open conflicts
the retired tick raised in its commit section, after its LLM calls. A rewrite of an earlier chapter keeps the
later ticks; its world step expires each open tick conflict, detected after the rewritten chapter or later,
whose held change no longer stands against the latest canon, and stops without asking any model. Each
expiry records a WORLD_CONFLICT_EXPIRED event with its reason.

The conflict rows are locked before the project row, as a resolve locks them, and only until the world
step commits.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.audit.events import DecisionEventType
from forwin.book_state.knowledge import FACT_REF_PREFIX
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.repository import BookStateRepository
from forwin.book_state.runtime import BookStateRuntime
from forwin.models.audit import DecisionEvent
from forwin.models.project import Project
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.protocol.book_state import ObserverType
from forwin.runtime.policy import RuntimePolicy

from .conflict_repository import WorldConflictRepository
from .conflict_resolution import (
    ConflictOptionUnavailable,
    ConflictStale,
    _check_intents_unsettled,
    _held_change,
    _resolution_context,
    canon_running_check,
)


# Why a retired tick's open conflicts expire.
RETIRED_TICK_REASON = "latest chapter rewritten; the world step re-ticks"


def expire_retired_tick_conflicts(
    session: Session, *, project_id: str, chapter_number: int, tick_ids: Sequence[str],
) -> list[str]:
    """Expire the open conflicts the retired ``tick_ids`` raised; their ids, oldest first.

    ``chapter_number`` is the rewritten chapter whose world step expires them. A conflict decided since is
    no longer open and is left as it is.
    """
    repo = WorldConflictRepository(session)
    rows = repo.open_by_origin(project_id, tick_ids)
    for row in rows:
        _expire(session, repo, row, reason=RETIRED_TICK_REASON, chapter_number=chapter_number)
    return [row.id for row in rows]


def expire_stale_open_conflicts(
    session: Session, *, project_id: str, from_chapter: int, policy: RuntimePolicy, reason: str,
) -> list[str]:
    """Expire each open world_tick conflict detected after ``from_chapter`` or later whose held change no
    longer stands against the latest canon; their ids, oldest first.

    In order, the stored held change must still parse, may not set the status of an intent canon has since
    settled (Ruling P22), must pass the running-state check against the latest canon (Ruling P30), and
    every ref the conflict involves must still exist. The first that fails expires the conflict with
    ``reason`` and what failed: a resolve could carry out nothing but a veto of it any more.
    """
    rows = list(session.scalars(
        select(WorldPlanConflictRow)
        .where(
            WorldPlanConflictRow.project_id == project_id,
            WorldPlanConflictRow.source == "world_tick",
            WorldPlanConflictRow.status == "open",
            WorldPlanConflictRow.detected_after_chapter >= from_chapter,
        )
        .order_by(WorldPlanConflictRow.created_at, WorldPlanConflictRow.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ))
    if not rows:
        return []
    # FOR NO KEY UPDATE after the conflict rows, as a resolve takes it: canon holds still while it is read.
    session.execute(select(Project.id).where(Project.id == project_id).with_for_update(key_share=True))
    latest = BookStateRepository(session).latest_canon_chapter(project_id) or 0
    runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=latest)
    repo = WorldConflictRepository(session)
    expired = []
    for row in rows:
        failure = _stale_reason(session, row, runtime=runtime, policy=policy)
        if failure:
            _expire(session, repo, row, reason=f"{reason}: {failure}", chapter_number=from_chapter)
            expired.append(row.id)
    return expired


def _stale_reason(
    session: Session, row: WorldPlanConflictRow, *, runtime: BookStateRuntime, policy: RuntimePolicy,
) -> str:
    """Why the conflict's held change no longer stands against ``runtime``, the latest canon; "" while it does."""
    try:
        held = _held_change(row)
        _check_intents_unsettled(row, held, runtime)
    except (ConflictOptionUnavailable, ConflictStale) as exc:
        return str(exc)
    context = _resolution_context(
        session, row.project_id, chapter_number=runtime.as_of_chapter, fallback_now=row.story_time,
    )
    check = canon_running_check(
        session, row.project_id, context=context, runtime=runtime, policy=policy, tick_id=f"conflict-{row.id}",
    )
    issues = check.issues(held)
    if issues:
        details = "; ".join(f"{issue.code}: {issue.message}" for issue in issues)
        return f"the held change no longer holds against canon: {details}"
    missing = [ref for ref in json.loads(row.involved_refs_json or "[]") if not _exists(runtime, ref)]
    if missing:
        return f"involved refs no longer exist: {missing}"
    return ""


def _exists(runtime: BookStateRuntime, ref: str) -> bool:
    if ref == ObserverType.READER.value:
        return True
    if ref.startswith(FACT_REF_PREFIX):
        return ref.removeprefix(FACT_REF_PREFIX) in runtime.world.facts_by_id
    return ref in runtime.world.nodes_by_id or ref in runtime.world.edges_by_id or ref in runtime.map.nodes_by_id


def _expire(
    session: Session, repo: WorldConflictRepository, row: WorldPlanConflictRow, *, reason: str, chapter_number: int,
) -> None:
    repo.expire(row, reason=reason)
    session.add(DecisionEvent(
        project_id=row.project_id,
        chapter_number=chapter_number,
        scope="chapter",
        event_family="business_event",
        event_type=DecisionEventType.WORLD_CONFLICT_EXPIRED,
        actor_type="system",
        summary=f"World conflict {row.id} expired.",
        reason=reason,
        payload_json=json.dumps({
            "conflict_id": row.id,
            "source": row.source,
            "violated": row.violated,
            "origin_key": row.origin_key,
            "detected_after_chapter": row.detected_after_chapter,
            "reason": reason,
        }, ensure_ascii=False, sort_keys=True),
        related_object_type="world_conflict",
        related_object_id=row.id,
    ))


__all__ = ["RETIRED_TICK_REASON", "expire_retired_tick_conflicts", "expire_stale_open_conflicts"]
