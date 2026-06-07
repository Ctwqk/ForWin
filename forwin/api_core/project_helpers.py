"""ForWin Web API – FastAPI interface for the novel generation system."""
from __future__ import annotations

import logging
import os
import threading
import uuid
import json
import io
import inspect
import time
import zipfile
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from forwin.api_pages import render_home_page, render_publishers_page
from forwin import (
    api_automation,
    api_governance_ops,
    api_governance_routes,
    api_governance_support,
    api_observability_routes,
    api_project_ops,
    api_project_routes,
    api_publisher_ops,
    api_publisher_routes,
    api_route_registry,
    api_system_routes,
    api_task_routes,
)
from forwin.api_task_center_service import TaskCenterService
from forwin.api_project_payloads import (
    build_project_detail,
    build_project_summaries,
    build_provisional_band_detail,
    latest_provisional_band_execution,
    normalize_project_automation,
)
from forwin.api_runtime import (
    build_home_page_settings,
    build_runtime_config,
    build_saved_runtime_config,
    copy_config,
    run_continue_project_with_config,
    run_generation_with_config,
)
from forwin.api_task_history import augment_task_with_rehearsal_history
from forwin.api_auth import basic_auth_enabled, make_basic_auth_middleware
from forwin.api_schemas import (
    BandCheckpointApproveRequest,
    BandCheckpointDetail,
    BandExperienceOverrideRequest,
    BandExperienceOverrideResponse,
    ActiveGenerationTaskCheckResponse,
    BookGenesisDetail,
    BookGenesisPatchRequest,
    BookGenesisRefineRequest,
    BookGenesisStageRunRequest,
    CausalReplayResponse,
    CandidateDraftDetail,
    DecisionEventsResponse,
    BulkDeleteResponse,
    ChapterDetail,
    ChapterInfo,
    ChapterReviewApproveRequest,
    ChapterReviewApproveResponse,
    ChapterReviewDetail,
    ChapterReviewIssueInfo,
    CommentSyncJobResultRequest,
    EntityInfo,
    ExtensionClaimCommentSyncJobRequest,
    ExtensionClaimCommentSyncJobResponse,
    ExtensionClaimUploadJobRequest,
    ExtensionClaimUploadJobResponse,
    ExtensionCommentsBatchRequest,
    ExtensionCommentsBatchResponse,
    ExtensionHeartbeatRequest,
    ExtensionHeartbeatResponse,
    ExtensionPlatformHeartbeat,
    ExtensionBrowserSessionResponse,
    ExtensionSessionSyncRequest,
    ExtensionSessionSyncResponse,
    GenerateRequest,
    GenerationControlInfo,
    LLMDefaultProfileRequest,
    LLMPreferencesRequest,
    LLMProfileUpsertRequest,
    LLMSettingsRequest,
    LLMSettingsResponse,
    ModelProfile,
    NarrativeConstraintCreateRequest,
    NarrativeConstraintUpdateRequest,
    NarrativeConstraintsResponse,
    GovernanceInsightsResponse,
    ManualCheckpointRequest,
    ProjectArcSnapshotFields,
    ProjectChapterPublishRequest,
    ProjectCreateRequest,
    ProjectCreateResponse,
    ProjectContinueGenerationRequest,
    ProjectAutomationSettings,
    ProjectAutomationUpdateRequest,
    ProjectAutomationUpdateResponse,
    ProjectBulkDeleteRequest,
    ProjectDeleteResponse,
    ProjectDetail,
    ProjectGovernanceResponse,
    ProjectGovernanceUpdateRequest,
    ProjectSummary,
    ProvisionalBandDetail,
    ProvisionalChapterLedgerInfo,
    PublisherCommentSyncJobRequest,
    PublisherCommentSyncJobResponse,
    PublisherPlatformInfo,
    PublisherRawCommentInput,
    PublisherUploadJobCreateRequest,
    PublisherUploadJobResponse,
    TaskResponse,
    TaskCenterItemResponse,
    TaskBulkDeleteRequest,
    TaskMutationResponse,
    TaskContractResponse,
    TaskContractUpdateRequest,
    TaskSummaryResponse,
    ThreadInfo,
    TropeTemplateInfo,
    TropeRegistrySummaryResponse,
    TropeTemplateValidationRequest,
    TropeTemplateValidationResponse,
    UploadJobResultRequest,
    LintSignalInfo,
    StartWritingResponse,
)
from forwin.book_genesis import BookGenesisService, GENESIS_STAGE_ORDER, StaleGenesisRevisionError
from forwin.config import Config
from forwin.governance import (
    BandCheckpointIssueInfo,
    CONSTRAINT_LEVELS,
    CONSTRAINT_STATUSES,
    CONSTRAINT_TYPES,
    DecisionEventType,
    DecisionEventInfo,
    NarrativeConstraintInfo,
    ensure_decision_event_type,
    issue_group_for_issue,
    load_plan_task_contract,
    new_project_governance,
    normalize_project_governance,
    plan_task_contract_to_json,
)
from forwin.models.base import Base, get_session_factory
from forwin.models.genesis import BookGenesisRevision
from forwin.models.project import Project, ChapterPlan, ArcPlanVersion
from forwin.models.entity import Entity
from forwin.models.event import CanonEvent, EventEntityLink
from forwin.models.governance import BandCheckpoint, DecisionEvent, NarrativeConstraint
from forwin.models.publisher import PublisherCommentSyncJob, PublisherConnectionState, PublisherExtensionClient, PublisherRawComment, PublisherUploadJob
from forwin.models.thread import PlotThread
from forwin.models.task import GenerationTask
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.phase import (
    BandExperiencePlan,
    ChapterRewriteAttempt,
)
from forwin.models.timeline import ChapterTimeline, StoryTimePoint
from forwin.models.phase4 import NPCIntentSnapshot
import forwin.models.phase  # noqa: F401
from forwin.protocol.experience import BandDelightSchedule
from forwin.protocol.trope_library import (
    TROPE_TEMPLATE_LIBRARY,
    trope_registry_summary,
    validate_trope_template_payload,
)
from forwin.state.repo import StateRepository
from forwin.orchestrator.loop import WritingOrchestrator
from forwin.orchestrator.feedback_aggregator import derive_action_effectiveness
from forwin.publisher_runtime.codex_intervention import build_codex_intervention_handler
from forwin.publishers import PublisherManager
from forwin.runtime.container import RuntimeContainer
from forwin.runtime_settings import RuntimeSettingsStore
from forwin.state.query_helpers import load_latest_drafts_by_plan_id
from forwin.state.updater import StateUpdater

logger = logging.getLogger(__name__)

from forwin.api_core import state as api_state
from forwin.api_core.runtime import *
from forwin.api_core.tasks import *

def _delete_project(session, project_id: str) -> None:
    chapter_plan_ids = session.execute(
        select(ChapterPlan.id).where(ChapterPlan.project_id == project_id)
    ).scalars().all()
    if chapter_plan_ids:
        draft_ids = session.execute(
            select(ChapterDraft.id).where(ChapterDraft.chapter_plan_id.in_(chapter_plan_ids))
        ).scalars().all()
        session.execute(
            delete(CandidateDraftRecord).where(CandidateDraftRecord.project_id == project_id)
        )
        session.execute(
            delete(ChapterRewriteAttempt).where(ChapterRewriteAttempt.project_id == project_id)
        )
        if draft_ids:
            session.execute(
                delete(ChapterReview).where(ChapterReview.draft_id.in_(draft_ids))
            )
            session.execute(
                delete(ChapterDraft).where(ChapterDraft.id.in_(draft_ids))
            )

    thread_ids = session.execute(
        select(PlotThread.id).where(PlotThread.project_id == project_id)
    ).scalars().all()
    if thread_ids:
        session.execute(
            delete(Base.metadata.tables["plot_thread_beats"]).where(
                Base.metadata.tables["plot_thread_beats"].c.thread_id.in_(thread_ids)
            )
        )

    entity_ids = session.execute(
        select(Entity.id).where(Entity.project_id == project_id)
    ).scalars().all()
    if entity_ids:
        session.execute(
            delete(Base.metadata.tables["entity_states"]).where(
                Base.metadata.tables["entity_states"].c.entity_id.in_(entity_ids)
            )
        )

    event_ids = session.execute(
        select(CanonEvent.id).where(CanonEvent.project_id == project_id)
    ).scalars().all()
    if event_ids:
        session.execute(
            delete(EventEntityLink).where(EventEntityLink.event_id.in_(event_ids))
        )

    time_point_ids = session.execute(
        select(StoryTimePoint.id).where(StoryTimePoint.project_id == project_id)
    ).scalars().all()
    if time_point_ids:
        session.execute(
            delete(ChapterTimeline).where(
                (ChapterTimeline.start_time_id.in_(time_point_ids))
                | (ChapterTimeline.end_time_id.in_(time_point_ids))
            )
        )

    session.execute(
        delete(NPCIntentSnapshot).where(NPCIntentSnapshot.project_id == project_id)
    )

    for table in reversed(Base.metadata.sorted_tables):
        if table.name == "projects" or "project_id" not in table.c:
            continue
        session.execute(delete(table).where(table.c.project_id == project_id))

    session.execute(delete(Project).where(Project.id == project_id))


def _update_task(task_id: str, **changes: Any) -> None:
    task = _load_generation_task(task_id, include_deleted=True)
    if task is None or task.get("deleted"):
        return
    normalized = dict(changes)
    normalized.pop("requested_chapters", None)
    if task.get("cancel_requested") and normalized.get("status") in {"starting", "running", "needs_review"}:
        normalized.pop("status", None)
    if task.get("cancel_requested") and normalized.get("current_stage") not in {"terminating", "cancelled"}:
        normalized.pop("current_stage", None)
    if task.get("pause_requested") and normalized.get("status") in {"queued", "starting", "running"}:
        normalized.pop("status", None)
    if task.get("pause_requested") and normalized.get("current_stage") not in {"paused", "cancelled", "terminating"}:
        normalized.pop("current_stage", None)
    if "message" in normalized:
        normalized["message"] = str(normalized.get("message") or "")
    if "status" in normalized and normalized["status"] == "terminating":
        normalized["current_stage"] = "terminating"
    elif "status" in normalized:
        terminal_stage = api_state._GENERATION_TERMINAL_STAGE_BY_STATUS.get(str(normalized["status"]).strip())
        if terminal_stage:
            normalized["current_stage"] = terminal_stage
    if "current_chapter" in normalized:
        try:
            normalized["current_chapter"] = int(normalized["current_chapter"] or 0)
        except (TypeError, ValueError):
            normalized["current_chapter"] = 0
    now = _utcnow()
    next_status = str(normalized.get("status", task.get("status", "")) or "").strip()
    if next_status == "running" and str(task.get("lease_owner", "") or "").strip():
        normalized["heartbeat_at"] = now
        normalized["lease_expires_at"] = now + timedelta(
            seconds=_running_task_lease_seconds(task)
        )
    if normalized.get("status") == "paused":
        if bool(task.get("pause_requested")) or bool(normalized.get("pause_requested")):
            normalized["pause_requested"] = True
        normalized["paused_at"] = now
    next_stage = str(normalized.get("current_stage", "")).strip()
    if next_stage and next_stage != str(task.get("current_stage", "")).strip():
        history = list(task.get("stage_history", []))
        history.append(
            _new_stage_history_entry(
                next_stage,
                now=now,
                current_chapter=int(normalized.get("current_chapter", task.get("current_chapter", 0)) or 0),
                message=str(normalized.get("message", task.get("message", ""))).strip(),
            )
        )
        normalized["stage_history"] = history

    task.update(normalized)
    task["updated_at"] = now
    _sync_task_cache(task_id, task)

    if api_state._SessionFactory is not None:
        def _operation() -> None:
            with _get_session() as session:
                row = session.get(GenerationTask, task_id)
                if row is None:
                    row = GenerationTask(id=task_id)
                _apply_generation_task_to_row(row, task, now=now)
                session.add(row)
                session.commit()

        try:
            _run_generation_task_db_write(_operation, context=f"update_generation_task:{task_id}")
        except GenerationTaskPersistenceError as exc:
            if task.get("status") in api_state._GENERATION_TERMINAL_STATUSES:
                raise
            _mark_task_persistence_degraded(task_id, task, exc)
        else:
            _clear_task_persistence_degraded(task_id, task)


def _running_task_lease_seconds(task: dict[str, Any]) -> int:
    heartbeat_at = _coerce_task_datetime(task.get("heartbeat_at"))
    lease_expires_at = _coerce_task_datetime(task.get("lease_expires_at"))
    if heartbeat_at > datetime.min.replace(tzinfo=timezone.utc) and lease_expires_at > heartbeat_at:
        return max(30, int((lease_expires_at - heartbeat_at).total_seconds()))
    return 300


def _task_should_abort(task_id: str) -> bool:
    task = _load_generation_task(task_id)
    if task is None or task.get("deleted"):
        return True
    return bool(task.get("cancel_requested"))


def _task_should_pause(task_id: str) -> bool:
    task = _load_generation_task(task_id)
    if task is None or task.get("deleted"):
        return False
    return bool(task.get("pause_requested")) and not bool(task.get("cancel_requested"))


def _get_generation_task_or_404(task_id: str) -> dict[str, Any]:
    _prune_tasks(include_db=False)
    task = _load_generation_task(task_id)
    if task is None or task.get("deleted"):
        raise HTTPException(404, "任务不存在")
    return task


def _saved_runtime_config_or_503() -> Config:
    if not api_state._config:
        raise HTTPException(503, "服务尚未初始化")
    return build_saved_runtime_config(
        base_config=api_state._config,
        runtime_settings=api_state._runtime_settings,
    )


def _require_reason(reason: str, *, action: str) -> str:
    normalized = str(reason or "").strip()
    if not normalized:
        raise HTTPException(400, f"{action} 必须填写 reason。")
    return normalized


def _validate_constraint_payload(*, constraint_type: str, level: str, status: str) -> tuple[str, str, str]:
    return api_governance_support.validate_constraint_payload(
        constraint_type=constraint_type,
        level=level,
        status=status,
    )


def _persist_project_automation(
    session,
    project: Project,
    automation: ProjectAutomationSettings,
) -> ProjectAutomationSettings:
    return api_governance_support.persist_project_automation(session, project, automation)


def _governance_request_payload(req: object) -> dict[str, object]:
    return api_governance_support.governance_request_payload(req)


def _resolve_project_governance(
    project: Project | None,
    *,
    overrides: dict[str, object] | None = None,
    base_config: Config | None = None,
) -> object:
    return api_governance_support.resolve_project_governance(
        project,
        overrides=overrides,
        base_config=base_config,
    )


def _persist_project_governance(
    session,
    project: Project,
    governance,
) -> object:
    return api_governance_support.persist_project_governance(
        session,
        project,
        governance,
        base_config=api_state._config,
    )


def _log_decision_event(session, **kwargs):
    return api_governance_support.log_decision_event(session, **kwargs)


def _latest_band_checkpoint_row(session, *, project_id: str, band_id: str = ""):
    return api_governance_support.latest_band_checkpoint_row(
        session,
        project_id=project_id,
        band_id=band_id,
    )


def _serialize_band_checkpoint(row: BandCheckpoint, *, session=None) -> BandCheckpointDetail:
    return api_governance_support.serialize_band_checkpoint(row, session=session)


def _serialize_constraint(row: NarrativeConstraint) -> NarrativeConstraintInfo:
    return api_governance_support.serialize_constraint(row)


def _serialize_decision_event(row: DecisionEvent) -> DecisionEventInfo:
    return api_governance_support.serialize_decision_event(row)


def _decision_event_stmt(**kwargs):
    return api_governance_support.decision_event_stmt(**kwargs)


def _list_decision_event_rows(session, **kwargs) -> list[DecisionEvent]:
    return api_governance_support.list_decision_event_rows(session, **kwargs)


def _latest_related_decision_event(session, **kwargs) -> DecisionEvent | None:
    return api_governance_support.latest_related_decision_event(session, **kwargs)


def _decision_refs_for_checkpoint(session, row: BandCheckpoint) -> list[DecisionEventInfo]:
    return api_governance_support.decision_refs_for_checkpoint(session, row)


def _decision_refs_for_chapter_review(
    session,
    *,
    project_id: str,
    chapter_number: int,
    review_id: str,
) -> list[DecisionEventInfo]:
    return api_governance_support.decision_refs_for_chapter_review(
        session,
        project_id=project_id,
        chapter_number=chapter_number,
        review_id=review_id,
    )


def _counter_rows(counter: Counter[str], *, limit: int = 5) -> list[dict[str, Any]]:
    return api_governance_support.counter_rows(counter, limit=limit)


def _build_causal_replay(
    session,
    *,
    project_id: str,
    scope: str = "",
    arc_id: str = "",
    band_id: str = "",
    chapter_number: int = 0,
    task_id: str = "",
) -> CausalReplayResponse:
    return api_governance_support.build_causal_replay(
        session,
        project_id=project_id,
        scope=scope,
        arc_id=arc_id,
        band_id=band_id,
        chapter_number=chapter_number,
        task_id=task_id,
    )


def _build_governance_insights(session, *, project_id: str) -> GovernanceInsightsResponse:
    return api_governance_support.build_governance_insights(session, project_id=project_id)



__all__ = [name for name in globals() if not name.startswith("__")]
