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

# Backwards-compatible aliases for tests and local integrations while api.py is being split.
_build_runtime_config = build_runtime_config
_build_saved_runtime_config = build_saved_runtime_config
_run_generation_with_config = run_generation_with_config
_run_continue_project_with_config = run_continue_project_with_config

from forwin.api_core import state as api_state
from forwin.api_core.runtime import *
from forwin.api_core.tasks import *
from forwin.api_core.project_helpers import *
from forwin.api_core.generation import *

def _automation_daily_start_minutes(automation: ProjectAutomationSettings) -> int:
    return api_automation.automation_daily_start_minutes(automation)


def _load_automation_scheduler_metrics(
    session,
    project_ids: list[str],
) -> tuple[dict[str, int], dict[str, int], dict[str, list[int]], set[str]]:
    return api_automation.load_automation_scheduler_metrics(
        session,
        project_ids,
        terminal_statuses=api_state._GENERATION_TERMINAL_STATUSES,
    )


def _run_scheduled_review_action(project_id: str, chapter_number: int) -> Any:
    if api_state._orchestrator is None:
        return None
    return api_state._orchestrator.accept_review(
        project_id,
        int(chapter_number),
        reason="production_scheduler_review_quota",
    )


def _run_automation_scheduler_pass() -> None:
    production_scheduler_factory = None
    if api_state._runtime_container is not None:
        production_scheduler_factory = api_state._runtime_container.services().production_scheduler
    return api_automation.run_automation_scheduler_pass(
        session_factory=api_state._SessionFactory,
        config=api_state._config,
        saved_runtime_config_or_503=_saved_runtime_config_or_503,
        utcnow=_utcnow,
        display_tz=api_state._DISPLAY_TZ,
        display_datetime=_display_datetime,
        get_session=_get_session,
        persist_project_automation=_persist_project_automation,
        create_generation_task=_create_generation_task,
        create_continue_generation_task=_create_continue_generation_task,
        active_generation_task_error_cls=ActiveGenerationTaskError,
        terminal_statuses=api_state._GENERATION_TERMINAL_STATUSES,
        review_chapter=_run_scheduled_review_action,
        approve_chapter_review=_run_scheduled_review_action,
        production_scheduler_factory=production_scheduler_factory,
    )


def _automation_scheduler_loop() -> None:
    while not api_state._automation_scheduler_stop.wait(30.0):
        try:
            _run_automation_scheduler_pass()
        except Exception:  # noqa: BLE001
            logger.exception("Automation scheduler loop failed.")


def _start_automation_scheduler() -> None:
    if api_state._automation_scheduler_thread is not None and api_state._automation_scheduler_thread.is_alive():
        return
    api_state._automation_scheduler_stop.clear()
    api_state._automation_scheduler_thread = threading.Thread(
        target=_automation_scheduler_loop,
        name="forwin-automation-scheduler",
        daemon=True,
    )
    api_state._automation_scheduler_thread.start()


def _stop_automation_scheduler() -> None:
    api_state._automation_scheduler_stop.set()
    thread = api_state._automation_scheduler_thread
    api_state._automation_scheduler_thread = None
    if thread is not None and thread.is_alive():
        thread.join(timeout=2.0)


def _list_generation_tasks(limit: int) -> list[tuple[str, dict[str, Any]]]:
    _prune_tasks(include_db=False)
    normalized_limit = max(1, min(int(limit or 30), 100))
    if api_state._SessionFactory is None:
        with api_state._tasks_lock:
            return [
                (task_id, dict(task))
                for task_id, task in sorted(
                    api_state._tasks.items(),
                    key=lambda item: item[1].get("updated_at", _utcnow()),
                    reverse=True,
                )
                if not task.get("deleted")
            ][:normalized_limit]

    with _get_session() as session:
        rows = session.execute(
            select(GenerationTask)
            .where(GenerationTask.deleted_at.is_(None))
            .order_by(GenerationTask.updated_at.desc())
            .limit(normalized_limit)
        ).scalars().all()
        merged: dict[str, dict[str, Any]] = {}
        for row in rows:
            persisted = _generation_task_from_row(row)
            cached = _cached_generation_task(row.id)
            task = _prefer_cached_generation_task(persisted, cached)
            if task is not None:
                task = _augment_task_with_provisional_history(session, task)
                visible = _apply_task_visibility_rules(task, include_deleted=False)
                if visible is not None:
                    merged[row.id] = visible
        with api_state._tasks_lock:
            cached_items = [(task_id, dict(task)) for task_id, task in api_state._tasks.items()]
        for task_id, cached in cached_items:
            visible = _apply_task_visibility_rules(cached, include_deleted=False)
            if visible is None:
                continue
            current = merged.get(task_id)
            merged[task_id] = _prefer_cached_generation_task(current, visible) or visible
        return sorted(
            merged.items(),
            key=lambda item: _coerce_task_datetime(item[1].get("updated_at")),
            reverse=True,
        )[:normalized_limit]



__all__ = [name for name in globals() if not name.startswith("__")]
