from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from forwin.book_state import BookStateRepository
from forwin.api_schemas import (
    BandCheckpointDetail,
    BookGenesisPack,
    BookGenesisStageState,
    ChapterInfo,
    EntityInfo,
    GenerationControlInfo,
    PromptTraceInfo,
    ProjectAutomationSettings,
    ProjectDetail,
    ProjectSummary,
    ProvisionalBandDetail,
    ProvisionalChapterLedgerInfo,
    ScenarioRehearsalDetail,
    ThreadInfo,
)
from forwin.governance import (
    BandCheckpointIssueInfo,
    BlockingReasonInfo,
    DecisionEventInfo,
    NarrativeConstraintInfo,
    ProjectGovernanceSettings,
    DecisionEventType,
    checkpoint_reason_with_legacy_status,
    chapter_blocking_message,
    normalize_checkpoint_status,
    normalize_project_governance,
)
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.entity import Entity
from forwin.models.governance import BandCheckpoint, DecisionEvent, NarrativeConstraint
from forwin.models.phase import (
    ArcEnvelopeAnalysis,
    ArcStructureDraft,
    BandExperiencePlan,
    ProjectReplanEvent,
    ProjectStageAnalysis,
    ProvisionalBandExecution,
    ProvisionalChapterLedger,
)
from forwin.models.phase4 import NPCIntentSnapshot, WorldSimulationTurn
from forwin.models.world_v4 import ScenarioRehearsalRunRow
from forwin.models.genesis import BookGenesisRevision, PromptTrace
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.publisher import PublisherUploadJob
from forwin.models.subworld import SubWorld, SubWorldRosterItem
from forwin.models.thread import PlotThread
from forwin.protocol.review import normalize_repair_scope
from forwin.state.query_helpers import (
    load_latest_active_arc_envelope_by_project,
    load_latest_arc_envelope_analysis_by_project,
    load_latest_drafts_by_plan_id,
    load_latest_provisional_band_execution_by_project,
    load_latest_replan_event_by_project,
    load_latest_rewrite_attempts_by_chapter,
    load_latest_stage_analysis_by_project,
    load_latest_world_turn_by_project,
)
from forwin.world_templates import empty_world_root


DisplayDatetime = Callable[[datetime | None], str]
_GENESIS_STAGE_ORDER = ("brief", "world", "map", "story_engine", "book_blueprint", "bootstrap")
_PROJECT_DETAIL_CHAPTER_PREVIEW_LIMIT = 60
_PROJECT_SUMMARY_CHAPTER_PREVIEW_LIMIT = 3
from .arc_snapshot import _band_checkpoint_detail


def _derive_blocking_reason(
    *,
    plans: list[ChapterPlan],
    latest_band_checkpoint: BandCheckpoint | None,
    decision_events: list[DecisionEventInfo] | None = None,
    future_constraints_enabled: bool = True,
) -> BlockingReasonInfo:
    ordered_events = list(decision_events or [])
    fallback_event_id = str(getattr(ordered_events[0], "id", "") or "") if ordered_events else ""

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
            if int(getattr(event, "chapter_number", 0) or 0) == int(chapter_number or 0):
                return str(getattr(event, "id", "") or "")
        return fallback_event_id

    def _latest_event_id_for_related_object(related_object_type: str, related_object_id: str) -> str:
        if not related_object_type or not related_object_id:
            return fallback_event_id
        for event in ordered_events:
            if (
                str(getattr(event, "related_object_type", "") or "") == related_object_type
                and str(getattr(event, "related_object_id", "") or "") == related_object_id
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

    def _latest_future_constraint_event(chapter_number: int) -> DecisionEventInfo | None:
        for event in ordered_events:
            if int(getattr(event, "chapter_number", 0) or 0) != int(chapter_number or 0):
                continue
            payload = _event_payload(event)
            if str(getattr(event, "event_type", "") or "") == DecisionEventType.HARD_GATE_HIT and str(
                payload.get("blocking_reason") or ""
            ) == "future_constraint_block":
                return event
            if str(getattr(event, "event_type", "") or "") != DecisionEventType.REVIEW_VERDICT_RECORDED:
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
            detail=detail or f"第 {constraint_block_plan.chapter_number} 章命中了 hard future constraint。",
        )

    blocking_plan = next(
        (
            plan
            for plan in sorted(plans, key=lambda item: item.chapter_number)
            if plan.status in {"planned", "drafted", "needs_review", "failed"}
        ),
        None,
    )
    if blocking_plan is not None and blocking_plan.chapter_number > 1:
        previous_plan = next(
            (plan for plan in plans if plan.chapter_number == blocking_plan.chapter_number - 1),
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
                decision_event_id=_latest_event_id_for_chapter(previous_plan.chapter_number),
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
            _latest_event_id_for_related_object("band_checkpoint", str(latest_band_checkpoint.id or ""))
            or _latest_event_id_for_band(str(latest_band_checkpoint.band_id or ""))
            or fallback_event_id
        ),
        detail=str(latest_band_checkpoint.summary or latest_band_checkpoint.reason or "").strip(),
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


def build_generation_control(
    *,
    plans: list[ChapterPlan],
    latest_replan,
    review_interval_chapters: int = 0,
    active_stage: str = "",
    active_chapter: int = 0,
    pause_requested: bool = False,
    can_pause: bool = False,
    latest_band_checkpoint: BandCheckpoint | None = None,
    decision_events: list[DecisionEventInfo] | None = None,
    future_constraints_enabled: bool = True,
) -> GenerationControlInfo:
    accepted = [plan.chapter_number for plan in plans if plan.status == "accepted"]
    drafted = [plan.chapter_number for plan in plans if plan.status == "drafted"]
    planned = [plan.chapter_number for plan in plans if plan.status == "planned"]
    failed = [plan.chapter_number for plan in plans if plan.status == "failed"]
    pending_review = [plan.chapter_number for plan in plans if plan.status == "needs_review"]
    generated = [plan.chapter_number for plan in plans if plan.status in {"drafted", "accepted", "needs_review"}]
    next_candidates = planned + failed
    next_chapter = min(next_candidates) if next_candidates else 0
    if not plans:
        plan_state = "none"
    elif pending_review:
        plan_state = "needs_review"
    elif drafted:
        plan_state = "pending_acceptance"
    elif len(accepted) == len(plans):
        plan_state = "completed"
    elif failed and not planned:
        plan_state = "failed"
    else:
        plan_state = "in_progress"
    writing_state = "not_started"
    if generated:
        writing_state = "completed" if len(accepted) == len(plans) else "started"
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
        chapters_until_review = review_interval - completed_since_review if completed_since_review else review_interval
    chapters_until_replan = 0
    if latest_replan is not None:
        cooldown_until = int(getattr(latest_replan, "cooldown_until_chapter", 0) or 0)
        if cooldown_until:
            chapters_until_replan = max(0, cooldown_until - (max(accepted, default=0) + 1))
    blocking_reason = _derive_blocking_reason(
        plans=plans,
        latest_band_checkpoint=latest_band_checkpoint,
        decision_events=decision_events,
        future_constraints_enabled=future_constraints_enabled,
    )
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
        can_resume=bool(next_candidates and not pending_review and not drafted),
        pause_requested=bool(pause_requested),
        review_interval_chapters=review_interval,
        chapters_until_review=chapters_until_review,
        chapters_until_replan_eligible=chapters_until_replan,
        blocking_reason=blocking_reason,
        latest_band_checkpoint=_band_checkpoint_detail(latest_band_checkpoint),
        next_gate=_derive_next_gate(plans=plans, blocking_reason=blocking_reason),
    )


__all__ = [
    '_derive_blocking_reason',
    '_derive_next_gate',
    'effective_target_total_chapters',
    'build_generation_control',
]
