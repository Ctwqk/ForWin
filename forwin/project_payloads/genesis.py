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


__all__ = [
    '_normalize_genesis_pack',
    '_load_latest_genesis_revision_by_project',
    '_stage_overview_from_revision',
    '_can_start_writing',
    '_prompt_trace_infos',
]
