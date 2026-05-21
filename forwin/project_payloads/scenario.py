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


def latest_scenario_rehearsal_run(
    session: Session,
    project_id: str,
    *,
    arc_id: str | None = None,
) -> ScenarioRehearsalRunRow | None:
    stmt = select(ScenarioRehearsalRunRow).where(
        ScenarioRehearsalRunRow.project_id == project_id
    )
    if arc_id:
        stmt = stmt.where(ScenarioRehearsalRunRow.arc_id == arc_id)
    return session.execute(
        stmt.order_by(
            ScenarioRehearsalRunRow.created_at.desc(),
            ScenarioRehearsalRunRow.id.desc(),
        ).limit(1)
    ).scalar_one_or_none()


def build_scenario_rehearsal_detail(
    *,
    project_id: str,
    latest: ScenarioRehearsalRunRow,
    display_datetime: DisplayDatetime,
) -> ScenarioRehearsalDetail:
    chapter_numbers = _json_list_strings(latest.chapter_numbers_json)
    trigger_reasons = _json_list_strings(latest.trigger_reasons_json)
    return ScenarioRehearsalDetail(
        project_id=project_id,
        arc_id=str(latest.arc_id or ""),
        band_id=str(latest.band_id or ""),
        rehearsal_scope=str(latest.rehearsal_scope or "band"),
        chapter_numbers=[
            int(item)
            for item in chapter_numbers
            if str(item).strip().lstrip("-").isdigit()
        ],
        trigger_reasons=trigger_reasons,
        recommendation=str(latest.recommendation or "pass"),
        risk_count=int(latest.risk_count or 0),
        blocker_count=int(latest.blocker_count or 0),
        required_patch_count=int(latest.required_patch_count or 0),
        resolution_status=str(_json_object(latest.report_json).get("resolution_status") or ""),
        patch_attempt_count=int(_json_object(latest.report_json).get("patch_attempt_count") or 0),
        checkpoint_id=str(_json_object(latest.report_json).get("checkpoint_id") or ""),
        replan_event_id=str(_json_object(latest.report_json).get("replan_event_id") or ""),
        report=_json_object(latest.report_json),
        created_at=display_datetime(latest.created_at),
    )


__all__ = [
    'latest_scenario_rehearsal_run',
    'build_scenario_rehearsal_detail',
]
