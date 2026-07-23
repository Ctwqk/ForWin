from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from forwin.application.projects import (
    ProjectApplicationService,
)
from forwin.application.project_control import ProjectControlApplicationService
from forwin.application.tasks import TaskApplicationService
from forwin.application.publisher import PublisherApplicationService
from forwin.http.adapters import (
    api_book_state_routes,
    api_project_control_routes,
    api_llm_kb_routes,
    api_map_routes,
    api_maintenance_routes,
    api_obsidian_routes,
    api_proposal_routes,
    api_projection_routes,
    api_project_routes,
    api_publisher_routes,
    api_system_routes,
    api_task_routes,
    api_world_model_routes,
)
from forwin.api_schema import (
    ActiveGenerationTaskCheckResponse,
    ArtifactReadResponse,
    BandCheckpointDetail,
    BandExperienceOverrideResponse,
    BookGenesisDetail,
    BookGenesisNameGenerateResponse,
    BookStatePathResponse,
    BookStateRuntimeResponse,
    BulkDeleteResponse,
    CausalReplayResponse,
    CandidateDraftDetail,
    ChapterLedgerResponse,
    ChapterDetail,
    ChapterInfo,
    ChapterListResponse,
    ChapterReviewApproveResponse,
    ChapterReviewDetail,
    CodexBridgeStatusResponse,
    DecisionEventsResponse,
    ExtensionBrowserSessionResponse,
    ExtensionClaimCommentSyncJobResponse,
    ExtensionClaimUploadJobResponse,
    ExtensionCommentsBatchResponse,
    ExtensionHeartbeatResponse,
    ExtensionLoginQrNotifyResponse,
    ExtensionSessionSyncResponse,
    PublisherBrowserSessionSummaryResponse,
    PublisherChapterBindingResponse,
    PublisherCoverAssetResponse,
    AuditInsightsResponse,
    GateLedgerReportResponse,
    CostLedgerReportResponse,
    RuleProvenanceReportResponse,
    MapEnsureResponse,
    MapPathResponse,
    MapRuntimeResponse,
    NarrativeConstraintInfo,
    NarrativeConstraintsResponse,
    PerformanceReportResponse,
    PerformanceSpanInfo,
    PostCanonMaintenanceStatusResponse,
    ProjectionRefreshResponse,
    ProjectionStatusResponse,
    ProjectAutomationUpdateResponse,
    ProjectCreateResponse,
    ProjectDeleteResponse,
    ProjectDetail,
    RuntimeCatalogResponse,
    RuntimePolicyResponse,
    ProjectSummary,
    PromptTraceDetailResponse,
    PublisherCommentSyncJobResponse,
    PublisherLoginQrOneShotResponse,
    PublisherPlatformInfo,
    PublisherPreflightResponse,
    PublisherUploadJobResponse,
    PublisherUploadResumeResponse,
    PublisherWorkBindingResponse,
    UploadAttemptReceiptResponse,
    UploadAttemptPauseResponse,
    UploadAttemptReconcileResponse,
    UploadAttemptResultResponse,
    UploadAttemptStateResponse,
    StartWritingResponse,
    TaskContractResponse,
    TaskMutationResponse,
    TaskResponse,
    TaskCenterItemResponse,
    TaskSummaryResponse,
    TaskTimelineResponse,
    TropeRegistrySummaryResponse,
    TropeTemplateInfo,
    TropeTemplateValidationResponse,
    WorldEditProposalInfo,
    WorldModelConflictInfo,
    WorldModelExportResponse,
    WorldModelPageInfo,
    WorldModelSnapshotInfo,
)


@dataclass(frozen=True)
class CoreDeps:
    get_config: Callable[[], Any]
    get_session: Callable[[], Any]
    render_home_page: Callable[..., str]
    get_memory_index: Callable[[], Any] = lambda: None
    provide_memory_index: Callable[[], Any] = lambda: None


@dataclass(frozen=True)
class TaskDeps:
    service: TaskApplicationService
    get_task_timeline: Callable[..., Any]


@dataclass(frozen=True)
class ProjectDeps:
    service: ProjectApplicationService


@dataclass(frozen=True)
class ProjectControlDeps:
    service: ProjectControlApplicationService


@dataclass(frozen=True)
class ObservabilityDeps:
    get_chapter_observability_ledger: Callable[..., Any]
    get_prompt_trace_detail: Callable[..., Any]
    read_artifact_preview: Callable[..., Any]
    get_task_performance_report: Callable[..., Any]
    get_project_performance_report: Callable[..., Any]
    get_chapter_performance_report: Callable[..., Any]
    get_slow_performance_spans: Callable[..., Any]
    get_llm_performance_report: Callable[..., Any]
    get_db_performance_report: Callable[..., Any]


@dataclass(frozen=True)
class PublisherDeps:
    get_publisher_manager: Callable[[], Any]
    render_publishers_page: Callable[..., str]


@dataclass(frozen=True)
class ApiRouteDeps:
    core: CoreDeps
    task: TaskDeps
    project: ProjectDeps
    project_control: ProjectControlDeps
    observability: ObservabilityDeps
    publisher: PublisherDeps


def register_api_routes(
    app: FastAPI,
    *,
    deps: ApiRouteDeps,
) -> dict[str, Callable[..., Any]]:
    get_config = deps.core.get_config
    get_publisher_manager = deps.publisher.get_publisher_manager
    get_session = deps.core.get_session
    render_home_page = deps.core.render_home_page
    render_publishers_page = deps.publisher.render_publishers_page
    get_task_timeline = deps.task.get_task_timeline
    get_chapter_observability_ledger = (
        deps.observability.get_chapter_observability_ledger
    )
    get_prompt_trace_detail = deps.observability.get_prompt_trace_detail
    read_artifact_preview = deps.observability.read_artifact_preview
    get_task_performance_report = deps.observability.get_task_performance_report
    get_project_performance_report = deps.observability.get_project_performance_report
    get_chapter_performance_report = deps.observability.get_chapter_performance_report
    get_slow_performance_spans = deps.observability.get_slow_performance_spans
    get_llm_performance_report = deps.observability.get_llm_performance_report
    get_db_performance_report = deps.observability.get_db_performance_report
    get_memory_index = deps.core.get_memory_index

    system_handlers = api_system_routes.build_handlers(
        get_config=get_config,
        get_publisher_manager=get_publisher_manager,
        get_session=get_session,
        render_home_page=render_home_page,
        render_publishers_page=render_publishers_page,
        get_memory_index=get_memory_index,
    )
    task_handlers = api_task_routes.build_handlers(
        service=deps.task.service,
    )
    publisher_handlers = api_publisher_routes.build_handlers(
        service=PublisherApplicationService(
            get_publisher_manager=get_publisher_manager,
            extension_root=Path.cwd() / "browser_extension" / "forwin-publisher",
        ),
        get_config=get_config,
    )
    project_handlers = api_project_routes.build_handlers(service=deps.project.service)
    project_control_handlers = api_project_control_routes.build_handlers(
        service=deps.project_control.service,
    )
    world_model_handlers = api_world_model_routes.build_handlers(
        get_session=get_session,
        get_config=get_config,
    )
    book_state_handlers = api_book_state_routes.build_handlers(
        get_session=get_session,
    )
    obsidian_handlers = api_obsidian_routes.build_handlers(
        get_session=get_session,
        get_config=get_config,
    )
    llm_kb_handlers = api_llm_kb_routes.build_handlers(
        get_session=get_session,
        get_config=get_config,
    )
    projection_handlers = api_projection_routes.build_handlers(
        get_session=get_session,
        get_config=get_config,
        memory_index_provider=deps.core.provide_memory_index,
    )
    maintenance_handlers = api_maintenance_routes.build_handlers(
        get_session=get_session,
    )
    proposal_handlers = api_proposal_routes.build_handlers(
        get_session=get_session,
    )
    map_handlers = api_map_routes.build_handlers(
        get_session=get_session,
    )

    handlers = {
        **system_handlers,
        **task_handlers,
        **publisher_handlers,
        **project_handlers,
        **project_control_handlers,
        **world_model_handlers,
        **book_state_handlers,
        **obsidian_handlers,
        **llm_kb_handlers,
        **projection_handlers,
        **maintenance_handlers,
        **proposal_handlers,
        **map_handlers,
    }

    route_definitions = [
        ("/health", ["GET"], handlers["health"], {}),
        ("/", ["GET"], handlers["home_page"], {"response_class": HTMLResponse}),
        (
            "/publishers",
            ["GET"],
            handlers["publishers_page"],
            {"response_class": HTMLResponse},
        ),
        (
            "/world-studio",
            ["GET"],
            handlers["world_studio_page"],
            {"response_class": HTMLResponse},
        ),
        (
            "/world-studio/assets/{asset_path:path}",
            ["GET"],
            handlers["world_studio_asset"],
            {},
        ),
        (
            "/api/settings/llm",
            ["GET"],
            handlers["get_runtime_catalog"],
            {"response_model": RuntimeCatalogResponse},
        ),
        (
            "/api/projects/{project_id}/world-studio/search",
            ["GET"],
            handlers["search_project_world_studio"],
            {},
        ),
        (
            "/api/settings/codex/health",
            ["GET"],
            handlers["get_codex_bridge_status"],
            {"response_model": CodexBridgeStatusResponse},
        ),
        (
            "/api/tasks/active-generation-check",
            ["GET"],
            handlers["active_generation_task_check"],
            {"response_model": ActiveGenerationTaskCheckResponse},
        ),
        (
            "/api/tasks/{task_id}",
            ["GET"],
            handlers["get_task"],
            {"response_model": TaskResponse},
        ),
        (
            "/api/tasks/{task_id}/timeline",
            ["GET"],
            get_task_timeline,
            {"response_model": TaskTimelineResponse},
        ),
        (
            "/api/tasks",
            ["GET"],
            handlers["list_tasks"],
            {"response_model": list[TaskSummaryResponse]},
        ),
        (
            "/api/task-center/items",
            ["GET"],
            handlers["list_task_center_items"],
            {"response_model": list[TaskCenterItemResponse]},
        ),
        (
            "/api/task-center/items/{task_kind}/{task_id}",
            ["GET"],
            handlers["get_task_center_item"],
            {"response_model": TaskCenterItemResponse},
        ),
        (
            "/api/tasks/{task_id}/terminate",
            ["POST"],
            handlers["terminate_task"],
            {"response_model": TaskMutationResponse},
        ),
        (
            "/api/tasks/{task_id}/pause",
            ["POST"],
            handlers["pause_task"],
            {"response_model": TaskMutationResponse},
        ),
        (
            "/api/tasks/{task_id}",
            ["DELETE"],
            handlers["delete_task"],
            {"response_model": TaskMutationResponse},
        ),
        (
            "/api/tasks/bulk-delete",
            ["POST"],
            handlers["bulk_delete_tasks"],
            {"response_model": BulkDeleteResponse},
        ),
        (
            "/api/publishers/extension-package",
            ["GET"],
            handlers["download_publisher_extension_package"],
            {},
        ),
        (
            "/api/publishers/extension-package/firefox",
            ["GET"],
            handlers["download_publisher_firefox_extension_package"],
            {},
        ),
        (
            "/api/publishers/platforms",
            ["GET"],
            handlers["list_publisher_platforms"],
            {"response_model": list[PublisherPlatformInfo]},
        ),
        (
            "/api/publishers/upload-jobs",
            ["POST"],
            handlers["create_publisher_upload_job"],
            {"response_model": PublisherUploadJobResponse},
        ),
        (
            "/api/publishers/upload-jobs/{job_id}",
            ["GET"],
            handlers["get_publisher_upload_job"],
            {"response_model": PublisherUploadJobResponse},
        ),
        (
            "/api/publishers/upload-jobs",
            ["GET"],
            handlers["list_publisher_upload_jobs"],
            {"response_model": list[PublisherUploadJobResponse]},
        ),
        (
            "/api/publishers/work-bindings",
            ["GET"],
            handlers["list_publisher_work_bindings"],
            {"response_model": list[PublisherWorkBindingResponse]},
        ),
        (
            "/api/publishers/chapter-bindings",
            ["GET"],
            handlers["list_publisher_chapter_bindings"],
            {"response_model": list[PublisherChapterBindingResponse]},
        ),
        (
            "/api/publishers/covers",
            ["GET"],
            handlers["list_publisher_cover_assets"],
            {"response_model": list[PublisherCoverAssetResponse]},
        ),
        (
            "/api/publishers/covers/select",
            ["POST"],
            handlers["select_publisher_cover_asset"],
            {"response_model": PublisherCoverAssetResponse},
        ),
        (
            "/api/publishers/covers/approve",
            ["POST"],
            handlers["approve_publisher_cover_asset"],
            {"response_model": PublisherCoverAssetResponse},
        ),
        (
            "/api/publishers/covers/reject",
            ["POST"],
            handlers["reject_publisher_cover_asset"],
            {"response_model": PublisherCoverAssetResponse},
        ),
        (
            "/api/publishers/covers/upload",
            ["POST"],
            handlers["enqueue_publisher_cover_upload"],
            {"response_model": PublisherUploadJobResponse},
        ),
        (
            "/api/publishers/audit-sync",
            ["POST"],
            handlers["enqueue_publisher_audit_sync"],
            {"response_model": PublisherUploadJobResponse},
        ),
        (
            "/api/publishers/preflight",
            ["POST"],
            handlers["publisher_preflight"],
            {"response_model": PublisherPreflightResponse},
        ),
        (
            "/api/publishers/login-qr-one-shot",
            ["POST"],
            handlers["start_publisher_login_qr_one_shot"],
            {"response_model": PublisherLoginQrOneShotResponse},
        ),
        (
            "/api/publishers/upload-jobs/{job_id}/terminate",
            ["POST"],
            handlers["terminate_publisher_upload_job"],
            {"response_model": TaskMutationResponse},
        ),
        (
            "/api/publishers/upload-jobs/{job_id}/resume",
            ["POST"],
            handlers["resume_publisher_upload_job"],
            {"response_model": PublisherUploadResumeResponse},
        ),
        (
            "/api/publishers/upload-jobs/{job_id}",
            ["DELETE"],
            handlers["delete_publisher_upload_job"],
            {"response_model": TaskMutationResponse},
        ),
        (
            "/api/publishers/extension/heartbeat",
            ["POST"],
            handlers["publisher_extension_heartbeat"],
            {"response_model": ExtensionHeartbeatResponse},
        ),
        (
            "/api/publishers/extension/login-qr",
            ["POST"],
            handlers["publisher_extension_login_qr_notify"],
            {"response_model": ExtensionLoginQrNotifyResponse},
        ),
        (
            "/api/publishers/extension/heartbeat-status",
            ["GET"],
            handlers["publisher_extension_heartbeat_status"],
            {},
        ),
        (
            "/api/publishers/extension/session-sync",
            ["POST"],
            handlers["publisher_extension_session_sync"],
            {"response_model": ExtensionSessionSyncResponse},
        ),
        (
            "/api/publishers/browser-sessions/{platform}",
            ["GET"],
            handlers["get_publisher_browser_session_summary"],
            {"response_model": PublisherBrowserSessionSummaryResponse | None},
        ),
        (
            "/api/publishers/extension/browser-sessions/{platform}",
            ["GET"],
            handlers["publisher_extension_get_browser_session"],
            {"response_model": ExtensionBrowserSessionResponse | None},
        ),
        (
            "/api/publishers/extension/upload-jobs/claim",
            ["POST"],
            handlers["claim_publisher_upload_job"],
            {"response_model": ExtensionClaimUploadJobResponse},
        ),
        (
            "/api/publishers/extension/upload-jobs/{job_id}/attempts/{attempt_id}/heartbeat",
            ["POST"],
            handlers["heartbeat_publisher_upload_attempt"],
            {"response_model": UploadAttemptStateResponse},
        ),
        (
            "/api/publishers/extension/upload-jobs/{job_id}/attempts/{attempt_id}/phase",
            ["POST"],
            handlers["transition_publisher_upload_attempt"],
            {"response_model": UploadAttemptStateResponse},
        ),
        (
            "/api/publishers/extension/upload-jobs/{job_id}/attempts/{attempt_id}/result",
            ["POST"],
            handlers["finish_publisher_upload_attempt"],
            {"response_model": UploadAttemptResultResponse},
        ),
        (
            "/api/publishers/extension/upload-jobs/{job_id}/attempts/{attempt_id}/pause",
            ["POST"],
            handlers["pause_publisher_upload_attempt"],
            {"response_model": UploadAttemptPauseResponse},
        ),
        (
            "/api/publishers/extension/upload-jobs/{job_id}/attempts/{attempt_id}/receipt",
            ["POST"],
            handlers["record_publisher_upload_receipt"],
            {"response_model": UploadAttemptReceiptResponse},
        ),
        (
            "/api/publishers/extension/upload-jobs/{job_id}/attempts/{attempt_id}/reconcile",
            ["POST"],
            handlers["reconcile_publisher_upload_attempt"],
            {"response_model": UploadAttemptReconcileResponse},
        ),
        (
            "/api/publishers/extension/comment-sync-jobs/claim",
            ["POST"],
            handlers["claim_publisher_comment_sync_job"],
            {"response_model": ExtensionClaimCommentSyncJobResponse},
        ),
        (
            "/api/publishers/comment-sync-jobs",
            ["POST"],
            handlers["create_publisher_comment_sync_job"],
            {"response_model": PublisherCommentSyncJobResponse},
        ),
        (
            "/api/publishers/comment-sync-jobs/{job_id}/result",
            ["POST"],
            handlers["update_publisher_comment_sync_job_result"],
            {"response_model": PublisherCommentSyncJobResponse},
        ),
        (
            "/api/publishers/extension/comments/batch",
            ["POST"],
            handlers["ingest_publisher_comments_batch"],
            {"response_model": ExtensionCommentsBatchResponse},
        ),
        (
            "/api/projects",
            ["GET"],
            handlers["list_projects"],
            {"response_model": list[ProjectSummary]},
        ),
        (
            "/api/projects",
            ["POST"],
            handlers["create_project"],
            {"response_model": ProjectCreateResponse},
        ),
        (
            "/api/projects/{project_id}",
            ["DELETE"],
            handlers["delete_project"],
            {"response_model": ProjectDeleteResponse},
        ),
        (
            "/api/projects/bulk-delete",
            ["POST"],
            handlers["bulk_delete_projects"],
            {"response_model": BulkDeleteResponse},
        ),
        (
            "/api/projects/{project_id}",
            ["GET"],
            handlers["get_project"],
            {"response_model": ProjectDetail},
        ),
        (
            "/api/projects/{project_id}/policy",
            ["GET"],
            handlers["get_project_policy"],
            {"response_model": RuntimePolicyResponse},
        ),
        (
            "/api/projects/{project_id}/policy",
            ["PUT"],
            handlers["update_project_policy"],
            {"response_model": RuntimePolicyResponse},
        ),
        (
            "/api/projects/{project_id}/genesis",
            ["GET"],
            handlers["get_project_genesis"],
            {"response_model": BookGenesisDetail},
        ),
        (
            "/api/projects/{project_id}/genesis",
            ["PATCH"],
            handlers["patch_project_genesis"],
            {"response_model": BookGenesisDetail},
        ),
        (
            "/api/projects/{project_id}/genesis/stages/{stage_key}/generate",
            ["POST"],
            handlers["generate_project_genesis_stage"],
            {"response_model": BookGenesisDetail},
        ),
        (
            "/api/projects/{project_id}/genesis/stages/{stage_key}/lock",
            ["POST"],
            handlers["lock_project_genesis_stage"],
            {"response_model": BookGenesisDetail},
        ),
        (
            "/api/projects/{project_id}/genesis/stages/{stage_key}/rerun",
            ["POST"],
            handlers["rerun_project_genesis_stage"],
            {"response_model": BookGenesisDetail},
        ),
        (
            "/api/projects/{project_id}/genesis/stages/{stage_key}/refine",
            ["POST"],
            handlers["refine_project_genesis_stage"],
            {"response_model": BookGenesisDetail},
        ),
        (
            "/api/projects/{project_id}/genesis/generate-name",
            ["POST"],
            handlers["generate_project_genesis_name"],
            {"response_model": BookGenesisNameGenerateResponse},
        ),
        (
            "/api/projects/{project_id}/world-model/snapshots",
            ["GET"],
            handlers["list_project_world_model_snapshots"],
            {"response_model": list[WorldModelSnapshotInfo]},
        ),
        (
            "/api/projects/{project_id}/world-model/snapshots/latest",
            ["GET"],
            handlers["get_latest_project_world_model_snapshot"],
            {"response_model": WorldModelSnapshotInfo},
        ),
        (
            "/api/projects/{project_id}/world-model/pages",
            ["GET"],
            handlers["list_project_world_model_pages"],
            {"response_model": list[WorldModelPageInfo]},
        ),
        (
            "/api/projects/{project_id}/world-model/pages/{page_key}",
            ["GET"],
            handlers["get_project_world_model_page"],
            {"response_model": WorldModelPageInfo},
        ),
        (
            "/api/projects/{project_id}/world-model/conflicts",
            ["GET"],
            handlers["list_project_world_model_conflicts"],
            {"response_model": list[WorldModelConflictInfo]},
        ),
        (
            "/api/projects/{project_id}/proposals",
            ["GET"],
            handlers["list_project_proposals"],
            {"response_model": list[WorldEditProposalInfo]},
        ),
        (
            "/api/projects/{project_id}/proposals",
            ["POST"],
            handlers["create_project_proposal"],
            {"response_model": WorldEditProposalInfo},
        ),
        (
            "/api/projects/{project_id}/proposals/{proposal_id}",
            ["GET"],
            handlers["get_project_proposal"],
            {"response_model": WorldEditProposalInfo},
        ),
        (
            "/api/projects/{project_id}/proposals/{proposal_id}/approve",
            ["POST"],
            handlers["approve_project_proposal"],
            {"response_model": WorldEditProposalInfo},
        ),
        (
            "/api/projects/{project_id}/proposals/{proposal_id}/reject",
            ["POST"],
            handlers["reject_project_proposal"],
            {"response_model": WorldEditProposalInfo},
        ),
        (
            "/api/projects/{project_id}/start-writing",
            ["POST"],
            handlers["start_project_writing"],
            {"response_model": StartWritingResponse},
        ),
        (
            "/api/projects/{project_id}/continue-generation",
            ["POST"],
            handlers["continue_project_generation"],
            {"response_model": TaskResponse},
        ),
        (
            "/api/projects/{project_id}/extend-generation",
            ["POST"],
            handlers["extend_project_generation"],
            {"response_model": ProjectDetail},
        ),
        (
            "/api/projects/{project_id}/automation",
            ["PUT"],
            handlers["update_project_automation"],
            {"response_model": ProjectAutomationUpdateResponse},
        ),
        (
            "/api/projects/{project_id}/manual-checkpoints",
            ["POST"],
            handlers["create_manual_checkpoint"],
            {"response_model": BandCheckpointDetail},
        ),
        (
            "/api/projects/{project_id}/bands/{band_id}/checkpoint",
            ["GET"],
            handlers["get_band_checkpoint"],
            {"response_model": BandCheckpointDetail},
        ),
        (
            "/api/projects/{project_id}/bands/{band_id}/checkpoint/approve",
            ["POST"],
            handlers["approve_band_checkpoint"],
            {"response_model": BandCheckpointDetail},
        ),
        (
            "/api/projects/{project_id}/chapters/{chapter_number}/task-contract",
            ["GET"],
            handlers["get_chapter_task_contract"],
            {"response_model": TaskContractResponse},
        ),
        (
            "/api/projects/{project_id}/chapters/{chapter_number}/task-contract",
            ["PUT"],
            handlers["update_chapter_task_contract"],
            {"response_model": TaskContractResponse},
        ),
        (
            "/api/projects/{project_id}/bands/{band_id}/task-contract",
            ["GET"],
            handlers["get_band_task_contract"],
            {"response_model": TaskContractResponse},
        ),
        (
            "/api/projects/{project_id}/bands/{band_id}/task-contract",
            ["PUT"],
            handlers["update_band_task_contract"],
            {"response_model": TaskContractResponse},
        ),
        (
            "/api/projects/{project_id}/constraints",
            ["GET"],
            handlers["list_project_constraints"],
            {"response_model": NarrativeConstraintsResponse},
        ),
        (
            "/api/projects/{project_id}/constraints",
            ["POST"],
            handlers["create_project_constraint"],
            {"response_model": NarrativeConstraintInfo},
        ),
        (
            "/api/projects/{project_id}/constraints/{constraint_id}",
            ["PATCH"],
            handlers["update_project_constraint"],
            {"response_model": NarrativeConstraintInfo},
        ),
        (
            "/api/projects/{project_id}/decision-events",
            ["GET"],
            handlers["list_project_decision_events"],
            {"response_model": DecisionEventsResponse},
        ),
        (
            "/api/projects/{project_id}/causal-replay",
            ["GET"],
            handlers["get_project_causal_replay"],
            {"response_model": CausalReplayResponse},
        ),
        (
            "/api/projects/{project_id}/audit-insights",
            ["GET"],
            handlers["get_project_audit_insights"],
            {"response_model": AuditInsightsResponse},
        ),
        (
            "/api/gate-ledger",
            ["GET"],
            handlers["get_gate_ledger_report"],
            {"response_model": GateLedgerReportResponse},
        ),
        (
            "/api/cost-report",
            ["GET"],
            handlers["get_cost_report"],
            {"response_model": CostLedgerReportResponse},
        ),
        (
            "/api/rule-provenance",
            ["GET"],
            handlers["get_rule_provenance_report"],
            {"response_model": RuleProvenanceReportResponse},
        ),
        ("/api/personality-skills", ["GET"], handlers["list_personality_skills"], {}),
        (
            "/api/projects/{project_id}/characters",
            ["POST"],
            handlers["create_character"],
            {},
        ),
        (
            "/api/projects/{project_id}/characters/personality/preview",
            ["POST"],
            handlers["preview_character_personality"],
            {},
        ),
        (
            "/api/projects/{project_id}/characters/personality/active-context/preview",
            ["POST"],
            handlers["preview_character_active_personality_context"],
            {},
        ),
        (
            "/api/projects/{project_id}/characters/personality/relationships/enrich",
            ["POST"],
            handlers["enrich_character_relationships"],
            {},
        ),
        (
            "/api/projects/{project_id}/characters/personality/coverage",
            ["GET"],
            handlers["get_character_personality_coverage"],
            {},
        ),
        (
            "/api/projects/{project_id}/characters/personality/metrics",
            ["GET"],
            handlers["get_character_personality_metrics"],
            {},
        ),
        (
            "/api/projects/{project_id}/characters/personality/backfill",
            ["POST"],
            handlers["backfill_character_personalities"],
            {},
        ),
        (
            "/api/projects/{project_id}/characters/personality/assignment-reports/{assignment_id}",
            ["GET"],
            handlers["get_character_assignment_report_by_id"],
            {},
        ),
        (
            "/api/projects/{project_id}/characters/{character_id}/personality/reassign",
            ["POST"],
            handlers["reassign_character_personality"],
            {},
        ),
        (
            "/api/projects/{project_id}/characters/{character_id}/personality/assignment-report",
            ["GET"],
            handlers["get_character_assignment_report"],
            {},
        ),
        (
            "/api/projects/{project_id}/book-state/snapshot",
            ["GET"],
            handlers["get_book_state_snapshot"],
            {},
        ),
        (
            "/api/projects/{project_id}/book-state/nodes",
            ["GET"],
            handlers["list_book_state_nodes"],
            {},
        ),
        (
            "/api/projects/{project_id}/book-state/edges",
            ["GET"],
            handlers["list_book_state_edges"],
            {},
        ),
        (
            "/api/projects/{project_id}/book-state/deltas",
            ["GET"],
            handlers["list_book_state_deltas"],
            {},
        ),
        (
            "/api/projects/{project_id}/book-state/cognition",
            ["GET"],
            handlers["list_book_state_cognition"],
            {},
        ),
        (
            "/api/projects/{project_id}/book-state/reader-promises",
            ["GET"],
            handlers["list_book_state_reader_promises"],
            {},
        ),
        (
            "/api/projects/{project_id}/book-state/characters/personality",
            ["GET"],
            handlers["list_character_personality_loadouts"],
            {},
        ),
        (
            "/api/projects/{project_id}/book-state/characters/{character_id}/personality-loadout",
            ["GET"],
            handlers["get_character_personality_loadout"],
            {},
        ),
        (
            "/api/projects/{project_id}/book-state/characters/{character_id}/personality-loadout",
            ["PUT"],
            handlers["set_character_personality_loadout"],
            {},
        ),
        (
            "/api/projects/{project_id}/book-state/runtime",
            ["GET"],
            handlers["get_book_state_runtime"],
            {"response_model": BookStateRuntimeResponse},
        ),
        (
            "/api/projects/{project_id}/book-state/map/path",
            ["GET"],
            handlers["get_book_state_path"],
            {"response_model": BookStatePathResponse},
        ),
        (
            "/api/projects/{project_id}/map/runtime",
            ["GET"],
            handlers["get_project_map_runtime"],
            {"response_model": MapRuntimeResponse},
        ),
        (
            "/api/projects/{project_id}/map/path",
            ["GET"],
            handlers["get_project_map_path"],
            {"response_model": MapPathResponse},
        ),
        (
            "/api/projects/{project_id}/map/ensure-from-genesis",
            ["POST"],
            handlers["ensure_project_map_from_genesis"],
            {"response_model": MapEnsureResponse},
        ),
        (
            "/api/projects/{project_id}/obsidian/export",
            ["POST"],
            handlers["export_obsidian"],
            {"response_model": WorldModelExportResponse},
        ),
        (
            "/api/projects/{project_id}/llm-kb/rebuild",
            ["POST"],
            handlers["rebuild_llm_kb"],
            {},
        ),
        (
            "/api/projects/{project_id}/llm-kb/files",
            ["GET"],
            handlers["list_llm_kb_files"],
            {},
        ),
        (
            "/api/projects/{project_id}/llm-kb/file/{file_key}",
            ["GET"],
            handlers["get_llm_kb_file"],
            {},
        ),
        (
            "/api/projects/{project_id}/llm-kb/search",
            ["GET"],
            handlers["search_llm_kb"],
            {},
        ),
        (
            "/api/projects/{project_id}/projections/refresh",
            ["POST"],
            handlers["refresh_projection"],
            {"response_model": ProjectionRefreshResponse},
        ),
        (
            "/api/projects/{project_id}/projections/status",
            ["GET"],
            handlers["get_projection_status"],
            {"response_model": ProjectionStatusResponse},
        ),
        (
            "/api/projects/{project_id}/projections/pages",
            ["GET"],
            handlers["list_projection_pages"],
            {},
        ),
        (
            "/api/projects/{project_id}/projections/pages/{page_key}",
            ["GET"],
            handlers["get_projection_page"],
            {},
        ),
        (
            "/api/projects/{project_id}/maintenance/post-canon",
            ["GET"],
            handlers["get_post_canon_maintenance_status"],
            {"response_model": PostCanonMaintenanceStatusResponse},
        ),
        (
            "/api/projects/{project_id}/context-pack/{role}",
            ["GET"],
            handlers["get_context_pack"],
            {},
        ),
        (
            "/api/projects/{project_id}/chapters",
            ["GET"],
            handlers["list_chapters"],
            {"response_model": list[ChapterInfo]},
        ),
        (
            "/api/projects/{project_id}/chapters/page",
            ["GET"],
            handlers["list_chapter_page"],
            {"response_model": ChapterListResponse},
        ),
        (
            "/api/projects/{project_id}/chapters/{chapter_number}",
            ["GET"],
            handlers["get_chapter"],
            {"response_model": ChapterDetail},
        ),
        (
            "/api/projects/{project_id}/chapters/{chapter_number}/ledger",
            ["GET"],
            get_chapter_observability_ledger,
            {"response_model": ChapterLedgerResponse},
        ),
        (
            "/api/observability/performance/tasks/{task_id}",
            ["GET"],
            get_task_performance_report,
            {"response_model": PerformanceReportResponse},
        ),
        (
            "/api/observability/performance/projects/{project_id}",
            ["GET"],
            get_project_performance_report,
            {"response_model": PerformanceReportResponse},
        ),
        (
            "/api/observability/performance/projects/{project_id}/chapters/{chapter_number}",
            ["GET"],
            get_chapter_performance_report,
            {"response_model": PerformanceReportResponse},
        ),
        (
            "/api/observability/performance/slow-spans",
            ["GET"],
            get_slow_performance_spans,
            {"response_model": list[PerformanceSpanInfo]},
        ),
        (
            "/api/observability/performance/llm",
            ["GET"],
            get_llm_performance_report,
            {"response_model": PerformanceReportResponse},
        ),
        (
            "/api/observability/performance/db",
            ["GET"],
            get_db_performance_report,
            {"response_model": PerformanceReportResponse},
        ),
        (
            "/api/projects/{project_id}/chapters/{chapter_number}/candidate-draft",
            ["GET"],
            handlers["get_candidate_draft"],
            {"response_model": CandidateDraftDetail},
        ),
        (
            "/api/projects/{project_id}/publishers/upload-jobs",
            ["POST"],
            handlers["create_project_chapter_upload_job"],
            {"response_model": PublisherUploadJobResponse},
        ),
        (
            "/api/projects/{project_id}/chapters/{chapter_number}/review",
            ["GET"],
            handlers["get_chapter_review"],
            {"response_model": ChapterReviewDetail},
        ),
        (
            "/api/prompt-traces/{trace_id}",
            ["GET"],
            get_prompt_trace_detail,
            {"response_model": PromptTraceDetailResponse},
        ),
        (
            "/api/artifacts/read",
            ["GET"],
            read_artifact_preview,
            {"response_model": ArtifactReadResponse},
        ),
        (
            "/api/tropes/templates",
            ["GET"],
            handlers["get_trope_templates"],
            {"response_model": list[TropeTemplateInfo]},
        ),
        (
            "/api/tropes/templates/summary",
            ["GET"],
            handlers["get_trope_template_summary"],
            {"response_model": TropeRegistrySummaryResponse},
        ),
        (
            "/api/tropes/templates/validate",
            ["POST"],
            handlers["validate_trope_templates"],
            {"response_model": TropeTemplateValidationResponse},
        ),
        (
            "/api/projects/{project_id}/bands/{band_id}/experience",
            ["POST"],
            handlers["override_band_experience"],
            {"response_model": BandExperienceOverrideResponse},
        ),
        (
            "/api/projects/{project_id}/chapters/{chapter_number}/review/approve",
            ["POST"],
            handlers["approve_chapter_review"],
            {"response_model": ChapterReviewApproveResponse},
        ),
        (
            "/api/projects/{project_id}/chapters/{chapter_number}/review/retry",
            ["POST"],
            handlers["retry_chapter_review"],
            {"response_model": ChapterReviewApproveResponse},
        ),
    ]

    for path, methods, endpoint, options in route_definitions:
        app.add_api_route(path, endpoint, methods=methods, **options)
    return handlers
