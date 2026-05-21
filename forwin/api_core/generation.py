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
from forwin.generation.auto_continue import GenerationAutoContinueController
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

def _active_generation_task_ids(project_id: str = "", *, session=None) -> list[str]:
    normalized_project_id = str(project_id or "").strip()
    if api_state._SessionFactory is None:
        active_ids: list[str] = []
        with api_state._tasks_lock:
            for task_id, task in api_state._tasks.items():
                if task.get("deleted"):
                    continue
                if str(task.get("task_kind", "generation")) != "generation":
                    continue
                if normalized_project_id and str(task.get("project_id", "")).strip() != normalized_project_id:
                    continue
                if _task_is_terminal(str(task.get("status", "")).strip()):
                    continue
                active_ids.append(task_id)
        return active_ids

    def _cached_task_is_active(task: dict[str, Any] | None) -> bool | None:
        if task is None:
            return None
        if task.get("deleted"):
            return False
        if str(task.get("task_kind", "generation")) != "generation":
            return False
        if normalized_project_id and str(task.get("project_id", "") or "").strip() != normalized_project_id:
            return False
        return not _task_is_terminal(str(task.get("status", "")).strip())

    def _query_active_ids(active_session) -> list[str]:
        criteria = [
            GenerationTask.deleted_at.is_(None),
            GenerationTask.task_kind == "generation",
            GenerationTask.status.notin_(tuple(api_state._GENERATION_TERMINAL_STATUSES)),
        ]
        if normalized_project_id:
            criteria.append(GenerationTask.project_id == normalized_project_id)
        rows = active_session.execute(
            select(GenerationTask.id).where(*criteria).order_by(
                GenerationTask.updated_at.desc(),
                GenerationTask.id.desc(),
            )
        ).scalars().all()
        active_ids: list[str] = []
        seen: set[str] = set()
        for task_id in rows:
            normalized_task_id = str(task_id)
            cached_active = _cached_task_is_active(_cached_generation_task(normalized_task_id))
            if cached_active is False:
                continue
            active_ids.append(normalized_task_id)
            seen.add(normalized_task_id)
        with api_state._tasks_lock:
            cached_items = [(task_id, dict(task)) for task_id, task in api_state._tasks.items()]
        for task_id, task in cached_items:
            if task_id in seen:
                continue
            if _cached_task_is_active(task):
                active_ids.append(task_id)
        return active_ids

    if session is not None:
        return _query_active_ids(session)
    with _get_session() as managed_session:
        return _active_generation_task_ids(
            normalized_project_id,
            session=managed_session,
        )


def _project_has_active_generation_task(project_id: str, *, session=None) -> bool:
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id:
        return False
    return bool(_active_generation_task_ids(normalized_project_id, session=session))


def _project_has_active_upload_job(project_id: str, *, session=None) -> bool:
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id or api_state._SessionFactory is None:
        return False
    if session is not None:
        active_job_id = session.execute(
            select(PublisherUploadJob.id).where(
                PublisherUploadJob.deleted_at.is_(None),
                PublisherUploadJob.project_id == normalized_project_id,
                PublisherUploadJob.status.notin_(tuple(api_state._UPLOAD_TERMINAL_STATUSES)),
            ).limit(1)
        ).scalar_one_or_none()
        return active_job_id is not None
    with _get_session() as managed_session:
        return _project_has_active_upload_job(
            normalized_project_id,
            session=managed_session,
        )


def _generation_task_conflict_message(project_id: str) -> str:
    return f"项目 {project_id} 已有运行中的生成任务，请先终止或等待当前任务完成。"


def _project_delete_conflict_message(blockers: list[str]) -> str:
    labels = {
        "generation": "生成任务",
        "upload": "发布任务",
    }
    blocker_text = "、".join(labels.get(item, item) for item in blockers)
    return f"项目存在运行中的{blocker_text}，请先终止后再删除。"


def _project_delete_blockers(project_id: str, *, session) -> list[str]:
    blockers: list[str] = []
    if _project_has_active_generation_task(project_id, session=session):
        blockers.append("generation")
    if _project_has_active_upload_job(project_id, session=session):
        blockers.append("upload")
    return blockers


def _create_task_root_event(
    *,
    project_id: str,
    task_id: str,
    event_type: str,
    summary: str,
    reason: str = "",
    actor_type: str = "api",
) -> str:
    if not str(project_id or "").strip():
        return ""
    with _get_session() as session:
        row = _log_decision_event(
            session,
            project_id=project_id,
            task_id=task_id,
            scope="task",
            event_family="audit_action",
            event_type=event_type,
            actor_type=actor_type,
            summary=summary,
            reason=reason,
            related_object_type="generation_task",
            related_object_id=task_id,
        )
        session.commit()
        return row.id


def _make_generation_completion_handler(
    *,
    task_id: str,
    root_event_id: str = "",
    prior_handler=None,
    runtime_config: Config | None = None,
    auto_continue: bool = False,
    run_until_chapter: int | None = None,
    max_chapters: int | None = None,
    create_continue_generation_task=None,
):
    def _handler(result) -> None:
        if prior_handler is not None:
            prior_handler(result)
        project_id = str(getattr(result, "project_id", "") or "").strip()
        if not project_id:
            return
        event_type = ""
        summary = ""
        if getattr(result, "paused", False):
            event_type = DecisionEventType.PAUSE_REACHED
            summary = "生成任务已在安全检查点暂停。"
        elif getattr(result, "cancelled", False):
            event_type = DecisionEventType.TERMINATE_REACHED
            summary = "生成任务已在安全检查点终止。"
        elif getattr(result, "failed_chapters", None):
            event_type = DecisionEventType.RUN_COMPLETED_WITH_FAILURES
            summary = "生成任务已结束，存在失败章节。"
        else:
            event_type = DecisionEventType.RUN_COMPLETED
            summary = "生成任务已完成。"
        with _get_session() as session:
            _log_decision_event(
                session,
                project_id=project_id,
                task_id=task_id,
                scope="task",
                event_family="business_event",
                event_type=event_type,
                actor_type="system",
                summary=summary,
                payload={
                    "completed_chapters": list(getattr(result, "completed_chapters", []) or []),
                    "failed_chapters": list(getattr(result, "failed_chapters", []) or []),
                    "paused_chapters": list(getattr(result, "paused_chapters", []) or []),
                },
                related_object_type="generation_task",
                related_object_id=task_id,
                causal_root_id=root_event_id,
            )
            session.commit()
        if auto_continue and create_continue_generation_task is not None:
            controller = GenerationAutoContinueController(
                session_factory=_get_session,
                create_continue_generation_task=create_continue_generation_task,
            )
            controller.after_task_completion(
                result,
                parent_task_id=task_id,
                run_until_chapter=run_until_chapter,
                max_chapters=max_chapters,
                auto_continue=auto_continue,
                runtime_config=runtime_config,
            )

    return _handler


def _create_generation_task(
    *,
    premise: str,
    genre: str,
    num_chapters: int,
    runtime_config: Config,
    project_id: str = "",
    title: str = "",
    subtitle: str = "",
) -> str:
    normalized_project_id = str(project_id or "").strip()
    if normalized_project_id and _project_has_active_generation_task(normalized_project_id):
        raise ActiveGenerationTaskError(
            _generation_task_conflict_message(normalized_project_id)
        )
    task_id = uuid.uuid4().hex[:12]
    task_record = _create_task_record(
        message=f"开始生成 {num_chapters} 章。",
        title=title or (premise.strip()[:36] if premise.strip() else "未命名生成任务"),
        subtitle=subtitle or f"{genre} · {num_chapters} 章",
        requested_chapters=num_chapters,
    )
    if normalized_project_id:
        task_record["project_id"] = normalized_project_id
    _persist_generation_task(task_id, task_record)
    root_event_id = ""
    if normalized_project_id:
        root_event_id = _create_task_root_event(
            project_id=normalized_project_id,
            task_id=task_id,
            event_type=DecisionEventType.GENERATION_REQUESTED,
            summary="项目生成任务已创建。",
        )
    runtime_config = copy_config(
        runtime_config,
        governance_task_id=task_id,
        governance_causal_root_id=root_event_id,
    )
    t = threading.Thread(
        target=_run_generation_with_config,
        args=(
            task_id,
            premise,
            genre,
            num_chapters,
            runtime_config,
            _update_task,
            logger,
            normalized_project_id or None,
            lambda: _task_should_abort(task_id),
            lambda: _task_should_pause(task_id),
            _make_generation_completion_handler(
                task_id=task_id,
                root_event_id=root_event_id,
                prior_handler=_maybe_enqueue_auto_publish_jobs,
                auto_continue=False,
            ),
        ),
        daemon=True,
    )
    t.start()
    return task_id


def _create_continue_generation_task(
    *,
    project_id: str,
    runtime_config: Config,
    requested_chapters: int,
    max_chapters: int | None = None,
    auto_continue: bool = True,
    run_until_chapter: int | None = None,
    title: str = "",
    subtitle: str = "",
    message: str = "",
) -> str:
    normalized_project_id = str(project_id or "").strip()
    if normalized_project_id and _project_has_active_generation_task(normalized_project_id):
        raise ActiveGenerationTaskError(
            _generation_task_conflict_message(normalized_project_id)
        )
    task_id = uuid.uuid4().hex[:12]
    task_record = _create_task_record(
        message=message or "准备继续后续章节。",
        title=title or f"继续生成 {normalized_project_id}",
        subtitle=subtitle or f"项目 {normalized_project_id}",
        requested_chapters=requested_chapters,
    )
    task_record["project_id"] = normalized_project_id
    _persist_generation_task(task_id, task_record)
    root_event_id = _create_task_root_event(
        project_id=normalized_project_id,
        task_id=task_id,
        event_type=DecisionEventType.CONTINUE_REQUESTED,
        summary="继续生成任务已创建。",
    )
    runtime_config = copy_config(
        runtime_config,
        governance_task_id=task_id,
        governance_causal_root_id=root_event_id,
    )
    thread = threading.Thread(
        target=_run_continue_project_with_config,
        args=(
            task_id,
            normalized_project_id,
            runtime_config,
            _update_task,
            logger,
            lambda: _task_should_abort(task_id),
            lambda: _task_should_pause(task_id),
            max_chapters,
            _make_generation_completion_handler(
                task_id=task_id,
                root_event_id=root_event_id,
                prior_handler=_maybe_enqueue_auto_publish_jobs,
                runtime_config=runtime_config,
                auto_continue=auto_continue,
                run_until_chapter=run_until_chapter,
                max_chapters=max_chapters,
                create_continue_generation_task=_create_continue_generation_task,
            ),
        ),
        daemon=True,
    )
    thread.start()
    return task_id


def _maybe_enqueue_auto_publish_jobs(result) -> None:
    project_id = str(getattr(result, "project_id", "") or "").strip()
    chapter_numbers = sorted({
        int(item)
        for item in getattr(result, "completed_chapters", []) or []
        if str(item).isdigit() or isinstance(item, int)
    })
    if not project_id or not chapter_numbers or api_state._publisher_manager is None:
        return

    session = _get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            return
        automation = normalize_project_automation(project.automation_json)
        if not automation.auto_publish:
            return
        publish = automation.publish
        platform = str(publish.platform or "").strip()
        book_name = str(publish.book_name or "").strip() or project.title
        if not platform or not book_name:
            return
        plans = session.execute(
            select(ChapterPlan)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number.in_(chapter_numbers),
            )
            .order_by(ChapterPlan.chapter_number.asc())
        ).scalars().all()
        if not plans:
            return
        plan_by_number = {
            int(plan.chapter_number): plan
            for plan in plans
        }
        draft_map = load_latest_drafts_by_plan_id(session, [plan.id for plan in plans])
        jobs = []
        for chapter_number in chapter_numbers:
            plan = plan_by_number.get(chapter_number)
            if plan is None:
                continue
            draft = draft_map.get(plan.id)
            if draft is None:
                continue
            jobs.append(
                {
                    "chapter_title": plan.title,
                    "body": draft.body_text,
                }
            )
        if not jobs:
            return
        api_state._publisher_manager.create_upload_jobs_batch(
            project_id=project_id,
            platform=platform,
            book_name=book_name,
            jobs=jobs,
            upload_url=publish.upload_url or None,
            publish=True,
            create_if_missing=bool(publish.create_if_missing),
            book_meta=publish.book_meta.model_dump(mode="json"),
        )
    except Exception:  # noqa: BLE001
        logger.exception("Auto publish enqueue failed for project %s", project_id)
    finally:
        session.close()




__all__ = [name for name in globals() if not name.startswith("__")]
