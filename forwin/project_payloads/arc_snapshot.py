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
from .common import (
    _deep_merge_dict,
    _json_list_strings,
    _json_object,
    _latest_rows_by_project,
    _load_json_list,
    _normalized_project_ids,
    _recent_rows_by_project,
)


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
    normalized_status = normalize_checkpoint_status(row.status)
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
        status=normalized_status,
        summary=str(row.summary or ""),
        reason=checkpoint_reason_with_legacy_status(row.reason, row.status),
        issues=issues,
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
        resolved_at=(
            (row.resolved_at or (row.updated_at if normalized_status in {"pass", "overridden"} else None)).isoformat()
            if row.resolved_at or (normalized_status in {"pass", "overridden"} and row.updated_at)
            else ""
        ),
    )


def project_arc_snapshot_payload(
    latest_arc_envelope,
    latest_arc_analysis,
    latest_provisional,
    latest_arc_structure=None,
    latest_band_experience=None,
    latest_scenario_rehearsal=None,
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
        "scenario_rehearsal_band_id": "",
        "scenario_rehearsal_recommendation": "",
        "scenario_rehearsal_risk_count": 0,
        "scenario_rehearsal_blocker_count": 0,
        "scenario_rehearsal_required_patch_count": 0,
        "scenario_rehearsal_resolution_status": "",
        "scenario_rehearsal_trigger_reasons": [],
        "scenario_rehearsal_patch_attempt_count": 0,
        "scenario_rehearsal_checkpoint_id": "",
        "scenario_rehearsal_replan_event_id": "",
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
    if latest_scenario_rehearsal is not None:
        rehearsal_report = _json_object(getattr(latest_scenario_rehearsal, "report_json", "{}"))
        payload.update(
            {
                "scenario_rehearsal_band_id": latest_scenario_rehearsal.band_id,
                "scenario_rehearsal_recommendation": latest_scenario_rehearsal.recommendation,
                "scenario_rehearsal_risk_count": latest_scenario_rehearsal.risk_count,
                "scenario_rehearsal_blocker_count": latest_scenario_rehearsal.blocker_count,
                "scenario_rehearsal_required_patch_count": latest_scenario_rehearsal.required_patch_count,
                "scenario_rehearsal_resolution_status": str(rehearsal_report.get("resolution_status") or ""),
                "scenario_rehearsal_trigger_reasons": [
                    str(item)
                    for item in (rehearsal_report.get("trigger_reasons") or [])
                    if str(item).strip()
                ],
                "scenario_rehearsal_patch_attempt_count": int(rehearsal_report.get("patch_attempt_count") or 0),
                "scenario_rehearsal_checkpoint_id": str(rehearsal_report.get("checkpoint_id") or ""),
                "scenario_rehearsal_replan_event_id": str(rehearsal_report.get("replan_event_id") or ""),
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


__all__ = [
    '_latest_band_checkpoint_by_project',
    '_decision_timeline_by_project',
    '_narrative_constraints_by_project',
    '_band_checkpoint_detail',
    'project_arc_snapshot_payload',
]
