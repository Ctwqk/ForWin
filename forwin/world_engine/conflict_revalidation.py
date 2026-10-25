"""Expire the open world conflicts a historical rewrite left without ground; no model is asked.

A rewrite of the latest chapter retires the tick that ran after it (``retired_tick_ids`` in the commit's
historical_rewrite result); the tick the chapter's world step runs in its place expires the open conflicts
the retired tick raised in its commit section, after its LLM calls. There it also expires the open chapter
conflicts over the next chapter's draft when that chapter is drafted or in needs_review: the draft was reviewed
against the replaced text, so the next chapter's review is retried. A rewrite of an earlier chapter keeps the
later ticks; its world step expires each open tick conflict, detected after the rewritten chapter or later,
whose held change no longer stands against the latest canon, along with conflicts over later reviewed drafts,
and stops without asking any model. Each expiry records a WORLD_CONFLICT_EXPIRED event with its reason.

The conflict rows are locked before the project row, as a resolve locks them, and only until the world
step commits.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from forwin.audit.events import DecisionEventType
from forwin.book_state.knowledge import FACT_REF_PREFIX
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.repository import BookStateRepository
from forwin.book_state.runtime import BookStateRuntime
from forwin.models.audit import DecisionEvent
from forwin.models.project import ChapterPlan, Project
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
# The next chapter's statuses in which its draft was reviewed against the latest chapter's text.
_REVIEWED_DRAFT_STATUSES = ("drafted", "needs_review")


def rewritten_chapter_reason(chapter_number: int) -> str:
    """Why the next chapter's open chapter conflicts expire once chapter ``chapter_number`` is rewritten."""
    return (
        f"chapter {chapter_number} rewritten; chapter {chapter_number + 1}'s draft was reviewed against the "
        f"replaced text, so retry its review"
    )


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


def expire_next_chapter_conflicts(session: Session, *, project_id: str, chapter_number: int) -> list[str]:
    """Expire the open chapter_extraction conflicts over chapter ``chapter_number + 1`` after a rewrite of chapter
    ``chapter_number``, the latest canon chapter, while the next chapter is drafted or in needs_review; their ids,
    oldest first.

    It reads without a lock first and locks only the rows it expires; the caller takes the project row after them.
    """
    following = chapter_number + 1
    status = session.scalar(select(ChapterPlan.status).where(
        ChapterPlan.project_id == project_id, ChapterPlan.chapter_number == following,
    ))
    if status not in _REVIEWED_DRAFT_STATUSES:
        return []
    open_rows = (
        WorldPlanConflictRow.project_id == project_id,
        WorldPlanConflictRow.source == "chapter_extraction",
        WorldPlanConflictRow.blocks_from_chapter == following,
        WorldPlanConflictRow.status == "open",
    )
    if session.scalar(select(WorldPlanConflictRow.id).where(*open_rows).limit(1)) is None:
        return []
    rows = list(session.scalars(
        select(WorldPlanConflictRow).where(*open_rows)
        .order_by(WorldPlanConflictRow.created_at, WorldPlanConflictRow.id)
        .with_for_update().execution_options(populate_existing=True)
    ))
    repo = WorldConflictRepository(session)
    for row in rows:
        _expire(session, repo, row, reason=rewritten_chapter_reason(chapter_number), chapter_number=chapter_number)
    return [row.id for row in rows]


def expire_stale_open_conflicts(
    session: Session, *, project_id: str, from_chapter: int, policy: RuntimePolicy, reason: str,
) -> list[str]:
    """Expire stale world_tick conflicts and reviewed draft conflicts after a rewrite; their ids, oldest first.

    Chapter extraction conflicts blocking reviewed drafts after ``from_chapter`` lose their review basis.
    For world ticks, the stored held change must still parse, may not set the status of an intent canon has since
    settled (Ruling P22), must pass the running-state check against the latest canon (Ruling P30), and
    every ref the conflict involves must still exist. The first that fails expires the conflict with
    ``reason`` and what failed: a resolve could carry out nothing but a veto of it any more.
    """
    rows = list(session.scalars(
        select(WorldPlanConflictRow)
        .where(
            WorldPlanConflictRow.project_id == project_id,
            WorldPlanConflictRow.status == "open",
            or_(
                and_(WorldPlanConflictRow.source == "world_tick",
                     WorldPlanConflictRow.detected_after_chapter >= from_chapter),
                and_(WorldPlanConflictRow.source == "chapter_extraction",
                     WorldPlanConflictRow.blocks_from_chapter > from_chapter,
                     select(ChapterPlan.id).where(
                         ChapterPlan.project_id == project_id,
                         ChapterPlan.chapter_number == WorldPlanConflictRow.blocks_from_chapter,
                         ChapterPlan.status.in_(_REVIEWED_DRAFT_STATUSES),
                     ).exists()),
            ),
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
    runtime = (BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=latest)
               if any(row.source == "world_tick" for row in rows) else None)
    repo = WorldConflictRepository(session)
    expired = []
    for row in rows:
        if row.source == "chapter_extraction":
            status = session.scalar(select(ChapterPlan.status).where(
                ChapterPlan.project_id == project_id, ChapterPlan.chapter_number == row.blocks_from_chapter,
            ))
            failure = (f"chapter {row.blocks_from_chapter}'s draft was reviewed against replaced text; retry its review"
                       if status in _REVIEWED_DRAFT_STATUSES else "")
        else:
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


__all__ = [
    "RETIRED_TICK_REASON",
    "expire_next_chapter_conflicts",
    "expire_retired_tick_conflicts",
    "expire_stale_open_conflicts",
    "rewritten_chapter_reason",
]
