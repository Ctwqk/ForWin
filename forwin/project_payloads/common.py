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


__all__ = [
    '_deep_merge_dict',
    '_normalized_project_ids',
    '_recent_rows_by_project',
    '_latest_rows_by_project',
    '_json_list_strings',
    '_json_object',
    '_load_json_list',
]
