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

    def _normalize_quota(value: Any, *, default: int = 0, minimum: int = 0) -> int:
        try:
            quota_value = int(value)
        except (TypeError, ValueError):
            quota_value = default
        return min(20, max(minimum, quota_value))

    quota = _normalize_quota(payload.get("daily_chapter_quota", 1), default=1, minimum=1)
    daily_plan_quota = _normalize_quota(payload.get("daily_plan_quota", 0), default=0, minimum=0)
    daily_write_raw = _normalize_quota(payload.get("daily_write_quota", 0), default=0, minimum=0)
    daily_write_quota = quota if daily_write_raw <= 0 else daily_write_raw
    daily_review_quota = _normalize_quota(payload.get("daily_review_quota", 0), default=0, minimum=0)
    auto_publish = bool(payload.get("auto_publish", False))
    daily_publish_raw = _normalize_quota(payload.get("daily_publish_quota", 0), default=0, minimum=0)
    daily_publish_quota = 1 if auto_publish and daily_publish_raw <= 0 else daily_publish_raw

    return ProjectAutomationSettings.model_validate(
        {
            "enabled": bool(payload.get("enabled", False)),
            "daily_start_time": time_text,
            "daily_chapter_quota": quota,
            "daily_plan_quota": daily_plan_quota,
            "daily_write_quota": daily_write_quota,
            "daily_review_quota": daily_review_quota,
            "daily_publish_quota": daily_publish_quota,
            "stop_when_review_pending": bool(payload.get("stop_when_review_pending", True)),
            "auto_publish": auto_publish,
            "publish": publish_payload,
            "publish_bindings": publish_bindings,
            "last_scheduler_date": str(payload.get("last_scheduler_date", "")).strip(),
            "last_scheduler_at": str(payload.get("last_scheduler_at", "")).strip(),
            "last_scheduler_action": str(payload.get("last_scheduler_action", "")).strip(),
            "last_scheduler_message": str(payload.get("last_scheduler_message", "")).strip(),
            "last_scheduler_task_id": str(payload.get("last_scheduler_task_id", "")).strip(),
        }
    )


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


def load_latest_scenario_rehearsal_by_project(
    session: Session,
    project_ids: list[str],
) -> dict[str, ScenarioRehearsalRunRow]:
    if not project_ids:
        return {}
    rows = list(
        session.execute(
            select(ScenarioRehearsalRunRow)
            .where(ScenarioRehearsalRunRow.project_id.in_(project_ids))
            .order_by(
                ScenarioRehearsalRunRow.project_id.asc(),
                ScenarioRehearsalRunRow.created_at.desc(),
                ScenarioRehearsalRunRow.id.desc(),
            )
        ).scalars().all()
    )
    latest: dict[str, ScenarioRehearsalRunRow] = {}
    for row in rows:
        latest.setdefault(row.project_id, row)
    return latest


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
    scenario_rehearsal_map = load_latest_scenario_rehearsal_by_project(session, project_ids)
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
        "scenario_rehearsal_map": scenario_rehearsal_map,
        "latest_arc_structure_map": latest_arc_structure_map,
        "latest_band_experience_map": latest_band_experience_map,
        "recent_replans_map": recent_replans_map,
        "recent_npc_map": recent_npc_map,
    }


__all__ = [
    'load_recent_replan_events_by_project',
    'load_recent_npc_intents_by_project',
    '_load_latest_arc_structure_by_project',
    '_load_latest_band_experience_by_project',
    'normalize_project_automation',
    'load_project_upload_stats',
    'load_latest_scenario_rehearsal_by_project',
    'load_project_runtime_maps',
]
