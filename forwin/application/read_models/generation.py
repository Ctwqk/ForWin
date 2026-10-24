from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Callable

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.api_schema import (
    GenerationControlInfo,
)
from forwin.planning.checkpoints import (
    BlockingReasonInfo,
    chapter_blocking_message,
)
from forwin.audit.events import (
    DecisionEventInfo,
    DecisionEventType,
)
from forwin.models.planning_control import BandCheckpoint
from forwin.generation.continue_workset import writable_chapter_ranges
from forwin.models.project import ChapterPlan, Project
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.world_engine.conflict_repository import WorldConflictRepository
from .arc_snapshot import _band_checkpoint_detail


DisplayDatetime = Callable[[datetime | None], str]
_PROJECT_SUMMARY_CHAPTER_PREVIEW_LIMIT = 3


class WorldConflictBlockInfo(BaseModel):
    conflict_id: str
    blocks_from_chapter: int
    summary: str
    created_at: datetime = datetime.min


def load_open_world_conflicts(
    session: Session, project_id: str
) -> list[WorldConflictBlockInfo]:
    return [
        WorldConflictBlockInfo(
            conflict_id=row.id,
            blocks_from_chapter=row.blocks_from_chapter,
            summary=row.summary,
            created_at=row.created_at,
        )
        for row in WorldConflictRepository(session).list(project_id, status="open")
        if row.required_decider == "human"
    ]


def load_open_world_conflicts_by_project(
    session: Session, project_ids: list[str]
) -> dict[str, list[WorldConflictBlockInfo]]:
    if not project_ids:
        return {}
    rows = session.execute(
        select(
            WorldPlanConflictRow.id,
            WorldPlanConflictRow.project_id,
            WorldPlanConflictRow.blocks_from_chapter,
            WorldPlanConflictRow.summary,
            WorldPlanConflictRow.created_at,
        )
        .where(
            WorldPlanConflictRow.project_id.in_(project_ids),
            WorldPlanConflictRow.status == "open",
            WorldPlanConflictRow.required_decider == "human",
        )
        .order_by(
            WorldPlanConflictRow.blocks_from_chapter,
            WorldPlanConflictRow.created_at,
            WorldPlanConflictRow.id,
        )
    ).all()
    grouped: dict[str, list[WorldConflictBlockInfo]] = {
        project_id: [] for project_id in project_ids
    }
    for row in rows:
        grouped[row.project_id].append(
            WorldConflictBlockInfo(
                conflict_id=row.id,
                blocks_from_chapter=row.blocks_from_chapter,
                summary=row.summary,
                created_at=row.created_at,
            )
        )
    return grouped


def _derive_blocking_reason(
    *,
    plans: list[ChapterPlan],
    latest_band_checkpoint: BandCheckpoint | None,
    decision_events: list[DecisionEventInfo] | None = None,
    future_constraints_enabled: bool = True,
    open_world_conflicts: Sequence[WorldConflictBlockInfo] = (),
    next_chapter: int = 0,
) -> BlockingReasonInfo:
    blocking_plan = next(
        (
            plan
            for plan in sorted(plans, key=lambda item: item.chapter_number)
            if plan.status in {"planned", "drafted", "needs_review", "failed"}
        ),
        None,
    )
    conflict_chapter = (
        blocking_plan.chapter_number if blocking_plan is not None else next_chapter
    )
    if conflict_chapter > 0:
        conflict = min(
            (
                item
                for item in open_world_conflicts
                if item.blocks_from_chapter <= conflict_chapter
            ),
            key=lambda item: (item.blocks_from_chapter, item.created_at, item.conflict_id),
            default=None,
        )
        if conflict is not None:
            return BlockingReasonInfo(
                code="world_conflict_pending",
                message=chapter_blocking_message("world_conflict_pending"),
                chapter_number=conflict_chapter,
                detail=f"conflict_id={conflict.conflict_id}: {conflict.summary}",
            )

    ordered_events = list(decision_events or [])
    fallback_event_id = (
        str(getattr(ordered_events[0], "id", "") or "") if ordered_events else ""
    )

    def _event_payload(event) -> dict[str, Any]:  # noqa: ANN001
        payload = getattr(event, "payload", None)
        if isinstance(payload, dict):
            return payload
        try:
            parsed = json.loads(getattr(event, "payload_json", "") or "{}")
        except (json.JSONDecodeError, TypeError):
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}

    def _latest_event_id_for_chapter(chapter_number: int) -> str:
        for event in ordered_events:
            if int(getattr(event, "chapter_number", 0) or 0) == int(
                chapter_number or 0
            ):
                return str(getattr(event, "id", "") or "")
        return fallback_event_id

    def _latest_event_id_for_related_object(
        related_object_type: str, related_object_id: str
    ) -> str:
        if not related_object_type or not related_object_id:
            return fallback_event_id
        for event in ordered_events:
            if (
                str(getattr(event, "related_object_type", "") or "")
                == related_object_type
                and str(getattr(event, "related_object_id", "") or "")
                == related_object_id
            ):
                return str(getattr(event, "id", "") or "")
        return fallback_event_id

    def _latest_event_id_for_band(band_id: str) -> str:
        if not band_id:
            return fallback_event_id
        for event in ordered_events:
            if str(getattr(event, "band_id", "") or "") == band_id:
                return str(getattr(event, "id", "") or "")
        return fallback_event_id

    def _latest_future_constraint_event(
        chapter_number: int,
    ) -> DecisionEventInfo | None:
        for event in ordered_events:
            if int(getattr(event, "chapter_number", 0) or 0) != int(
                chapter_number or 0
            ):
                continue
            payload = _event_payload(event)
            if (
                str(getattr(event, "event_type", "") or "")
                == DecisionEventType.HARD_GATE_HIT
                and str(payload.get("blocking_reason") or "")
                == "future_constraint_block"
            ):
                return event
            if (
                str(getattr(event, "event_type", "") or "")
                != DecisionEventType.REVIEW_VERDICT_RECORDED
            ):
                continue
            issue_types = payload.get("issue_types") or []
            if not isinstance(issue_types, list):
                continue
            if "future_constraint" not in {str(item or "") for item in issue_types}:
                continue
            if str(payload.get("verdict") or "").strip() != "fail":
                continue
            return event
        return None

    constraint_block_plan = (
        next(
            (
                plan
                for plan in sorted(plans, key=lambda item: item.chapter_number)
                if plan.status in {"needs_review", "failed"}
                and _latest_future_constraint_event(plan.chapter_number) is not None
            ),
            None,
        )
        if future_constraints_enabled
        else None
    )
    if constraint_block_plan is not None:
        event = _latest_future_constraint_event(constraint_block_plan.chapter_number)
        detail = ""
        if event is not None:
            detail = str(getattr(event, "summary", "") or "").strip()
            if not detail:
                detail = str(_event_payload(event).get("error_summary") or "").strip()
        return BlockingReasonInfo(
            code="future_constraint_block",
            message=chapter_blocking_message("future_constraint_block"),
            chapter_number=constraint_block_plan.chapter_number,
            decision_event_id=str(
                getattr(event, "id", "")
                if event is not None
                else _latest_event_id_for_chapter(constraint_block_plan.chapter_number)
            ),
            detail=detail
            or f"第 {constraint_block_plan.chapter_number} 章命中了 hard future constraint。",
        )

    if blocking_plan is not None and blocking_plan.chapter_number > 1:
        previous_plan = next(
            (
                plan
                for plan in plans
                if plan.chapter_number == blocking_plan.chapter_number - 1
            ),
            None,
        )
        if previous_plan is not None and previous_plan.status != "accepted":
            return BlockingReasonInfo(
                code="chapter_not_canon",
                message=chapter_blocking_message(
                    "chapter_not_canon",
                    chapter_number=previous_plan.chapter_number,
                    band_id="",
                ),
                chapter_number=previous_plan.chapter_number,
                decision_event_id=_latest_event_id_for_chapter(
                    previous_plan.chapter_number
                ),
                detail=f"章节 {previous_plan.chapter_number} 当前状态为 {previous_plan.status}。",
            )
    if blocking_plan is None or latest_band_checkpoint is None:
        return BlockingReasonInfo()
    code_map = {
        "pending": "band_checkpoint_pending",
        "warn": "band_checkpoint_warn",
        "fail": "band_checkpoint_fail",
        "error": "band_checkpoint_fail",
    }
    code = code_map.get(str(latest_band_checkpoint.status or ""))
    if not code:
        return BlockingReasonInfo()
    return BlockingReasonInfo(
        code=code,
        message=chapter_blocking_message(
            code,
            chapter_number=latest_band_checkpoint.boundary_chapter,
            band_id=latest_band_checkpoint.band_id,
        ),
        chapter_number=latest_band_checkpoint.boundary_chapter,
        band_id=latest_band_checkpoint.band_id,
        decision_event_id=(
            _latest_event_id_for_related_object(
                "band_checkpoint", str(latest_band_checkpoint.id or "")
            )
            or _latest_event_id_for_band(str(latest_band_checkpoint.band_id or ""))
            or fallback_event_id
        ),
        detail=str(
            latest_band_checkpoint.summary or latest_band_checkpoint.reason or ""
        ).strip(),
    )


def _derive_next_gate(
    *,
    plans: list[ChapterPlan],
    blocking_reason: BlockingReasonInfo,
) -> str:
    if blocking_reason.code:
        return blocking_reason.code
    for plan in sorted(plans, key=lambda item: item.chapter_number):
        if plan.status == "accepted":
            continue
        chapter_number = int(plan.chapter_number or 0)
        if plan.status == "drafted":
            return f"chapter_{chapter_number}_accept"
        if plan.status == "needs_review":
            return f"chapter_{chapter_number}_review"
        if plan.status == "failed":
            return f"chapter_{chapter_number}_rewrite"
        return f"chapter_{chapter_number}_write"
    return "completed"


def effective_target_total_chapters(project: Project, chapter_count: int) -> int:
    stored = int(getattr(project, "target_total_chapters", 0) or 0)
    planned = max(0, int(chapter_count or 0))
    if stored <= 0:
        return max(3, planned)
    return max(stored, planned)


def load_writable_chapter_ranges(
    session: Session, project_ids: list[str]
) -> dict[str, range]:
    """Map writing projects to their chapters to write, as the continue workset counts them.

    The chapters in range that have no plan row yet (Genesis future arcs, extend-generation,
    bands not planned) are written by a continue run, which plans each band first.
    """
    ids = [str(item or "").strip() for item in project_ids if str(item or "").strip()]
    if not ids:
        return {}
    writing = list(
        session.scalars(
            select(Project.id).where(
                Project.id.in_(ids), Project.creation_status == "writing"
            )
        )
    )
    return {
        project_id: chapters
        for project_id, chapters in writable_chapter_ranges(session, writing).items()
        if chapters
    }


def build_generation_control(
    *,
    plans: list[ChapterPlan],
    review_interval_chapters: int = 0,
    active_stage: str = "",
    active_chapter: int = 0,
    pause_requested: bool = False,
    can_pause: bool = False,
    latest_band_checkpoint: BandCheckpoint | None = None,
    decision_events: list[DecisionEventInfo] | None = None,
    future_constraints_enabled: bool = True,
    writable_chapters: range = range(0),
    open_world_conflicts: Sequence[WorldConflictBlockInfo] = (),
) -> GenerationControlInfo:
    accepted = [plan.chapter_number for plan in plans if plan.status == "accepted"]
    drafted = [plan.chapter_number for plan in plans if plan.status == "drafted"]
    planned = [plan.chapter_number for plan in plans if plan.status == "planned"]
    failed = [plan.chapter_number for plan in plans if plan.status == "failed"]
    pending_review = [
        plan.chapter_number for plan in plans if plan.status == "needs_review"
    ]
    generated = [
        plan.chapter_number
        for plan in plans
        if plan.status in {"drafted", "accepted", "needs_review"}
    ]
    next_candidates = planned + failed
    next_chapter = min(next_candidates) if next_candidates else 0
    # As the continue workset counts: the first writable chapter not accepted. Without
    # a plan row it waits for run control to plan its band.
    statuses = {plan.chapter_number: plan.status for plan in plans}
    first_unwritten = next(
        (number for number in writable_chapters if statuses.get(number) != "accepted"),
        0,
    )
    pending_band_plan = (
        not next_candidates and first_unwritten > 0 and first_unwritten not in statuses
    )
    if pending_band_plan:
        next_chapter = first_unwritten
    if pending_review:
        plan_state = "needs_review"
    elif drafted:
        plan_state = "pending_acceptance"
    elif pending_band_plan:
        plan_state = "pending_band_plan"
    elif not plans:
        plan_state = "none"
    elif len(accepted) == len(plans):
        plan_state = "completed"
    elif failed and not planned:
        plan_state = "failed"
    else:
        plan_state = "in_progress"
    writing_state = "not_started"
    if generated:
        writing_state = (
            "completed"
            if len(accepted) == len(plans) and not pending_band_plan
            else "started"
        )
    if pending_review:
        review_state = "pending"
    elif drafted:
        review_state = "pending_acceptance"
    else:
        review_state = "none"
    review_interval = max(0, int(review_interval_chapters or 0))
    chapters_until_review = 0
    if review_interval and not pending_review:
        completed_since_review = len(accepted) % review_interval
        chapters_until_review = (
            review_interval - completed_since_review
            if completed_since_review
            else review_interval
        )
    blocking_reason = _derive_blocking_reason(
        plans=plans,
        latest_band_checkpoint=latest_band_checkpoint,
        decision_events=decision_events,
        future_constraints_enabled=future_constraints_enabled,
        open_world_conflicts=open_world_conflicts,
        next_chapter=next_chapter,
    )
    can_resume = bool(
        (next_candidates or pending_band_plan)
        and not pending_review
        and not drafted
        and blocking_reason.code != "world_conflict_pending"
    )
    next_gate = _derive_next_gate(plans=plans, blocking_reason=blocking_reason)
    if pending_band_plan and next_gate == "completed":
        next_gate = f"chapter_{next_chapter}_write"
    return GenerationControlInfo(
        plan_state=plan_state,
        writing_state=writing_state,
        review_state=review_state,
        current_stage=active_stage,
        current_chapter=int(active_chapter or max(generated + failed, default=0)),
        next_chapter=next_chapter,
        accepted_chapters=accepted,
        drafted_chapters=drafted,
        generated_chapters=generated,
        planned_chapters=planned,
        failed_chapters=failed,
        pending_review_chapters=pending_review,
        can_pause=can_pause,
        can_resume=can_resume,
        pause_requested=bool(pause_requested),
        review_interval_chapters=review_interval,
        chapters_until_review=chapters_until_review,
        blocking_reason=blocking_reason,
        latest_band_checkpoint=_band_checkpoint_detail(latest_band_checkpoint),
        next_gate=next_gate,
    )


__all__ = [
    "WorldConflictBlockInfo",
    "load_open_world_conflicts",
    "load_open_world_conflicts_by_project",
    "_derive_blocking_reason",
    "_derive_next_gate",
    "effective_target_total_chapters",
    "load_writable_chapter_ranges",
    "build_generation_control",
]
