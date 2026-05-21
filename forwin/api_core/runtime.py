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

class ActiveGenerationTaskError(RuntimeError):
    pass


def _get_session():
    return api_state._SessionFactory()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _display_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(api_state._DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def _json_load_list(raw: str | None) -> list[Any]:
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _json_load_object(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_dump(value: Any, fallback: Any) -> str:
    normalized = value if isinstance(value, type(fallback)) else fallback
    return json.dumps(normalized, ensure_ascii=False)


def _resolve_runtime_profile(requested_profile_id: str = "") -> dict[str, str]:
    stored = api_state._runtime_settings.get() if api_state._runtime_settings else {}
    profiles = [
        item for item in stored.get("profiles", [])
        if isinstance(item, dict)
    ]
    target_id = str(requested_profile_id or "").strip() or str(stored.get("default_profile_id", "")).strip()
    selected = next(
        (
            item for item in profiles
            if str(item.get("id", "")).strip() == target_id
        ),
        None,
    )
    if selected is None and profiles:
        selected = profiles[0]
    if selected is None:
        selected = {
            "id": "",
            "name": "",
            "api_key": str(stored.get("api_key", "")).strip(),
            "base_url": str(stored.get("base_url", "")).strip(),
            "model": str(stored.get("model", "")).strip(),
        }
    return {
        "id": str(selected.get("id", "")).strip(),
        "name": str(selected.get("name", "")).strip(),
        "api_key": str(selected.get("api_key", "")).strip(),
        "base_url": str(selected.get("base_url", "")).strip(),
        "model": str(selected.get("model", "")).strip(),
    }


def _saved_runtime_config_or_default(model_profile_id: str = "") -> Config:
    if not api_state._config:
        return Config(minimax_api_key="")
    if model_profile_id:
        return build_runtime_config(
            GenerateRequest(
                premise="Genesis model selection",
                model_profile_id=model_profile_id,
            ),
            base_config=api_state._config,
            runtime_settings=api_state._runtime_settings,
        )
    return build_saved_runtime_config(
        base_config=api_state._config,
        runtime_settings=api_state._runtime_settings,
    )


def _build_genesis_service(
    runtime_config: Config | None = None,
    *,
    model_profile_id: str = "",
) -> BookGenesisService:
    resolved_profile = _resolve_runtime_profile(model_profile_id)
    resolved = runtime_config or _saved_runtime_config_or_default(model_profile_id)
    shared_container = runtime_config is None and not model_profile_id and api_state._runtime_container is not None
    container = api_state._runtime_container if shared_container else RuntimeContainer.from_config(resolved)
    service = container.services().book_genesis if shared_container else container.build_book_genesis_service()
    setattr(service, "_forwin_runtime_owned", True)
    setattr(service, "_forwin_runtime_container", container if shared_container else None)
    setattr(service, "_forwin_runtime_shared", bool(shared_container))
    setattr(service.llm_client, "profile_id", resolved_profile.get("id", ""))
    setattr(service.llm_client, "profile_name", resolved_profile.get("name", ""))
    return service


def _close_genesis_service(service: BookGenesisService | None) -> None:
    if getattr(service, "_forwin_runtime_shared", False):
        return
    client = getattr(service, "llm_client", None)
    close = getattr(client, "client", None)
    if close is not None:
        try:
            close.close()
        except Exception:  # noqa: BLE001
            logger.debug("BookGenesisService client close failed", exc_info=True)
    container = getattr(service, "_forwin_runtime_container", None)
    if container is not None and container is not api_state._runtime_container:
        try:
            container.services().engine.dispose()
        except Exception:  # noqa: BLE001
            logger.debug("BookGenesisService runtime engine dispose failed", exc_info=True)


def _active_genesis_revision(session: Session, project: Project) -> BookGenesisRevision | None:
    revision_id = str(getattr(project, "active_genesis_revision_id", "") or "").strip()
    if not revision_id:
        return None
    return session.get(BookGenesisRevision, revision_id)


def _require_genesis_project(project: Project) -> None:
    creation_status = str(getattr(project, "creation_status", "") or "").strip()
    if creation_status and creation_status not in {"creating", "genesis_ready", "writing"}:
        raise HTTPException(400, f"项目生命周期状态无效：{creation_status}")


def _genesis_patch_payload(req: BookGenesisPatchRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in (
        "book_brief",
        "world",
        "book_arc_blueprint",
        "subworld_policy",
        "execution_bootstrap",
        "stage_states",
    ):
        value = getattr(req, key)
        if value is None:
            continue
        payload[key] = value
    return payload


def _coerce_int_list(value: Any) -> list[int]:
    numbers: list[int] = []
    for item in value if isinstance(value, list) else []:
        try:
            numbers.append(int(item))
        except (TypeError, ValueError):
            continue
    return numbers




__all__ = [name for name in globals() if not name.startswith("__")]
