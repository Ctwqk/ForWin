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
from forwin.api_core.project_helpers import *
from forwin.api_core.generation import *
from forwin.api_core.automation import *

def _shutdown_runtime_state() -> None:

    _stop_automation_scheduler()
    if api_state._orchestrator is not None:
        try:
            api_state._orchestrator.llm_client.close()
        except Exception:  # noqa: BLE001
            logger.debug("Ignoring orchestrator LLM client shutdown error.", exc_info=True)
        try:
            api_state._orchestrator.engine.dispose()
        except Exception:  # noqa: BLE001
            logger.debug("Ignoring orchestrator engine shutdown error.", exc_info=True)

    if api_state._engine is not None:
        try:
            api_state._engine.dispose()
        except Exception:  # noqa: BLE001
            logger.debug("Ignoring API engine shutdown error.", exc_info=True)

    api_state._orchestrator = None
    api_state._runtime_container = None
    api_state._publisher_manager = None
    api_state._runtime_settings = None
    api_state._task_center_service = None
    api_state._SessionFactory = None
    api_state._engine = None


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):

    if api_state._config is None:
        api_state._config = Config.from_env()
    if str(api_state._config.http_bind or "").strip() in {"0.0.0.0", "::"} and not basic_auth_enabled(api_state._config):
        logger.warning(
            "ForWin is reachable beyond localhost and HTTP Basic Auth is disabled. "
            "This is acceptable only on a trusted LAN."
        )
    if api_state._runtime_container is None:
        database_url = os.environ.get("FORWIN_DATABASE_URL", api_state._config.database_url)
        api_state._config = api_state._config.model_copy(update={"database_url": database_url})
        api_state._runtime_container = RuntimeContainer.from_config(api_state._config, role="api")
        runtime_services = api_state._runtime_container.services()
        api_state._engine = runtime_services.engine
        api_state._SessionFactory = runtime_services.session_factory
    if api_state._SessionFactory is None:
        api_state._SessionFactory = get_session_factory(api_state._engine)
    if api_state._orchestrator is None:
        api_state._orchestrator = api_state._runtime_container.build_writing_orchestrator() if api_state._runtime_container is not None else WritingOrchestrator(api_state._config)
        with api_state._SessionFactory() as bootstrap_session:
            created_envelopes = api_state._orchestrator.arc_envelope_manager.backfill_missing_resolutions(
                session=bootstrap_session
            )
            if created_envelopes:
                bootstrap_session.commit()
                logger.info("Backfilled %d active arc envelopes.", created_envelopes)
            else:
                bootstrap_session.rollback()
    recovered_generation_ids = _recover_interrupted_generation_tasks()
    if recovered_generation_ids:
        logger.info(
            "Recovered %d interrupted generation tasks after restart.",
            len(recovered_generation_ids),
        )
    if api_state._publisher_manager is None:
        api_state._publisher_manager = PublisherManager(
            api_state._SessionFactory,
            extension_api_key=api_state._config.publisher_extension_api_key,
            preferred_client_id=api_state._config.publisher_preferred_client_id,
            strict_preferred_client=api_state._config.publisher_strict_preferred_client,
            publisher_session_secret=api_state._config.publisher_session_secret,
            publisher_session_encryption_required=api_state._config.publisher_session_encryption_required,
            publisher_login_discord_webhook_url=api_state._config.publisher_login_discord_webhook_url,
            codex_intervention_handler=build_codex_intervention_handler(api_state._config),
        )
    api_state._publisher_manager.requeue_interrupted_upload_jobs()
    if api_state._runtime_settings is None:
        api_state._runtime_settings = RuntimeSettingsStore(
            api_state._config.runtime_settings_path,
            default_api_key=api_state._config.minimax_api_key,
            default_base_url=api_state._config.minimax_base_url,
            default_model=api_state._config.minimax_model,
            default_operation_mode=api_state._config.operation_mode,
            default_freeze_failed_candidates=api_state._config.freeze_failed_candidates,
            default_min_chapter_chars=api_state._config.min_chapter_chars,
            default_review_interval_chapters=api_state._config.review_interval_chapters,
            default_progression_mode=api_state._config.progression_mode,
            default_auto_band_checkpoint=api_state._config.auto_band_checkpoint,
            default_band_warn_action=api_state._config.band_warn_action,
            default_manual_checkpoints_enabled=api_state._config.manual_checkpoints_enabled,
            default_future_constraints_enabled=api_state._config.future_constraints_enabled,
            default_skill_runtime_enabled=api_state._config.skill_runtime_enabled,
            default_skill_registry_path=api_state._config.skill_registry_path,
            default_skill_strictness=api_state._config.skill_strictness,
            default_enabled_skill_groups=api_state._config.enabled_skill_groups,
            default_disabled_skill_ids=api_state._config.disabled_skill_ids,
            env_llm_profiles=api_state._config.llm_env_profiles,
        )
    _start_automation_scheduler()
    logger.info("ForWin API started. DB: %s", api_state._engine.url.render_as_string(hide_password=True))
    try:
        yield
    finally:
        logger.info("ForWin API shutting down.")
        _shutdown_runtime_state()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="ForWin – 长篇中文网文生成系统",
    version="0.5.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def optional_basic_auth(request: Request, call_next):
    config = api_state._config
    if config is None or not basic_auth_enabled(config):
        return await call_next(request)
    return await make_basic_auth_middleware(config)(request, call_next)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

_observability_handlers = api_observability_routes.build_handlers(
    get_config=lambda: api_state._config,
    get_session=_get_session,
    list_decision_event_rows=lambda session, **kwargs: _list_decision_event_rows(session, **kwargs),
    serialize_decision_event=lambda row: _serialize_decision_event(row),
    display_datetime=_display_datetime,
    json_load_object=lambda raw: _json_load_object(raw),
    json_load_list=lambda raw: _json_load_list(raw),
)
get_task_timeline = _observability_handlers["get_task_timeline"]
get_chapter_observability_ledger = _observability_handlers["get_chapter_observability_ledger"]
get_prompt_trace_detail = _observability_handlers["get_prompt_trace_detail"]
read_artifact_preview = _observability_handlers["read_artifact_preview"]
get_task_performance_report = _observability_handlers["get_task_performance_report"]
get_project_performance_report = _observability_handlers["get_project_performance_report"]
get_chapter_performance_report = _observability_handlers["get_chapter_performance_report"]
get_slow_performance_spans = _observability_handlers["get_slow_performance_spans"]
get_llm_performance_report = _observability_handlers["get_llm_performance_report"]
get_db_performance_report = _observability_handlers["get_db_performance_report"]

globals().update(
    api_route_registry.register_api_routes(
        app,
        deps=api_route_registry.ApiRouteDeps(
            core=api_route_registry.CoreDeps(
                get_config=lambda: api_state._config,
                get_runtime_settings=lambda: api_state._runtime_settings,
                get_orchestrator=lambda: api_state._orchestrator,
                get_session=_get_session,
                render_home_page=render_home_page,
                build_home_page_settings=build_home_page_settings,
                build_runtime_config=build_runtime_config,
                copy_config=copy_config,
                serialize_llm_settings=lambda payload, *, message: _serialize_llm_settings(payload, message=message),
                active_generation_task_error_cls=ActiveGenerationTaskError,
                display_datetime=_display_datetime,
                json_load_object=lambda raw: _json_load_object(raw),
            ),
            task=api_route_registry.TaskDeps(
                create_generation_task=lambda **kwargs: _create_generation_task(**kwargs),
                serialize_task=lambda task_id, task: _serialize_task(task_id, task),
                get_generation_task_or_404=lambda task_id: _get_generation_task_or_404(task_id),
                project_has_active_generation_task=lambda project_id, *, session=None: _project_has_active_generation_task(project_id, session=session),
                active_generation_task_ids=lambda project_id='': _active_generation_task_ids(project_id),
                generation_task_conflict_message=lambda project_id: _generation_task_conflict_message(project_id),
                list_generation_tasks=lambda limit: _list_generation_tasks(limit),
                serialize_generation_task_center_item=lambda task_id, task: _serialize_generation_task_center_item(task_id, task),
                serialize_upload_task_center_item=lambda payload: _serialize_upload_task_center_item(payload),
                list_project_backed_task_items=lambda limit: _list_project_backed_task_items(limit),
                parse_project_task_id=lambda task_id: _parse_project_task_id(task_id),
                get_project_backed_task_item_or_404=lambda task_id: _get_project_backed_task_item_or_404(task_id),
                task_is_terminal=lambda status: _task_is_terminal(status),
                task_is_terminable=lambda task: _task_is_terminable(task),
                task_is_pausable=lambda task: _task_is_pausable(task),
                task_is_deletable=lambda task: _task_is_deletable(task),
                update_task=lambda task_id, **changes: _update_task(task_id, **changes),
                create_continue_generation_task=lambda **kwargs: _create_continue_generation_task(**kwargs),
                get_task_timeline=get_task_timeline,
            ),
            project=api_route_registry.ProjectDeps(
                build_genesis_service=lambda *args, **kwargs: _build_genesis_service(*args, **kwargs),
                close_genesis_service=lambda service=None: _close_genesis_service(service),
                require_genesis_project=lambda project: _require_genesis_project(project),
                active_genesis_revision=lambda session, project: _active_genesis_revision(session, project),
                genesis_patch_payload=lambda req: _genesis_patch_payload(req),
                delete_project_impl=lambda session, project_id: _delete_project(session, project_id),
                project_delete_blockers=lambda project_id, *, session: _project_delete_blockers(project_id, session=session),
                project_delete_conflict_message=lambda blockers: _project_delete_conflict_message(blockers),
                saved_runtime_config_or_default=lambda model_profile_id='': _saved_runtime_config_or_default(model_profile_id),
                persist_project_automation=lambda session, project, automation: _persist_project_automation(session, project, automation),
                require_reason=lambda reason, *, action: _require_reason(reason, action=action),
            ),
            governance=api_route_registry.GovernanceDeps(
                resolve_project_governance=lambda project, *, overrides=None, base_config=None: _resolve_project_governance(project, overrides=overrides, base_config=base_config),
                governance_request_payload=lambda req: _governance_request_payload(req),
                latest_related_decision_event=lambda session, **kwargs: _latest_related_decision_event(session, **kwargs),
                log_decision_event=lambda session, **kwargs: _log_decision_event(session, **kwargs),
                decision_refs_for_chapter_review=lambda session, *, project_id, chapter_number, review_id: _decision_refs_for_chapter_review(session, project_id=project_id, chapter_number=chapter_number, review_id=review_id),
                validate_constraint_payload=lambda **kwargs: _validate_constraint_payload(**kwargs),
                serialize_band_checkpoint=lambda row, *, session=None: _serialize_band_checkpoint(row, session=session),
                serialize_constraint=lambda row: _serialize_constraint(row),
                list_decision_event_rows=lambda session, **kwargs: _list_decision_event_rows(session, **kwargs),
                serialize_decision_event=lambda row: _serialize_decision_event(row),
                build_causal_replay=lambda session, **kwargs: _build_causal_replay(session, **kwargs),
                build_governance_insights=lambda session, *, project_id: _build_governance_insights(session, project_id=project_id),
                latest_band_checkpoint_row=lambda session, *, project_id, band_id='': _latest_band_checkpoint_row(session, project_id=project_id, band_id=band_id),
                persist_project_governance=lambda session, project, governance: _persist_project_governance(session, project, governance),
            ),
            observability=api_route_registry.ObservabilityDeps(
                get_chapter_observability_ledger=get_chapter_observability_ledger,
                get_prompt_trace_detail=get_prompt_trace_detail,
                read_artifact_preview=read_artifact_preview,
                get_task_performance_report=get_task_performance_report,
                get_project_performance_report=get_project_performance_report,
                get_chapter_performance_report=get_chapter_performance_report,
                get_slow_performance_spans=get_slow_performance_spans,
                get_llm_performance_report=get_llm_performance_report,
                get_db_performance_report=get_db_performance_report,
            ),
            publisher=api_route_registry.PublisherDeps(
                get_publisher_manager=lambda: api_state._publisher_manager,
                render_publishers_page=render_publishers_page,
            ),
        ),
    )
)


__all__ = [name for name in globals() if not name.startswith("__")]
