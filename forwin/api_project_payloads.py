from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

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
    ThreadInfo,
)
from forwin.governance import (
    BandCheckpointIssueInfo,
    BlockingReasonInfo,
    DecisionEventInfo,
    NarrativeConstraintInfo,
    ProjectGovernanceSettings,
    DecisionEventType,
    chapter_blocking_message,
    normalize_project_governance,
)
from forwin.models.draft import ChapterDraft
from forwin.models.entity import Entity
from forwin.models.governance import BandCheckpoint, DecisionEvent, NarrativeConstraint
from forwin.models.phase import (
    ArcEnvelopeAnalysis,
    ArcStructureDraft,
    BandExperiencePlan,
    ChapterRewriteAttempt,
    ProjectReplanEvent,
    ProjectStageAnalysis,
    ProvisionalBandExecution,
    ProvisionalChapterLedger,
)
from forwin.models.phase4 import NPCIntentSnapshot, WorldSimulationTurn
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
    load_latest_stage_analysis_by_project,
    load_latest_world_turn_by_project,
)
from forwin.world_templates import empty_world_root


DisplayDatetime = Callable[[datetime | None], str]
_GENESIS_STAGE_ORDER = ("brief", "world", "map", "story_engine", "book_blueprint", "bootstrap")


def _deep_merge_dict(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _normalized_project_ids(project_ids: list[str]) -> list[str]:
    return [str(project_id or "").strip() for project_id in project_ids if str(project_id or "").strip()]


def _recent_rows_by_project(
    session: Session,
    model,
    project_column,
    project_ids: list[str],
    *,
    order_by: tuple[Any, ...],
    limit: int,
) -> dict[str, list[Any]]:
    ids = _normalized_project_ids(project_ids)
    normalized_limit = max(1, int(limit or 1))
    if not ids:
        return {}
    ranked = (
        select(
            model.id.label("row_id"),
            project_column.label("project_id"),
            func.row_number()
            .over(partition_by=project_column, order_by=order_by)
            .label("rn"),
        )
        .where(project_column.in_(ids))
        .subquery()
    )
    rows = session.execute(
        select(model, ranked.c.rn)
        .join(ranked, model.id == ranked.c.row_id)
        .where(ranked.c.rn <= normalized_limit)
        .order_by(ranked.c.project_id.asc(), ranked.c.rn.asc())
    ).all()
    grouped: dict[str, list[Any]] = defaultdict(list)
    for row, _rn in rows:
        grouped[str(getattr(row, project_column.key) or "")].append(row)
    return dict(grouped)


def _latest_rows_by_project(
    session: Session,
    model,
    project_column,
    project_ids: list[str],
    *,
    order_by: tuple[Any, ...],
) -> dict[str, Any]:
    grouped = _recent_rows_by_project(
        session,
        model,
        project_column,
        project_ids,
        order_by=order_by,
        limit=1,
    )
    return {project_id: rows[0] for project_id, rows in grouped.items() if rows}


def _latest_band_checkpoint_by_project(
    session: Session,
    project_ids: list[str],
) -> dict[str, BandCheckpoint]:
    return _latest_rows_by_project(
        session,
        BandCheckpoint,
        BandCheckpoint.project_id,
        project_ids,
        order_by=(
            BandCheckpoint.created_at.desc(),
            BandCheckpoint.id.desc(),
        ),
    )


def _decision_timeline_by_project(
    session: Session,
    project_ids: list[str],
    *,
    limit: int,
) -> dict[str, list[DecisionEventInfo]]:
    rows_by_project = _recent_rows_by_project(
        session,
        DecisionEvent,
        DecisionEvent.project_id,
        project_ids,
        order_by=(
            DecisionEvent.created_at.desc(),
            DecisionEvent.id.desc(),
        ),
        limit=limit,
    )
    grouped: dict[str, list[DecisionEventInfo]] = defaultdict(list)
    for project_id, rows in rows_by_project.items():
        for row in rows:
            try:
                payload = json.loads(row.payload_json or "{}") or {}
            except (json.JSONDecodeError, TypeError):
                payload = {}
            grouped[project_id].append(
                DecisionEventInfo(
                    id=row.id,
                    project_id=row.project_id,
                    task_id=row.task_id,
                    band_id=row.band_id,
                    chapter_number=row.chapter_number,
                    scope=row.scope,
                    event_family=row.event_family,
                    event_type=row.event_type,
                    actor_type=row.actor_type,
                    actor_id=row.actor_id,
                    summary=row.summary,
                    reason=row.reason,
                    payload=payload if isinstance(payload, dict) else {},
                    related_object_type=row.related_object_type,
                    related_object_id=row.related_object_id,
                    parent_event_id=str(getattr(row, "parent_event_id", "") or ""),
                    causal_root_id=str(getattr(row, "causal_root_id", "") or ""),
                    created_at=row.created_at.isoformat() if row.created_at else "",
                )
            )
    return dict(grouped)


def _narrative_constraints_by_project(
    session: Session,
    project_ids: list[str],
    *,
    limit: int,
) -> dict[str, list[NarrativeConstraintInfo]]:
    rows_by_project = _recent_rows_by_project(
        session,
        NarrativeConstraint,
        NarrativeConstraint.project_id,
        project_ids,
        order_by=(
            NarrativeConstraint.created_at.desc(),
            NarrativeConstraint.id.desc(),
        ),
        limit=limit,
    )
    grouped: dict[str, list[NarrativeConstraintInfo]] = defaultdict(list)
    for project_id, rows in rows_by_project.items():
        for row in rows:
            try:
                payload = json.loads(row.payload_json or "{}") or {}
            except (json.JSONDecodeError, TypeError):
                payload = {}
            grouped[project_id].append(
                NarrativeConstraintInfo(
                    id=row.id,
                    project_id=row.project_id,
                    arc_id=row.arc_id,
                    band_id=row.band_id,
                    constraint_type=row.constraint_type,
                    level=row.level,
                    subject_name=row.subject_name,
                    description=row.description,
                    payload=payload if isinstance(payload, dict) else {},
                    effective_from_chapter=row.effective_from_chapter,
                    protect_until_chapter=row.protect_until_chapter,
                    status=row.status,
                    created_at=row.created_at.isoformat() if row.created_at else "",
                    updated_at=row.updated_at.isoformat() if row.updated_at else "",
                )
            )
    return dict(grouped)


def _band_checkpoint_detail(row: BandCheckpoint | None) -> BandCheckpointDetail | None:
    if row is None:
        return None
    try:
        issues_payload = json.loads(row.issues_json or "[]") or []
    except (json.JSONDecodeError, TypeError):
        issues_payload = []
    issues: list[BandCheckpointIssueInfo] = []
    for item in issues_payload:
        if not isinstance(item, dict):
            continue
        issues.append(BandCheckpointIssueInfo.model_validate(item))
    return BandCheckpointDetail(
        id=row.id,
        project_id=row.project_id,
        arc_id=row.arc_id,
        band_id=row.band_id,
        chapter_start=int(row.chapter_start or 0),
        chapter_end=int(row.chapter_end or 0),
        trigger_source=str(row.trigger_source or ""),
        boundary_kind=str(row.boundary_kind or ""),
        boundary_chapter=int(row.boundary_chapter or 0),
        status=str(row.status or "pending"),
        summary=str(row.summary or ""),
        reason=str(row.reason or ""),
        issues=issues,
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
        resolved_at=row.resolved_at.isoformat() if row.resolved_at else "",
    )


def _derive_blocking_reason(
    *,
    plans: list[ChapterPlan],
    latest_band_checkpoint: BandCheckpoint | None,
    decision_events: list[DecisionEventInfo] | None = None,
    future_constraints_enabled: bool = True,
) -> BlockingReasonInfo:
    ordered_events = list(decision_events or [])
    fallback_event_id = str(ordered_events[0].id or "") if ordered_events else ""

    def _latest_event_id_for_chapter(chapter_number: int) -> str:
        for event in ordered_events:
            if int(event.chapter_number or 0) == int(chapter_number or 0):
                return str(event.id or "")
        return fallback_event_id

    def _latest_event_id_for_related_object(related_object_type: str, related_object_id: str) -> str:
        if not related_object_type or not related_object_id:
            return fallback_event_id
        for event in ordered_events:
            if (
                str(event.related_object_type or "") == related_object_type
                and str(event.related_object_id or "") == related_object_id
            ):
                return str(event.id or "")
        return fallback_event_id

    def _latest_event_id_for_band(band_id: str) -> str:
        if not band_id:
            return fallback_event_id
        for event in ordered_events:
            if str(event.band_id or "") == band_id:
                return str(event.id or "")
        return fallback_event_id

    def _latest_future_constraint_event(chapter_number: int) -> DecisionEventInfo | None:
        for event in ordered_events:
            if int(event.chapter_number or 0) != int(chapter_number or 0):
                continue
            if str(event.event_type or "") == DecisionEventType.HARD_GATE_HIT and str(
                event.payload.get("blocking_reason") or ""
            ) == "future_constraint_block":
                return event
            if str(event.event_type or "") != DecisionEventType.REVIEW_VERDICT_RECORDED:
                continue
            issue_types = event.payload.get("issue_types") or []
            if not isinstance(issue_types, list):
                continue
            if "future_constraint" not in {str(item or "") for item in issue_types}:
                continue
            if str(event.payload.get("verdict") or "").strip() != "fail":
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
            detail = str(event.summary or "").strip()
            if not detail:
                detail = str(event.payload.get("error_summary") or "").strip()
        return BlockingReasonInfo(
            code="future_constraint_block",
            message=chapter_blocking_message("future_constraint_block"),
            chapter_number=constraint_block_plan.chapter_number,
            decision_event_id=str(event.id if event is not None else _latest_event_id_for_chapter(constraint_block_plan.chapter_number)),
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
    if latest_band_checkpoint is None:
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
    next_planned = next(
        (plan.chapter_number for plan in sorted(plans, key=lambda item: item.chapter_number) if plan.status != "accepted"),
        0,
    )
    if next_planned:
        return f"chapter_{next_planned}_write"
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
    elif len(accepted) == len(plans):
        plan_state = "completed"
    elif failed and not planned:
        plan_state = "failed"
    else:
        plan_state = "in_progress"
    writing_state = "not_started"
    if generated:
        writing_state = "completed" if len(accepted) == len(plans) else "started"
    review_state = "pending" if pending_review else "none"
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
        can_resume=bool(next_candidates and not pending_review),
        pause_requested=bool(pause_requested),
        review_interval_chapters=review_interval,
        chapters_until_review=chapters_until_review,
        chapters_until_replan_eligible=chapters_until_replan,
        blocking_reason=blocking_reason,
        latest_band_checkpoint=_band_checkpoint_detail(latest_band_checkpoint),
        next_gate=_derive_next_gate(plans=plans, blocking_reason=blocking_reason),
    )


def project_arc_snapshot_payload(
    latest_arc_envelope,
    latest_arc_analysis,
    latest_provisional,
    latest_arc_structure=None,
    latest_band_experience=None,
) -> dict[str, Any]:
    payload = {
        "active_arc_id": "",
        "active_arc_policy_tier": "",
        "active_arc_target_size": 0,
        "active_arc_soft_min": 0,
        "active_arc_soft_max": 0,
        "active_arc_detailed_band_size": 0,
        "active_arc_frozen_zone_size": 0,
        "active_arc_confidence": 0.0,
        "active_arc_recommendation": "",
        "active_arc_analysis_confidence": 0.0,
        "active_arc_evidence": [],
        "active_arc_expansion_signals": [],
        "active_arc_compression_signals": [],
        "provisional_band_id": "",
        "provisional_aggregate_verdict": "",
        "provisional_preview_char_count": 0,
        "provisional_issue_count": 0,
        "provisional_failure_count": 0,
        "active_reader_promise": {},
        "active_band_reward_mix": [],
        "active_band_stall_guard": 0,
        "active_revelation_layers": [],
        "active_band_curiosity_beats": [],
        "active_band_template_ids": [],
    }
    if latest_arc_envelope is not None:
        payload.update(
            {
                "active_arc_id": str(getattr(latest_arc_envelope, "arc_id", "") or ""),
                "active_arc_policy_tier": latest_arc_envelope.source_policy_tier,
                "active_arc_target_size": latest_arc_envelope.resolved_target_size,
                "active_arc_soft_min": latest_arc_envelope.resolved_soft_min,
                "active_arc_soft_max": latest_arc_envelope.resolved_soft_max,
                "active_arc_detailed_band_size": latest_arc_envelope.detailed_band_size,
                "active_arc_frozen_zone_size": latest_arc_envelope.frozen_zone_size,
                "active_arc_confidence": latest_arc_envelope.current_confidence,
            }
        )
    if latest_arc_analysis is not None:
        payload.update(
            {
                "active_arc_recommendation": latest_arc_analysis.recommendation,
                "active_arc_analysis_confidence": latest_arc_analysis.confidence,
                "active_arc_evidence": _json_list_strings(latest_arc_analysis.evidence_json),
                "active_arc_expansion_signals": _json_list_strings(
                    latest_arc_analysis.expansion_signals_json
                ),
                "active_arc_compression_signals": _json_list_strings(
                    latest_arc_analysis.compression_signals_json
                ),
            }
        )
    if latest_provisional is not None:
        payload.update(
            {
                "provisional_band_id": latest_provisional.band_id,
                "provisional_aggregate_verdict": latest_provisional.aggregate_verdict,
                "provisional_preview_char_count": latest_provisional.preview_char_count,
                "provisional_issue_count": latest_provisional.issue_count,
                "provisional_failure_count": latest_provisional.failure_count,
            }
        )
    if latest_arc_structure is not None:
        if not payload.get("active_arc_id"):
            payload["active_arc_id"] = str(getattr(latest_arc_structure, "arc_id", "") or "")
        payload["active_reader_promise"] = _json_object(latest_arc_structure.reader_promise_json)
        arc_payoff_map = _json_object(latest_arc_structure.arc_payoff_map_json)
        payload["active_revelation_layers"] = [
            item
            for item in (arc_payoff_map.get("revelation_layers") or [])
            if isinstance(item, dict)
        ]
    if latest_band_experience is not None:
        if not payload.get("active_arc_id"):
            payload["active_arc_id"] = str(getattr(latest_band_experience, "arc_id", "") or "")
        band_payload = _json_object(latest_band_experience.schedule_json)
        rewards = band_payload.get("scheduled_rewards") or []
        payload["active_band_reward_mix"] = [
            str(item.get("category") or "")
            for item in rewards
            if isinstance(item, dict) and str(item.get("category") or "").strip()
        ]
        payload["active_band_template_ids"] = [
            str(item.get("template_id") or "")
            for item in rewards
            if isinstance(item, dict) and str(item.get("template_id") or "").strip()
        ]
        payload["active_band_curiosity_beats"] = [
            item
            for item in (band_payload.get("curiosity_beats") or [])
            if isinstance(item, dict)
        ]
        payload["active_band_stall_guard"] = int(
            band_payload.get("stall_guard_max_gap") or latest_band_experience.stall_guard_max_gap or 0
        )
    return payload


def load_recent_replan_events_by_project(
    session: Session,
    project_ids: list[str],
    *,
    limit: int,
) -> dict[str, list[ProjectReplanEvent]]:
    return _recent_rows_by_project(
        session,
        ProjectReplanEvent,
        ProjectReplanEvent.project_id,
        project_ids,
        order_by=(
            ProjectReplanEvent.trigger_chapter.desc(),
            ProjectReplanEvent.created_at.desc(),
            ProjectReplanEvent.id.desc(),
        ),
        limit=limit,
    )


def load_recent_npc_intents_by_project(
    session: Session,
    project_ids: list[str],
    *,
    limit: int,
) -> dict[str, list[NPCIntentSnapshot]]:
    return _recent_rows_by_project(
        session,
        NPCIntentSnapshot,
        NPCIntentSnapshot.project_id,
        project_ids,
        order_by=(
            NPCIntentSnapshot.chapter_number.desc(),
            NPCIntentSnapshot.urgency.desc(),
            NPCIntentSnapshot.created_at.desc(),
            NPCIntentSnapshot.id.desc(),
        ),
        limit=limit,
    )


def _load_latest_arc_structure_by_project(
    session: Session,
    project_ids: list[str],
) -> dict[str, ArcStructureDraft]:
    return _latest_rows_by_project(
        session,
        ArcStructureDraft,
        ArcStructureDraft.project_id,
        project_ids,
        order_by=(
            ArcStructureDraft.created_at.desc(),
            ArcStructureDraft.id.desc(),
        ),
    )


def _load_latest_band_experience_by_project(
    session: Session,
    project_ids: list[str],
) -> dict[str, BandExperiencePlan]:
    return _latest_rows_by_project(
        session,
        BandExperiencePlan,
        BandExperiencePlan.project_id,
        project_ids,
        order_by=(
            BandExperiencePlan.created_at.desc(),
            BandExperiencePlan.id.desc(),
        ),
    )


def normalize_project_automation(raw: str | dict[str, Any] | None) -> ProjectAutomationSettings:
    payload: dict[str, Any]
    if isinstance(raw, dict):
        payload = dict(raw)
    else:
        try:
            payload = json.loads(raw or "{}") or {}
        except (json.JSONDecodeError, TypeError):
            payload = {}

    def _normalize_publish_settings(item: Any) -> dict[str, Any]:
        publish_raw = item if isinstance(item, dict) else {}
        book_meta_raw = publish_raw.get("book_meta")
        if not isinstance(book_meta_raw, dict):
            book_meta_raw = {}
        return {
            "platform": str(publish_raw.get("platform", "")).strip(),
            "book_name": str(publish_raw.get("book_name", "")).strip(),
            "upload_url": str(publish_raw.get("upload_url", "")).strip(),
            "create_if_missing": bool(publish_raw.get("create_if_missing", False)),
            "book_meta": {
                "audience": str(book_meta_raw.get("audience", "")).strip(),
                "primary_category": str(book_meta_raw.get("primary_category", "")).strip(),
                "theme_tags": [
                    str(entry).strip()
                    for entry in (book_meta_raw.get("theme_tags") or [])
                    if str(entry).strip()
                ],
                "role_tags": [
                    str(entry).strip()
                    for entry in (book_meta_raw.get("role_tags") or [])
                    if str(entry).strip()
                ],
                "plot_tags": [
                    str(entry).strip()
                    for entry in (book_meta_raw.get("plot_tags") or [])
                    if str(entry).strip()
                ],
                "protagonist_names": [
                    str(entry).strip()
                    for entry in (book_meta_raw.get("protagonist_names") or [])
                    if str(entry).strip()
                ],
                "intro": str(book_meta_raw.get("intro", "")).strip(),
            },
        }

    def _normalize_publish_bindings(items: Any) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seen_platforms: set[str] = set()
        for entry in items if isinstance(items, list) else []:
            publish_entry = _normalize_publish_settings(entry)
            platform = publish_entry["platform"]
            if not platform or platform in seen_platforms:
                continue
            normalized.append(publish_entry)
            seen_platforms.add(platform)
            if len(normalized) >= 2:
                break
        return normalized

    publish_payload = _normalize_publish_settings(payload.get("publish"))
    publish_bindings = _normalize_publish_bindings(payload.get("publish_bindings"))
    if publish_payload["platform"]:
        publish_bindings = [
            publish_payload,
            *[
                entry
                for entry in publish_bindings
                if entry["platform"] != publish_payload["platform"]
            ],
        ][:2]
    elif publish_bindings:
        publish_payload = publish_bindings[0]

    time_text = str(payload.get("daily_start_time", "09:00") or "09:00").strip() or "09:00"
    parts = time_text.split(":", 1)
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        time_text = "09:00"
    else:
        hour = min(23, max(0, int(parts[0])))
        minute = min(59, max(0, int(parts[1])))
        time_text = f"{hour:02d}:{minute:02d}"

    quota_raw = payload.get("daily_chapter_quota", 1)
    try:
        quota = int(quota_raw)
    except (TypeError, ValueError):
        quota = 1
    quota = min(20, max(1, quota))

    return ProjectAutomationSettings.model_validate(
        {
            "enabled": bool(payload.get("enabled", False)),
            "daily_start_time": time_text,
            "daily_chapter_quota": quota,
            "auto_publish": bool(payload.get("auto_publish", False)),
            "publish": publish_payload,
            "publish_bindings": publish_bindings,
            "last_scheduler_date": str(payload.get("last_scheduler_date", "")).strip(),
            "last_scheduler_at": str(payload.get("last_scheduler_at", "")).strip(),
            "last_scheduler_action": str(payload.get("last_scheduler_action", "")).strip(),
            "last_scheduler_message": str(payload.get("last_scheduler_message", "")).strip(),
            "last_scheduler_task_id": str(payload.get("last_scheduler_task_id", "")).strip(),
        }
    )


def _normalize_genesis_pack(raw: str | None) -> BookGenesisPack:
    try:
        payload = json.loads(raw or "{}") or {}
    except (json.JSONDecodeError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    raw_world = _deep_merge_dict(
        empty_world_root(),
        payload.get("world") if isinstance(payload.get("world"), dict) else {},
    )
    if isinstance(payload.get("world_bible"), dict):
        raw_world["world_bible"] = _deep_merge_dict(
            raw_world.get("world_bible") if isinstance(raw_world.get("world_bible"), dict) else {},
            payload.get("world_bible") or {},
        )
    if isinstance(payload.get("map_atlas"), dict):
        raw_world["map_atlas"] = _deep_merge_dict(
            raw_world.get("map_atlas") if isinstance(raw_world.get("map_atlas"), dict) else {},
            payload.get("map_atlas") or {},
        )
    if isinstance(payload.get("story_engine"), dict):
        raw_world["story_engine"] = _deep_merge_dict(
            raw_world.get("story_engine") if isinstance(raw_world.get("story_engine"), dict) else {},
            payload.get("story_engine") or {},
        )
    raw_stage_states = payload.get("stage_states") if isinstance(payload, dict) else {}
    if not isinstance(raw_stage_states, dict):
        raw_stage_states = {}
    stage_states: dict[str, BookGenesisStageState] = {}
    for stage_key in _GENESIS_STAGE_ORDER:
        stage_raw = raw_stage_states.get(stage_key)
        if not isinstance(stage_raw, dict):
            stage_raw = {}
        stage_states[stage_key] = BookGenesisStageState(
            stage_key=stage_key,
            status=str(stage_raw.get("status", "todo") or "todo"),
            locked=bool(stage_raw.get("locked", False)),
            updated_at=str(stage_raw.get("updated_at", "") or ""),
            last_trace_id=str(stage_raw.get("last_trace_id", "") or ""),
        )
    return BookGenesisPack(
        book_brief=payload.get("book_brief") if isinstance(payload.get("book_brief"), dict) else {},
        world=raw_world,
        book_arc_blueprint=(
            payload.get("book_arc_blueprint") if isinstance(payload.get("book_arc_blueprint"), dict) else {}
        ),
        subworld_policy=(
            payload.get("subworld_policy") if isinstance(payload.get("subworld_policy"), dict) else {}
        ),
        execution_bootstrap=(
            payload.get("execution_bootstrap") if isinstance(payload.get("execution_bootstrap"), dict) else {}
        ),
        stage_states=stage_states,
    )


def _load_latest_genesis_revision_by_project(
    session: Session,
    project_ids: list[str],
) -> dict[str, BookGenesisRevision]:
    return _latest_rows_by_project(
        session,
        BookGenesisRevision,
        BookGenesisRevision.project_id,
        project_ids,
        order_by=(
            BookGenesisRevision.revision.desc(),
            BookGenesisRevision.created_at.desc(),
            BookGenesisRevision.id.desc(),
        ),
    )


def _stage_overview_from_revision(revision: BookGenesisRevision | None) -> list[BookGenesisStageState]:
    if revision is None:
        return []
    pack = _normalize_genesis_pack(revision.pack_json)
    return [pack.stage_states[stage_key] for stage_key in _GENESIS_STAGE_ORDER]


def _can_start_writing(project: Project, revision: BookGenesisRevision | None) -> bool:
    if str(getattr(project, "creation_status", "") or "").strip() != "genesis_ready":
        return False
    if revision is None:
        return False
    pack = _normalize_genesis_pack(revision.pack_json)
    return all(pack.stage_states[stage_key].locked for stage_key in _GENESIS_STAGE_ORDER)


def _prompt_trace_infos(
    session: Session,
    *,
    project_id: str,
    limit: int = 40,
) -> list[PromptTraceInfo]:
    rows = session.execute(
        select(PromptTrace)
        .where(PromptTrace.project_id == project_id)
        .order_by(PromptTrace.created_at.desc(), PromptTrace.id.desc())
        .limit(max(1, int(limit or 40)))
    ).scalars().all()
    payload: list[PromptTraceInfo] = []
    for row in rows:
        payload.append(
            PromptTraceInfo(
                id=row.id,
                trace_scope=str(row.trace_scope or "genesis"),
                stage_key=str(row.stage_key or ""),
                template_id=str(row.template_id or ""),
                template_version=str(row.template_version or "v1"),
                effective_system_prompt=str(row.effective_system_prompt or ""),
                prompt_layers=_load_json_list(row.prompt_layers_json),
                input_snapshot=_json_object(row.input_snapshot_json),
                model_profile=_json_object(row.model_profile_json),
                attempts=_load_json_list(row.attempts_json),
                output_summary=_json_object(row.output_summary_json),
                decision_event_id=str(row.decision_event_id or ""),
                parent_trace_id=str(row.parent_trace_id or ""),
                created_at=row.created_at.isoformat() if row.created_at else "",
            )
        )
    return payload


def load_project_upload_stats(
    session: Session,
    project_ids: list[str],
) -> dict[str, dict[str, int]]:
    ids = _normalized_project_ids(project_ids)
    if not ids:
        return {}
    rows = session.execute(
        select(
            PublisherUploadJob.project_id,
            func.count(PublisherUploadJob.id).label("upload_task_count"),
            func.sum(
                case(
                    (PublisherUploadJob.status == "succeeded", 1),
                    else_=0,
                )
            ).label("uploaded_chapter_count"),
        ).where(
            PublisherUploadJob.project_id.in_(ids),
            PublisherUploadJob.deleted_at.is_(None),
        ).group_by(PublisherUploadJob.project_id)
    ).all()
    stats = {
        project_id: {
            "upload_task_count": 0,
            "uploaded_chapter_count": 0,
        }
        for project_id in ids
    }
    for project_id, upload_task_count, uploaded_chapter_count in rows:
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id:
            continue
        stats[normalized_project_id] = {
            "upload_task_count": int(upload_task_count or 0),
            "uploaded_chapter_count": int(uploaded_chapter_count or 0),
        }
    return stats


def load_project_runtime_maps(
    session: Session,
    project_ids: list[str],
) -> dict[str, dict[str, Any]]:
    latest_stage_map = load_latest_stage_analysis_by_project(session, project_ids)
    last_replan_map = load_latest_replan_event_by_project(session, project_ids)
    latest_world_map = load_latest_world_turn_by_project(session, project_ids)
    latest_arc_envelope_map = load_latest_active_arc_envelope_by_project(session, project_ids)
    latest_arc_analysis_map = load_latest_arc_envelope_analysis_by_project(session, project_ids)
    provisional_map = load_latest_provisional_band_execution_by_project(session, project_ids)
    latest_arc_structure_map = _load_latest_arc_structure_by_project(session, project_ids)
    latest_band_experience_map = _load_latest_band_experience_by_project(session, project_ids)
    recent_replans_map = load_recent_replan_events_by_project(session, project_ids, limit=5)
    recent_npc_map = load_recent_npc_intents_by_project(session, project_ids, limit=6)
    return {
        "latest_stage_map": latest_stage_map,
        "last_replan_map": last_replan_map,
        "latest_world_map": latest_world_map,
        "latest_arc_envelope_map": latest_arc_envelope_map,
        "latest_arc_analysis_map": latest_arc_analysis_map,
        "provisional_map": provisional_map,
        "latest_arc_structure_map": latest_arc_structure_map,
        "latest_band_experience_map": latest_band_experience_map,
        "recent_replans_map": recent_replans_map,
        "recent_npc_map": recent_npc_map,
    }


def build_project_summaries(
    *,
    session: Session,
    projects: list[Project],
    display_datetime: DisplayDatetime,
    review_interval_chapters: int = 0,
) -> list[ProjectSummary]:
    project_ids = [project.id for project in projects]
    plans = (
        session.execute(
            select(ChapterPlan)
            .where(ChapterPlan.project_id.in_(project_ids))
            .order_by(ChapterPlan.project_id, ChapterPlan.chapter_number)
        ).scalars().all()
        if project_ids
        else []
    )
    draft_map = load_latest_drafts_by_plan_id(session, [plan.id for plan in plans])
    runtime_maps = load_project_runtime_maps(session, project_ids)
    upload_stats = load_project_upload_stats(session, project_ids)
    genesis_revision_map = _load_latest_genesis_revision_by_project(session, project_ids)
    planned_future_projects = {
        str(project_id or "").strip()
        for project_id in session.execute(
            select(ArcPlanVersion.project_id)
            .where(
                ArcPlanVersion.project_id.in_(project_ids),
                ArcPlanVersion.status == "planned",
            )
            .distinct()
        ).scalars().all()
        if str(project_id or "").strip()
    } if project_ids else set()
    latest_checkpoint_map = _latest_band_checkpoint_by_project(session, project_ids)
    decision_timeline_map = _decision_timeline_by_project(session, project_ids, limit=20)
    chapters_by_project: dict[str, list[dict[str, object]]] = {}
    plans_by_project: dict[str, list[ChapterPlan]] = defaultdict(list)
    chapter_stats_by_project: dict[str, dict[str, int]] = {
        project_id: {
            "chapter_count": 0,
            "generated_chapter_count": 0,
            "accepted_chapter_count": 0,
            "needs_review_chapter_count": 0,
        }
        for project_id in project_ids
    }
    for plan in plans:
        plans_by_project[plan.project_id].append(plan)
        draft = draft_map.get(plan.id)
        stats = chapter_stats_by_project.setdefault(
            plan.project_id,
            {
                "chapter_count": 0,
                "generated_chapter_count": 0,
                "accepted_chapter_count": 0,
                "needs_review_chapter_count": 0,
            },
        )
        stats["chapter_count"] += 1
        if draft is not None:
            stats["generated_chapter_count"] += 1
        if plan.status == "accepted":
            stats["accepted_chapter_count"] += 1
        if plan.status == "needs_review":
            stats["needs_review_chapter_count"] += 1
        chapters_by_project.setdefault(plan.project_id, []).append(
            {
                "chapter_number": plan.chapter_number,
                "title": plan.title,
                "status": plan.status,
                "char_count": draft.char_count if draft else 0,
                "summary": draft.summary if draft else "",
            }
        )

    payload: list[ProjectSummary] = []
    for project in projects:
        latest_stage = runtime_maps["latest_stage_map"].get(project.id)
        last_replan = runtime_maps["last_replan_map"].get(project.id)
        latest_world = runtime_maps["latest_world_map"].get(project.id)
        latest_arc_envelope = runtime_maps["latest_arc_envelope_map"].get(project.id)
        latest_arc_analysis = runtime_maps["latest_arc_analysis_map"].get(project.id)
        latest_provisional = runtime_maps["provisional_map"].get(project.id)
        latest_arc_structure = runtime_maps["latest_arc_structure_map"].get(project.id)
        latest_band_experience = runtime_maps["latest_band_experience_map"].get(project.id)
        latest_checkpoint = latest_checkpoint_map.get(project.id)
        chapter_stats = chapter_stats_by_project.get(project.id, {})
        project_upload_stats = upload_stats.get(project.id, {})
        governance = normalize_project_governance(project.governance_json)
        genesis_revision = genesis_revision_map.get(project.id)
        generation_control = build_generation_control(
            plans=plans_by_project.get(project.id, []),
            latest_replan=last_replan,
            review_interval_chapters=governance.review_interval_chapters or review_interval_chapters,
            latest_band_checkpoint=latest_checkpoint,
            decision_events=decision_timeline_map.get(project.id, []),
            future_constraints_enabled=governance.future_constraints_enabled,
        )
        if (
            str(getattr(project, "creation_status", "") or "").strip() == "writing"
            and project.id in planned_future_projects
            and not generation_control.pending_review_chapters
        ):
            generation_control.can_resume = True
            if generation_control.plan_state == "completed":
                generation_control.plan_state = "in_progress"
            if not generation_control.next_gate:
                generation_control.next_gate = "next_arc_ready"
        payload.append(
            ProjectSummary(
                id=project.id,
                title=project.title,
                genre=project.genre,
                premise=project.premise[:100] + "..." if len(project.premise) > 100 else project.premise,
                created_at=display_datetime(project.created_at),
                target_total_chapters=effective_target_total_chapters(
                    project,
                    int(chapter_stats.get("chapter_count", 0) or 0),
                ),
                creation_status=str(getattr(project, "creation_status", "") or "legacy"),
                active_genesis_revision_id=str(getattr(project, "active_genesis_revision_id", "") or ""),
                genesis_stage_overview=_stage_overview_from_revision(genesis_revision),
                can_start_writing=_can_start_writing(project, genesis_revision),
                chapter_count=int(chapter_stats.get("chapter_count", 0) or 0),
                generated_chapter_count=int(chapter_stats.get("generated_chapter_count", 0) or 0),
                accepted_chapter_count=int(chapter_stats.get("accepted_chapter_count", 0) or 0),
                needs_review_chapter_count=int(chapter_stats.get("needs_review_chapter_count", 0) or 0),
                upload_task_count=int(project_upload_stats.get("upload_task_count", 0) or 0),
                uploaded_chapter_count=int(project_upload_stats.get("uploaded_chapter_count", 0) or 0),
                automation=normalize_project_automation(project.automation_json),
                governance=governance,
                latest_stage=latest_stage.stage_label if latest_stage else "",
                pacing_verdict=latest_stage.pacing_verdict if latest_stage else "",
                pacing_summary=latest_stage.pacing_summary if latest_stage else "",
                last_replan_status=last_replan.status if last_replan else "",
                last_replan_strategy=last_replan.strategy if last_replan else "",
                last_replan_reason=last_replan.reason if last_replan else "",
                current_time_label=latest_stage.timeline_label if latest_stage else "",
                world_pressure_level=latest_world.pressure_level if latest_world else "",
                world_pressure_summary=latest_world.pressure_summary if latest_world else "",
                generation_control=generation_control,
                chapters=chapters_by_project.get(project.id, []),
                latest_band_checkpoint=generation_control.latest_band_checkpoint,
                blocking_reason=generation_control.blocking_reason,
                next_gate=generation_control.next_gate,
                **project_arc_snapshot_payload(
                    latest_arc_envelope,
                    latest_arc_analysis,
                    latest_provisional,
                    latest_arc_structure,
                    latest_band_experience,
                ),
            )
        )
    return payload


def build_project_detail(
    *,
    session: Session,
    project: Project,
    display_datetime: DisplayDatetime,
    review_interval_chapters: int = 0,
) -> ProjectDetail:
    project_id = project.id
    genesis_revision = _load_latest_genesis_revision_by_project(session, [project_id]).get(project_id)
    entities = session.execute(
        select(Entity).where(Entity.project_id == project_id, Entity.is_active == True)
    ).scalars().all()
    characters = [
        EntityInfo(id=e.id, kind=e.kind, name=e.name, description=e.description, importance=e.importance)
        for e in entities
        if e.kind == "character"
    ]
    locations = [
        EntityInfo(id=e.id, kind=e.kind, name=e.name, description=e.description, importance=e.importance)
        for e in entities
        if e.kind == "location"
    ]
    factions = [
        EntityInfo(id=e.id, kind=e.kind, name=e.name, description=e.description, importance=e.importance)
        for e in entities
        if e.kind == "faction"
    ]
    subworld_rows = session.execute(
        select(SubWorld).where(SubWorld.project_id == project_id).order_by(SubWorld.scope.asc(), SubWorld.created_at.asc())
    ).scalars().all()
    roster_rows = session.execute(
        select(SubWorldRosterItem).where(SubWorldRosterItem.project_id == project_id)
    ).scalars().all()
    roster_by_subworld: dict[str, list[SubWorldRosterItem]] = defaultdict(list)
    for row in roster_rows:
        roster_by_subworld[row.subworld_id].append(row)

    threads = session.execute(
        select(PlotThread).where(PlotThread.project_id == project_id)
    ).scalars().all()
    thread_infos = [
        ThreadInfo(id=t.id, name=t.name, description=t.description, status=t.status, priority=t.priority)
        for t in threads
    ]

    plans = session.execute(
        select(ChapterPlan).where(ChapterPlan.project_id == project_id).order_by(ChapterPlan.chapter_number)
    ).scalars().all()
    draft_map = load_latest_drafts_by_plan_id(session, [plan.id for plan in plans])
    latest_attempt_map: dict[int, ChapterRewriteAttempt] = {}
    for attempt in session.execute(
        select(ChapterRewriteAttempt)
        .where(ChapterRewriteAttempt.project_id == project_id)
        .order_by(
            ChapterRewriteAttempt.chapter_number.asc(),
            ChapterRewriteAttempt.attempt_no.desc(),
            ChapterRewriteAttempt.created_at.desc(),
        )
    ).scalars().all():
        latest_attempt_map.setdefault(int(attempt.chapter_number or 0), attempt)
    upload_stats = load_project_upload_stats(session, [project_id]).get(project_id, {})
    chapter_infos = [
        ChapterInfo(
            chapter_number=plan.chapter_number,
            title=plan.title,
            status=plan.status,
            char_count=draft_map.get(plan.id).char_count if draft_map.get(plan.id) else 0,
            summary=draft_map.get(plan.id).summary if draft_map.get(plan.id) else "",
            acceptance_mode=str(getattr(plan, "acceptance_mode", "") or ""),
            repair_attempt_count=int(getattr(plan, "repair_attempt_count", 0) or 0),
            canon_risk_level=str(getattr(plan, "canon_risk_level", "") or ""),
            latest_repair_scope=normalize_repair_scope(
                getattr(latest_attempt_map.get(plan.chapter_number), "repair_scope", "") or "",
                default="",
            ),
        )
        for plan in plans
    ]
    generated_chapter_count = sum(1 for plan in plans if draft_map.get(plan.id) is not None)
    accepted_chapter_count = sum(1 for plan in plans if plan.status == "accepted")
    needs_review_chapter_count = sum(1 for plan in plans if plan.status == "needs_review")

    runtime_maps = load_project_runtime_maps(session, [project_id])
    latest_checkpoint = _latest_band_checkpoint_by_project(session, [project_id]).get(project_id)
    governance = normalize_project_governance(project.governance_json)
    decision_timeline = _decision_timeline_by_project(session, [project_id], limit=30).get(project_id, [])
    narrative_constraints = _narrative_constraints_by_project(session, [project_id], limit=50).get(project_id, [])
    latest_stage = runtime_maps["latest_stage_map"].get(project_id)
    latest_world = runtime_maps["latest_world_map"].get(project_id)
    latest_arc_envelope = runtime_maps["latest_arc_envelope_map"].get(project_id)
    latest_arc_analysis = runtime_maps["latest_arc_analysis_map"].get(project_id)
    latest_provisional = runtime_maps["provisional_map"].get(project_id)
    latest_arc_structure = runtime_maps["latest_arc_structure_map"].get(project_id)
    latest_band_experience = runtime_maps["latest_band_experience_map"].get(project_id)
    npc_intents = runtime_maps["recent_npc_map"].get(project_id, [])
    replan_events = runtime_maps["recent_replans_map"].get(project_id, [])
    active_subworld_ids: set[str] = set()
    if latest_band_experience is not None:
        try:
            payload = json.loads(latest_band_experience.schedule_json or "{}") or {}
        except (json.JSONDecodeError, TypeError):
            payload = {}
        active_subworld_ids = {
            str(item).strip()
            for item in (payload.get("active_subworld_ids") or [])
            if str(item).strip()
        }
    subworlds = [
        {
            "id": row.id,
            "name": row.name,
            "purpose": row.purpose,
            "scope": row.scope,
            "status": row.status,
            "active_in_current_band": row.id in active_subworld_ids,
            "core_cast": [
                item.display_name
                for item in roster_by_subworld.get(row.id, [])
                if item.is_core and str(item.display_name or "").strip()
            ],
            "planned_slot_count": sum(
                1 for item in roster_by_subworld.get(row.id, []) if item.status == "planned_slot"
            ),
        }
        for row in subworld_rows
    ]
    generation_control = build_generation_control(
        plans=plans,
        latest_replan=replan_events[0] if replan_events else None,
        review_interval_chapters=governance.review_interval_chapters or review_interval_chapters,
        latest_band_checkpoint=latest_checkpoint,
        decision_events=decision_timeline,
        future_constraints_enabled=governance.future_constraints_enabled,
    )
    has_planned_future_arc = session.execute(
        select(ArcPlanVersion.id)
        .where(
            ArcPlanVersion.project_id == project_id,
            ArcPlanVersion.status == "planned",
        )
        .limit(1)
    ).scalar_one_or_none() is not None
    if (
        str(getattr(project, "creation_status", "") or "").strip() == "writing"
        and has_planned_future_arc
        and not generation_control.pending_review_chapters
    ):
        generation_control.can_resume = True
        if generation_control.plan_state == "completed":
            generation_control.plan_state = "in_progress"
        if not generation_control.next_gate:
            generation_control.next_gate = "next_arc_ready"

    return ProjectDetail(
        id=project.id,
        title=project.title,
        premise=project.premise,
        genre=project.genre,
        setting_summary=project.setting_summary,
        target_total_chapters=effective_target_total_chapters(project, len(plans)),
        creation_status=str(getattr(project, "creation_status", "") or "legacy"),
        active_genesis_revision_id=str(getattr(project, "active_genesis_revision_id", "") or ""),
        genesis_stage_overview=_stage_overview_from_revision(genesis_revision),
        can_start_writing=_can_start_writing(project, genesis_revision),
        chapter_count=len(plans),
        generated_chapter_count=generated_chapter_count,
        accepted_chapter_count=accepted_chapter_count,
        needs_review_chapter_count=needs_review_chapter_count,
        upload_task_count=int(upload_stats.get("upload_task_count", 0) or 0),
        uploaded_chapter_count=int(upload_stats.get("uploaded_chapter_count", 0) or 0),
        automation=normalize_project_automation(project.automation_json),
        governance=governance,
        characters=characters,
        locations=locations,
        factions=factions,
        subworlds=subworlds,
        threads=thread_infos,
        chapters=chapter_infos,
        latest_stage=latest_stage.stage_label if latest_stage else "",
        progress_ratio=latest_stage.progress_ratio if latest_stage else 0.0,
        pacing_verdict=latest_stage.pacing_verdict if latest_stage else "",
        pacing_summary=latest_stage.pacing_summary if latest_stage else "",
        current_time_label=latest_stage.timeline_label if latest_stage else "",
        world_pressure_level=latest_world.pressure_level if latest_world else "",
        world_pressure_summary=latest_world.pressure_summary if latest_world else "",
        npc_intent_count=len(npc_intents),
        **project_arc_snapshot_payload(
            latest_arc_envelope,
            latest_arc_analysis,
            latest_provisional,
            latest_arc_structure,
            latest_band_experience,
        ),
        recent_npc_intents=[
            {
                "chapter_number": item.chapter_number,
                "entity_name": item.entity_name,
                "intent_kind": item.intent_kind,
                "objective": item.objective,
                "tactic": item.tactic,
                "urgency": item.urgency,
                "notes": item.notes,
            }
            for item in npc_intents
        ],
        generation_control=generation_control,
        latest_band_checkpoint=generation_control.latest_band_checkpoint,
        blocking_reason=generation_control.blocking_reason,
        next_gate=generation_control.next_gate,
        decision_timeline=decision_timeline,
        narrative_constraints=narrative_constraints,
        recent_replans=[
            {
                "trigger_chapter": item.trigger_chapter,
                "risk_level": item.risk_level,
                "strategy": item.strategy,
                "status": item.status,
                "reason": item.reason,
                "cooldown_until_chapter": item.cooldown_until_chapter,
                "created_at": display_datetime(item.created_at),
            }
            for item in replan_events
        ],
    )


def latest_provisional_band_execution(
    session: Session,
    project_id: str,
    *,
    arc_id: str | None = None,
) -> ProvisionalBandExecution | None:
    stmt = select(ProvisionalBandExecution).where(
        ProvisionalBandExecution.project_id == project_id
    )
    if arc_id:
        stmt = stmt.where(ProvisionalBandExecution.arc_id == arc_id)
    return session.execute(
        stmt.order_by(ProvisionalBandExecution.created_at.desc()).limit(1)
    ).scalar_one_or_none()


def build_provisional_band_detail(
    *,
    session: Session,
    project_id: str,
    latest: ProvisionalBandExecution,
    display_datetime: DisplayDatetime,
) -> ProvisionalBandDetail:
    ledgers = list(
        session.execute(
            select(ProvisionalChapterLedger)
            .where(
                ProvisionalChapterLedger.project_id == project_id,
                ProvisionalChapterLedger.arc_id == latest.arc_id,
                ProvisionalChapterLedger.band_id == latest.band_id,
            )
            .order_by(
                ProvisionalChapterLedger.chapter_number.asc(),
                ProvisionalChapterLedger.created_at.asc(),
            )
        ).scalars().all()
    )
    chapter_numbers = []
    try:
        chapter_numbers = json.loads(latest.chapter_numbers_json or "[]") or []
    except (json.JSONDecodeError, TypeError):
        chapter_numbers = []

    return ProvisionalBandDetail(
        project_id=project_id,
        arc_id=latest.arc_id,
        band_id=latest.band_id,
        aggregate_verdict=latest.aggregate_verdict,
        preview_char_count=latest.preview_char_count,
        issue_count=latest.issue_count,
        failure_count=latest.failure_count,
        artifact_path=latest.artifact_path,
        chapter_numbers=[int(item) for item in chapter_numbers if isinstance(item, int)],
        created_at=display_datetime(latest.created_at),
        chapters=[
            ProvisionalChapterLedgerInfo(
                chapter_number=row.chapter_number,
                title=row.title,
                summary=row.summary,
                verdict=row.verdict,
                char_count=row.char_count,
                artifact_meta_path=row.artifact_meta_path,
                draft_blob_path=row.draft_blob_path,
                current_time_label=row.current_time_label,
                projected_time_label=row.projected_time_label,
                state_changes=_load_json_list(row.state_changes_json),
                events=_load_json_list(row.events_json),
                thread_beats=_load_json_list(row.thread_beats_json),
                time_advance=_json_object(row.time_advance_json),
                issues=_load_json_list(row.issues_json),
                error=row.error_text,
                created_at=display_datetime(row.created_at),
            )
            for row in ledgers
        ],
    )


def _json_list_strings(raw: str) -> list[str]:
    try:
        payload = json.loads(raw or "[]") or []
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(item) for item in payload if item is not None]


def _json_object(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}") or {}
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_json_list(raw: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(raw or "[]") or []
    except (json.JSONDecodeError, TypeError):
        return []
    return [item for item in payload if isinstance(item, dict)]
