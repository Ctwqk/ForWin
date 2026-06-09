from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from forwin.governance import DecisionEventInfo, DecisionEventType
from forwin.models.governance import DecisionEvent
from forwin.models.project import ChapterPlan
from forwin.state.updater import StateUpdater


AUTO_REVIEW_RETRY_SOURCES = {
    "auto_continue_review_retry",
    "review_approve_auto_retry",
}


def chapter_numbers(raw_values: Any) -> list[int]:
    numbers: list[int] = []
    for raw in raw_values or []:
        try:
            number = int(raw)
        except (TypeError, ValueError):
            continue
        if number > 0:
            numbers.append(number)
    return numbers


def eligible_for_auto_review_retry(
    plan: ChapterPlan,
    system_block_chapters: set[int] | None = None,
) -> bool:
    chapter_number = int(getattr(plan, "chapter_number", 0) or 0)
    if int(getattr(plan, "repair_attempt_count", 0) or 0) > 0:
        return False
    if chapter_number in (system_block_chapters or set()):
        return True
    return str(getattr(plan, "canon_risk_level", "") or "") == "high"


def prior_auto_review_retry_count(
    session: Any,
    project_id: str,
    chapter_number: int,
) -> int:
    events = session.execute(
        select(DecisionEvent).where(
            DecisionEvent.project_id == project_id,
            DecisionEvent.chapter_number == int(chapter_number),
            DecisionEvent.event_family == "audit_action",
            DecisionEvent.event_type == DecisionEventType.RETRY_ATTEMPT,
            DecisionEvent.actor_type == "system",
        )
    ).scalars()
    count = 0
    for event in events:
        try:
            payload = json.loads(str(event.payload_json or "{}"))
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict) and payload.get("source") in AUTO_REVIEW_RETRY_SOURCES:
            count += 1
    return count


def reset_chapter_for_auto_review_retry(
    session: Any,
    *,
    project_id: str,
    chapter_number: int,
    plan: ChapterPlan,
    task_id: str = "",
    source: str,
    reason: str,
    summary: str,
    terminal_block_reason: str = "",
    system_block: bool = False,
    frozen_artifact: str = "",
) -> None:
    plan.status = "planned"
    plan.acceptance_mode = ""
    plan.repair_attempt_count = 0
    plan.residual_review_issues_json = "[]"
    plan.canon_risk_level = ""
    session.add(plan)

    StateUpdater(session).save_decision_event(
        DecisionEventInfo(
            project_id=project_id,
            task_id=task_id,
            chapter_number=chapter_number,
            scope="chapter",
            event_family="audit_action",
            event_type=DecisionEventType.RETRY_ATTEMPT,
            actor_type="system",
            summary=summary,
            reason=reason,
            payload={
                "source": source,
                "chapter_number": chapter_number,
                "terminal_block_reason": terminal_block_reason,
                "system_block": system_block,
                "frozen_artifact": frozen_artifact,
            },
            related_object_type="chapter",
            related_object_id=str(getattr(plan, "id", "") or ""),
        )
    )
