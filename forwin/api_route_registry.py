from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from forwin import (
    api_book_state_routes,
    api_governance_routes,
    api_llm_kb_routes,
    api_map_routes,
    api_obsidian_routes,
    api_project_routes,
    api_publisher_routes,
    api_system_routes,
    api_task_routes,
    api_world_model_routes,
    api_world_model_v4_routes,
)
from forwin.api_schemas import (
    ActiveGenerationTaskCheckResponse,
    ArtifactReadResponse,
    BandCheckpointDetail,
    BandExperienceOverrideResponse,
    BookGenesisDetail,
    BookGenesisNameGenerateResponse,
    BookStateLegacyImportResponse,
    BookStatePathResponse,
    BookStateRuntimeResponse,
    BulkDeleteResponse,
    CausalReplayResponse,
    CandidateDraftDetail,
    ChapterLedgerResponse,
    ChapterDetail,
    ChapterInfo,
    ChapterReviewApproveResponse,
    ChapterReviewDetail,
    CodexBridgeStatusResponse,
    DecisionEventsResponse,
    ExtensionBrowserSessionResponse,
    ExtensionClaimCommentSyncJobResponse,
    ExtensionClaimUploadJobResponse,
    ExtensionCommentsBatchResponse,
    ExtensionHeartbeatResponse,
    ExtensionSessionSyncResponse,
    PublisherBrowserSessionSummaryResponse,
    GovernanceInsightsResponse,
    LLMSettingsResponse,
    MapEnsureResponse,
    MapPathResponse,
    MapRuntimeResponse,
    NarrativeConstraintInfo,
    NarrativeConstraintsResponse,
    ProjectAutomationUpdateResponse,
    ProjectCreateResponse,
    ProjectDeleteResponse,
    ProjectDetail,
    ProjectGovernanceResponse,
    ProjectSummary,
    PromptTraceDetailResponse,
    ProvisionalBandDetail,
    ScenarioPlanPatchApproveRequest,
    ScenarioRehearsalDetail,
    PublisherCommentSyncJobResponse,
    PublisherPlatformInfo,
    PublisherUploadJobResponse,
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
    WorldEditProposalReviewRequest,
    WorldModelConflictInfo,
    WorldModelExportRequest,
    WorldModelExportResponse,
    WorldModelImportRequest,
    WorldModelImportResponse,
    WorldModelPageInfo,
    WorldModelSnapshotInfo,
    WorldModelV4DebugResponse,
    WorldModelV4ExportResponse,
    WorldModelV4GapInfo,
    WorldModelV4LineInfo,
    WorldModelV4RevealInfo,
)


@dataclass(frozen=True)
class ApiRouteDeps:
    get_config: Callable[[], Any]
    get_runtime_settings: Callable[[], Any]
    get_publisher_manager: Callable[[], Any]
    get_orchestrator: Callable[[], Any]
    get_session: Callable[[], Any]
    render_home_page: Callable[..., str]
    render_publishers_page: Callable[..., str]
    build_home_page_settings: Callable[..., dict[str, object]]
    build_runtime_config: Callable[..., Any]
    copy_config: Callable[..., Any]
    create_generation_task: Callable[..., str]
    serialize_task: Callable[[str, dict[str, Any]], Any]
    get_generation_task_or_404: Callable[[str], dict[str, Any]]
    project_has_active_generation_task: Callable[..., bool]
    active_generation_task_ids: Callable[[str], list[str]]
    generation_task_conflict_message: Callable[[str], str]
    resolve_project_governance: Callable[..., Any]
    governance_request_payload: Callable[[object], dict[str, object]]
    serialize_llm_settings: Callable[..., Any]
    active_generation_task_error_cls: type[Exception]
    list_generation_tasks: Callable[[int], list[tuple[str, dict[str, Any]]]]
    serialize_generation_task_center_item: Callable[[str, dict[str, Any]], Any]
    serialize_upload_task_center_item: Callable[[dict[str, Any]], Any]
    list_project_backed_task_items: Callable[[int], list[Any]]
    parse_project_task_id: Callable[[str], str | None]
    get_project_backed_task_item_or_404: Callable[[str], Any]
    task_is_terminal: Callable[[str], bool]
    task_is_terminable: Callable[[dict[str, Any]], bool]
    task_is_pausable: Callable[[dict[str, Any]], bool]
    task_is_deletable: Callable[[dict[str, Any]], bool]
    latest_related_decision_event: Callable[..., Any]
    log_decision_event: Callable[..., Any]
    update_task: Callable[..., None]
    display_datetime: Callable[[Any], str]
    build_genesis_service: Callable[..., Any]
    close_genesis_service: Callable[..., None]
    require_genesis_project: Callable[[Any], None]
    active_genesis_revision: Callable[..., Any]
    genesis_patch_payload: Callable[[Any], dict[str, Any]]
    delete_project_impl: Callable[..., None]
    project_delete_blockers: Callable[..., list[str]]
    project_delete_conflict_message: Callable[[list[str]], str]
    saved_runtime_config_or_default: Callable[..., Any]
    create_continue_generation_task: Callable[..., str]
    persist_project_automation: Callable[..., Any]
    require_reason: Callable[[str], str]
    decision_refs_for_chapter_review: Callable[..., list[Any]]
    validate_constraint_payload: Callable[..., tuple[str, str, str]]
    serialize_band_checkpoint: Callable[..., Any]
    serialize_constraint: Callable[[Any], Any]
    list_decision_event_rows: Callable[..., list[Any]]
    serialize_decision_event: Callable[[Any], Any]
    build_causal_replay: Callable[..., Any]
    build_governance_insights: Callable[..., Any]
    latest_band_checkpoint_row: Callable[..., Any]
    persist_project_governance: Callable[..., Any]
    json_load_object: Callable[[str | None], dict[str, Any]]
    get_task_timeline: Callable[..., Any]
    get_chapter_observability_ledger: Callable[..., Any]
    get_prompt_trace_detail: Callable[..., Any]
    read_artifact_preview: Callable[..., Any]


def register_api_routes(
    app: FastAPI,
    *,
    deps: ApiRouteDeps,
) -> dict[str, Callable[..., Any]]:
    get_config = deps.get_config
    get_runtime_settings = deps.get_runtime_settings
    get_publisher_manager = deps.get_publisher_manager
    get_orchestrator = deps.get_orchestrator
    get_session = deps.get_session
    render_home_page = deps.render_home_page
    render_publishers_page = deps.render_publishers_page
    build_home_page_settings = deps.build_home_page_settings
    build_runtime_config = deps.build_runtime_config
    copy_config = deps.copy_config
    create_generation_task = deps.create_generation_task
    serialize_task = deps.serialize_task
    get_generation_task_or_404 = deps.get_generation_task_or_404
    project_has_active_generation_task = deps.project_has_active_generation_task
    active_generation_task_ids = deps.active_generation_task_ids
    generation_task_conflict_message = deps.generation_task_conflict_message
    resolve_project_governance = deps.resolve_project_governance
    governance_request_payload = deps.governance_request_payload
    serialize_llm_settings = deps.serialize_llm_settings
    active_generation_task_error_cls = deps.active_generation_task_error_cls
    list_generation_tasks = deps.list_generation_tasks
    serialize_generation_task_center_item = deps.serialize_generation_task_center_item
    serialize_upload_task_center_item = deps.serialize_upload_task_center_item
    list_project_backed_task_items = deps.list_project_backed_task_items
    parse_project_task_id = deps.parse_project_task_id
    get_project_backed_task_item_or_404 = deps.get_project_backed_task_item_or_404
    task_is_terminal = deps.task_is_terminal
    task_is_terminable = deps.task_is_terminable
    task_is_pausable = deps.task_is_pausable
    task_is_deletable = deps.task_is_deletable
    latest_related_decision_event = deps.latest_related_decision_event
    log_decision_event = deps.log_decision_event
    update_task = deps.update_task
    display_datetime = deps.display_datetime
    build_genesis_service = deps.build_genesis_service
    close_genesis_service = deps.close_genesis_service
    require_genesis_project = deps.require_genesis_project
    active_genesis_revision = deps.active_genesis_revision
    genesis_patch_payload = deps.genesis_patch_payload
    delete_project_impl = deps.delete_project_impl
    project_delete_blockers = deps.project_delete_blockers
    project_delete_conflict_message = deps.project_delete_conflict_message
    saved_runtime_config_or_default = deps.saved_runtime_config_or_default
    create_continue_generation_task = deps.create_continue_generation_task
    persist_project_automation = deps.persist_project_automation
    require_reason = deps.require_reason
    decision_refs_for_chapter_review = deps.decision_refs_for_chapter_review
    validate_constraint_payload = deps.validate_constraint_payload
    serialize_band_checkpoint = deps.serialize_band_checkpoint
    serialize_constraint = deps.serialize_constraint
    list_decision_event_rows = deps.list_decision_event_rows
    serialize_decision_event = deps.serialize_decision_event
    build_causal_replay = deps.build_causal_replay
    build_governance_insights = deps.build_governance_insights
    latest_band_checkpoint_row = deps.latest_band_checkpoint_row
    persist_project_governance = deps.persist_project_governance
    json_load_object = deps.json_load_object
    get_task_timeline = deps.get_task_timeline
    get_chapter_observability_ledger = deps.get_chapter_observability_ledger
    get_prompt_trace_detail = deps.get_prompt_trace_detail
    read_artifact_preview = deps.read_artifact_preview

    system_handlers = api_system_routes.build_handlers(
        get_config=get_config,
        get_runtime_settings=get_runtime_settings,
        get_publisher_manager=get_publisher_manager,
        get_session=get_session,
        render_home_page=render_home_page,
        render_publishers_page=render_publishers_page,
        build_home_page_settings=build_home_page_settings,
        build_runtime_config=build_runtime_config,
        copy_config=copy_config,
        create_generation_task=create_generation_task,
        serialize_task=serialize_task,
        get_generation_task_or_404=get_generation_task_or_404,
        project_has_active_generation_task=project_has_active_generation_task,
        generation_task_conflict_message=generation_task_conflict_message,
        resolve_project_governance=resolve_project_governance,
        governance_request_payload=governance_request_payload,
        serialize_llm_settings=serialize_llm_settings,
        active_generation_task_error_cls=active_generation_task_error_cls,
    )
    task_handlers = api_task_routes.build_handlers(
        deps=api_task_routes.TaskRouteDeps(
            get_session=get_session,
            get_publisher_manager=get_publisher_manager,
            list_generation_tasks=list_generation_tasks,
            serialize_task=serialize_task,
            get_generation_task_or_404=get_generation_task_or_404,
            active_generation_task_ids=active_generation_task_ids,
            serialize_generation_task_center_item=serialize_generation_task_center_item,
            serialize_upload_task_center_item=serialize_upload_task_center_item,
            list_project_backed_task_items=list_project_backed_task_items,
            parse_project_task_id=parse_project_task_id,
            get_project_backed_task_item_or_404=get_project_backed_task_item_or_404,
            task_is_terminal=task_is_terminal,
            task_is_terminable=task_is_terminable,
            task_is_pausable=task_is_pausable,
            task_is_deletable=task_is_deletable,
            latest_related_decision_event=latest_related_decision_event,
            log_decision_event=log_decision_event,
            update_task=update_task,
        ),
    )
    publisher_handlers = api_publisher_routes.build_handlers(
        get_publisher_manager=get_publisher_manager,
        extension_root=Path.cwd() / "browser_extension" / "forwin-publisher",
    )
    project_handlers = api_project_routes.build_handlers(
        get_session=get_session,
        get_config=get_config,
        get_runtime_settings=get_runtime_settings,
        get_orchestrator=get_orchestrator,
        get_publisher_manager=get_publisher_manager,
        display_datetime=display_datetime,
        build_genesis_service=build_genesis_service,
        close_genesis_service=close_genesis_service,
        require_genesis_project=require_genesis_project,
        active_genesis_revision=active_genesis_revision,
        genesis_patch_payload=genesis_patch_payload,
        delete_project_impl=delete_project_impl,
        project_delete_blockers=project_delete_blockers,
        project_delete_conflict_message=project_delete_conflict_message,
        saved_runtime_config_or_default=saved_runtime_config_or_default,
        project_has_active_generation_task=project_has_active_generation_task,
        generation_task_conflict_message=generation_task_conflict_message,
        create_continue_generation_task=create_continue_generation_task,
        persist_project_automation=persist_project_automation,
        resolve_project_governance=resolve_project_governance,
        governance_request_payload=governance_request_payload,
        log_decision_event=log_decision_event,
        serialize_task=serialize_task,
        get_generation_task_or_404=get_generation_task_or_404,
        active_generation_task_error_cls=active_generation_task_error_cls,
        require_reason=require_reason,
        decision_refs_for_chapter_review=decision_refs_for_chapter_review,
        update_task=update_task,
    )
    governance_handlers = api_governance_routes.build_handlers(
        get_session=get_session,
        get_config=get_config,
        get_orchestrator=get_orchestrator,
        display_datetime=display_datetime,
        require_reason=require_reason,
        validate_constraint_payload=validate_constraint_payload,
        serialize_band_checkpoint=serialize_band_checkpoint,
        serialize_constraint=serialize_constraint,
        list_decision_event_rows=list_decision_event_rows,
        serialize_decision_event=serialize_decision_event,
        build_causal_replay=build_causal_replay,
        build_governance_insights=build_governance_insights,
        latest_band_checkpoint_row=latest_band_checkpoint_row,
        latest_related_decision_event=latest_related_decision_event,
        resolve_project_governance=resolve_project_governance,
        governance_request_payload=governance_request_payload,
        persist_project_governance=persist_project_governance,
        log_decision_event=log_decision_event,
        json_load_object=json_load_object,
    )
    world_model_handlers = api_world_model_routes.build_handlers(
        get_session=get_session,
        get_config=get_config,
    )
    world_model_v4_handlers = api_world_model_v4_routes.build_handlers(
        get_session=get_session,
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
    map_handlers = api_map_routes.build_handlers(
        get_session=get_session,
    )

    handlers = {
        **system_handlers,
        **task_handlers,
        **publisher_handlers,
        **project_handlers,
        **governance_handlers,
        **world_model_handlers,
        **world_model_v4_handlers,
        **book_state_handlers,
        **obsidian_handlers,
        **llm_kb_handlers,
        **map_handlers,
    }

    route_definitions = [
        ("/health", ["GET"], handlers["health"], {}),
        ("/", ["GET"], handlers["home_page"], {"response_class": HTMLResponse}),
        ("/publishers", ["GET"], handlers["publishers_page"], {"response_class": HTMLResponse}),
        ("/world-studio", ["GET"], handlers["world_studio_page"], {"response_class": HTMLResponse}),
        ("/world-studio/assets/{asset_path:path}", ["GET"], handlers["world_studio_asset"], {}),
        ("/api/generate", ["POST"], handlers["generate"], {"response_model": TaskResponse}),
        ("/api/settings/llm", ["GET"], handlers["get_llm_settings"], {"response_model": LLMSettingsResponse}),
        ("/api/settings/llm", ["POST"], handlers["save_llm_settings"], {"response_model": LLMSettingsResponse}),
        ("/api/settings/llm/preferences", ["POST"], handlers["save_llm_preferences"], {"response_model": LLMSettingsResponse}),
        ("/api/settings/llm/profiles", ["POST"], handlers["save_llm_profile"], {"response_model": LLMSettingsResponse}),
        ("/api/settings/llm/default-profile", ["POST"], handlers["set_default_llm_profile"], {"response_model": LLMSettingsResponse}),
        ("/api/settings/llm/profiles/{profile_id}", ["DELETE"], handlers["delete_llm_profile"], {"response_model": LLMSettingsResponse}),
        ("/api/settings/codex/health", ["GET"], handlers["get_codex_bridge_status"], {"response_model": CodexBridgeStatusResponse}),
        ("/api/tasks/active-generation-check", ["GET"], handlers["active_generation_task_check"], {"response_model": ActiveGenerationTaskCheckResponse}),
        ("/api/tasks/{task_id}", ["GET"], handlers["get_task"], {"response_model": TaskResponse}),
        ("/api/tasks/{task_id}/timeline", ["GET"], get_task_timeline, {"response_model": TaskTimelineResponse}),
        ("/api/tasks", ["GET"], handlers["list_tasks"], {"response_model": list[TaskSummaryResponse]}),
        ("/api/task-center/items", ["GET"], handlers["list_task_center_items"], {"response_model": list[TaskCenterItemResponse]}),
        ("/api/task-center/items/{task_kind}/{task_id}", ["GET"], handlers["get_task_center_item"], {"response_model": TaskCenterItemResponse}),
        ("/api/tasks/{task_id}/terminate", ["POST"], handlers["terminate_task"], {"response_model": TaskMutationResponse}),
        ("/api/tasks/{task_id}/pause", ["POST"], handlers["pause_task"], {"response_model": TaskMutationResponse}),
        ("/api/tasks/{task_id}", ["DELETE"], handlers["delete_task"], {"response_model": TaskMutationResponse}),
        ("/api/tasks/bulk-delete", ["POST"], handlers["bulk_delete_tasks"], {"response_model": BulkDeleteResponse}),
        ("/api/publishers/extension-package", ["GET"], handlers["download_publisher_extension_package"], {}),
        ("/api/publishers/platforms", ["GET"], handlers["list_publisher_platforms"], {"response_model": list[PublisherPlatformInfo]}),
        ("/api/publishers/upload-jobs", ["POST"], handlers["create_publisher_upload_job"], {"response_model": PublisherUploadJobResponse}),
        ("/api/publishers/upload-jobs/{job_id}", ["GET"], handlers["get_publisher_upload_job"], {"response_model": PublisherUploadJobResponse}),
        ("/api/publishers/upload-jobs", ["GET"], handlers["list_publisher_upload_jobs"], {"response_model": list[PublisherUploadJobResponse]}),
        ("/api/publishers/upload-jobs/{job_id}/terminate", ["POST"], handlers["terminate_publisher_upload_job"], {"response_model": TaskMutationResponse}),
        ("/api/publishers/upload-jobs/{job_id}", ["DELETE"], handlers["delete_publisher_upload_job"], {"response_model": TaskMutationResponse}),
        ("/api/publishers/extension/heartbeat", ["POST"], handlers["publisher_extension_heartbeat"], {"response_model": ExtensionHeartbeatResponse}),
        ("/api/publishers/extension/heartbeat-status", ["GET"], handlers["publisher_extension_heartbeat_status"], {}),
        ("/api/publishers/extension/session-sync", ["POST"], handlers["publisher_extension_session_sync"], {"response_model": ExtensionSessionSyncResponse}),
        ("/api/publishers/browser-sessions/{platform}", ["GET"], handlers["get_publisher_browser_session_summary"], {"response_model": PublisherBrowserSessionSummaryResponse | None}),
        ("/api/publishers/extension/browser-sessions/{platform}", ["GET"], handlers["publisher_extension_get_browser_session"], {"response_model": ExtensionBrowserSessionResponse | None}),
        ("/api/publishers/upload-jobs/{job_id}/result", ["POST"], handlers["update_publisher_upload_job_result"], {"response_model": PublisherUploadJobResponse}),
        ("/api/publishers/extension/upload-jobs/claim", ["POST"], handlers["claim_publisher_upload_job"], {"response_model": ExtensionClaimUploadJobResponse}),
        ("/api/publishers/extension/comment-sync-jobs/claim", ["POST"], handlers["claim_publisher_comment_sync_job"], {"response_model": ExtensionClaimCommentSyncJobResponse}),
        ("/api/publishers/comment-sync-jobs", ["POST"], handlers["create_publisher_comment_sync_job"], {"response_model": PublisherCommentSyncJobResponse}),
        ("/api/publishers/comment-sync-jobs/{job_id}/result", ["POST"], handlers["update_publisher_comment_sync_job_result"], {"response_model": PublisherCommentSyncJobResponse}),
        ("/api/publishers/extension/comments/batch", ["POST"], handlers["ingest_publisher_comments_batch"], {"response_model": ExtensionCommentsBatchResponse}),
        ("/api/projects", ["GET"], handlers["list_projects"], {"response_model": list[ProjectSummary]}),
        ("/api/projects", ["POST"], handlers["create_project"], {"response_model": ProjectCreateResponse}),
        ("/api/projects/{project_id}", ["DELETE"], handlers["delete_project"], {"response_model": ProjectDeleteResponse}),
        ("/api/projects/bulk-delete", ["POST"], handlers["bulk_delete_projects"], {"response_model": BulkDeleteResponse}),
        ("/api/projects/{project_id}", ["GET"], handlers["get_project"], {"response_model": ProjectDetail}),
        ("/api/projects/{project_id}/genesis", ["GET"], handlers["get_project_genesis"], {"response_model": BookGenesisDetail}),
        ("/api/projects/{project_id}/genesis", ["PATCH"], handlers["patch_project_genesis"], {"response_model": BookGenesisDetail}),
        ("/api/projects/{project_id}/genesis/stages/{stage_key}/generate", ["POST"], handlers["generate_project_genesis_stage"], {"response_model": BookGenesisDetail}),
        ("/api/projects/{project_id}/genesis/stages/{stage_key}/lock", ["POST"], handlers["lock_project_genesis_stage"], {"response_model": BookGenesisDetail}),
        ("/api/projects/{project_id}/genesis/stages/{stage_key}/rerun", ["POST"], handlers["rerun_project_genesis_stage"], {"response_model": BookGenesisDetail}),
        ("/api/projects/{project_id}/genesis/stages/{stage_key}/refine", ["POST"], handlers["refine_project_genesis_stage"], {"response_model": BookGenesisDetail}),
        ("/api/projects/{project_id}/genesis/generate-name", ["POST"], handlers["generate_project_genesis_name"], {"response_model": BookGenesisNameGenerateResponse}),
        ("/api/projects/{project_id}/world-model/snapshots", ["GET"], handlers["list_project_world_model_snapshots"], {"response_model": list[WorldModelSnapshotInfo]}),
        ("/api/projects/{project_id}/world-model/snapshots/latest", ["GET"], handlers["get_latest_project_world_model_snapshot"], {"response_model": WorldModelSnapshotInfo}),
        ("/api/projects/{project_id}/world-model/pages", ["GET"], handlers["list_project_world_model_pages"], {"response_model": list[WorldModelPageInfo]}),
        ("/api/projects/{project_id}/world-model/pages/{page_key}", ["GET"], handlers["get_project_world_model_page"], {"response_model": WorldModelPageInfo}),
        ("/api/projects/{project_id}/world-model/conflicts", ["GET"], handlers["list_project_world_model_conflicts"], {"response_model": list[WorldModelConflictInfo]}),
        ("/api/projects/{project_id}/world-model/export-obsidian", ["POST"], handlers["export_project_world_model"], {"response_model": WorldModelExportResponse}),
        ("/api/projects/{project_id}/world-model/import-obsidian", ["POST"], handlers["import_project_world_model"], {"response_model": WorldModelImportResponse}),
        ("/api/projects/{project_id}/world-model/proposals", ["GET"], handlers["list_project_world_model_proposals"], {"response_model": list[WorldEditProposalInfo]}),
        ("/api/projects/{project_id}/world-model/proposals/{proposal_id}/review", ["POST"], handlers["review_project_world_model_proposal"], {"response_model": WorldEditProposalInfo}),
        ("/api/projects/{project_id}/start-writing", ["POST"], handlers["start_project_writing"], {"response_model": StartWritingResponse}),
        ("/api/projects/{project_id}/continue-generation", ["POST"], handlers["continue_project_generation"], {"response_model": TaskResponse}),
        ("/api/projects/{project_id}/automation", ["PUT"], handlers["update_project_automation"], {"response_model": ProjectAutomationUpdateResponse}),
        ("/api/projects/{project_id}/governance", ["GET"], handlers["get_project_governance"], {"response_model": ProjectGovernanceResponse}),
        ("/api/projects/{project_id}/governance", ["PUT"], handlers["update_project_governance"], {"response_model": ProjectGovernanceResponse}),
        ("/api/projects/{project_id}/manual-checkpoints", ["POST"], handlers["create_manual_checkpoint"], {"response_model": BandCheckpointDetail}),
        ("/api/projects/{project_id}/bands/{band_id}/checkpoint", ["GET"], handlers["get_band_checkpoint"], {"response_model": BandCheckpointDetail}),
        ("/api/projects/{project_id}/bands/{band_id}/checkpoint/approve", ["POST"], handlers["approve_band_checkpoint"], {"response_model": BandCheckpointDetail}),
        ("/api/projects/{project_id}/chapters/{chapter_number}/task-contract", ["GET"], handlers["get_chapter_task_contract"], {"response_model": TaskContractResponse}),
        ("/api/projects/{project_id}/chapters/{chapter_number}/task-contract", ["PUT"], handlers["update_chapter_task_contract"], {"response_model": TaskContractResponse}),
        ("/api/projects/{project_id}/bands/{band_id}/task-contract", ["GET"], handlers["get_band_task_contract"], {"response_model": TaskContractResponse}),
        ("/api/projects/{project_id}/bands/{band_id}/task-contract", ["PUT"], handlers["update_band_task_contract"], {"response_model": TaskContractResponse}),
        ("/api/projects/{project_id}/constraints", ["GET"], handlers["list_project_constraints"], {"response_model": NarrativeConstraintsResponse}),
        ("/api/projects/{project_id}/constraints", ["POST"], handlers["create_project_constraint"], {"response_model": NarrativeConstraintInfo}),
        ("/api/projects/{project_id}/constraints/{constraint_id}", ["PATCH"], handlers["update_project_constraint"], {"response_model": NarrativeConstraintInfo}),
        ("/api/projects/{project_id}/decision-events", ["GET"], handlers["list_project_decision_events"], {"response_model": DecisionEventsResponse}),
        ("/api/projects/{project_id}/causal-replay", ["GET"], handlers["get_project_causal_replay"], {"response_model": CausalReplayResponse}),
        ("/api/projects/{project_id}/governance-insights", ["GET"], handlers["get_project_governance_insights"], {"response_model": GovernanceInsightsResponse}),
        ("/api/projects/{project_id}/provisional/latest", ["GET"], handlers["get_latest_provisional_band"], {"response_model": ProvisionalBandDetail}),
        ("/api/projects/{project_id}/scenario-rehearsal/latest", ["GET"], handlers["get_latest_scenario_rehearsal"], {"response_model": ScenarioRehearsalDetail}),
        ("/api/projects/{project_id}/scenario-rehearsal/{run_id}/rerun", ["POST"], handlers["rerun_scenario_rehearsal"], {"response_model": ScenarioRehearsalDetail}),
        ("/api/projects/{project_id}/scenario-rehearsal/patches/{patch_id}/approve", ["POST"], handlers["approve_scenario_plan_patch"], {"response_model": ScenarioRehearsalDetail}),
        ("/api/projects/{project_id}/world-model/v4/debug", ["GET"], handlers["get_world_model_v4_debug"], {"response_model": WorldModelV4DebugResponse}),
        ("/api/projects/{project_id}/world-model/v4/lines", ["GET"], handlers["get_world_model_v4_lines"], {"response_model": list[WorldModelV4LineInfo]}),
        ("/api/projects/{project_id}/world-model/v4/gaps", ["GET"], handlers["get_world_model_v4_gaps"], {"response_model": list[WorldModelV4GapInfo]}),
        ("/api/projects/{project_id}/world-model/v4/reveals", ["GET"], handlers["get_world_model_v4_reveals"], {"response_model": list[WorldModelV4RevealInfo]}),
        ("/api/projects/{project_id}/world-model/v4/export", ["GET"], handlers["get_world_model_v4_export"], {"response_model": WorldModelV4ExportResponse}),
        ("/api/personality-skills", ["GET"], handlers["list_personality_skills"], {}),
        ("/api/projects/{project_id}/characters", ["POST"], handlers["create_character"], {}),
        ("/api/projects/{project_id}/characters/personality/preview", ["POST"], handlers["preview_character_personality"], {}),
        ("/api/projects/{project_id}/characters/personality/active-context/preview", ["POST"], handlers["preview_character_active_personality_context"], {}),
        ("/api/projects/{project_id}/characters/personality/relationships/enrich", ["POST"], handlers["enrich_character_relationships"], {}),
        ("/api/projects/{project_id}/characters/personality/coverage", ["GET"], handlers["get_character_personality_coverage"], {}),
        ("/api/projects/{project_id}/characters/personality/metrics", ["GET"], handlers["get_character_personality_metrics"], {}),
        ("/api/projects/{project_id}/characters/personality/backfill", ["POST"], handlers["backfill_character_personalities"], {}),
        ("/api/projects/{project_id}/characters/personality/assignment-reports/{assignment_id}", ["GET"], handlers["get_character_assignment_report_by_id"], {}),
        ("/api/projects/{project_id}/characters/{character_id}/personality/reassign", ["POST"], handlers["reassign_character_personality"], {}),
        ("/api/projects/{project_id}/characters/{character_id}/personality/assignment-report", ["GET"], handlers["get_character_assignment_report"], {}),
        ("/api/projects/{project_id}/book-state/snapshot", ["GET"], handlers["get_book_state_snapshot"], {}),
        ("/api/projects/{project_id}/book-state/nodes", ["GET"], handlers["list_book_state_nodes"], {}),
        ("/api/projects/{project_id}/book-state/edges", ["GET"], handlers["list_book_state_edges"], {}),
        ("/api/projects/{project_id}/book-state/deltas", ["GET"], handlers["list_book_state_deltas"], {}),
        ("/api/projects/{project_id}/book-state/cognition", ["GET"], handlers["list_book_state_cognition"], {}),
        ("/api/projects/{project_id}/book-state/reader-promises", ["GET"], handlers["list_book_state_reader_promises"], {}),
        ("/api/projects/{project_id}/book-state/characters/personality", ["GET"], handlers["list_character_personality_loadouts"], {}),
        ("/api/projects/{project_id}/book-state/characters/{character_id}/personality-loadout", ["GET"], handlers["get_character_personality_loadout"], {}),
        ("/api/projects/{project_id}/book-state/characters/{character_id}/personality-loadout", ["PUT"], handlers["set_character_personality_loadout"], {}),
        ("/api/projects/{project_id}/book-state/runtime", ["GET"], handlers["get_book_state_runtime"], {"response_model": BookStateRuntimeResponse}),
        ("/api/projects/{project_id}/book-state/map/path", ["GET"], handlers["get_book_state_path"], {"response_model": BookStatePathResponse}),
        ("/api/projects/{project_id}/book-state/legacy-import", ["POST"], handlers["import_book_state_legacy"], {"response_model": BookStateLegacyImportResponse}),
        ("/api/projects/{project_id}/map/runtime", ["GET"], handlers["get_project_map_runtime"], {"response_model": MapRuntimeResponse}),
        ("/api/projects/{project_id}/map/path", ["GET"], handlers["get_project_map_path"], {"response_model": MapPathResponse}),
        ("/api/projects/{project_id}/map/ensure-from-genesis", ["POST"], handlers["ensure_project_map_from_genesis"], {"response_model": MapEnsureResponse}),
        ("/api/projects/{project_id}/obsidian/export", ["POST"], handlers["export_obsidian"], {"response_model": WorldModelExportResponse}),
        ("/api/projects/{project_id}/obsidian/import", ["POST"], handlers["import_obsidian"], {"response_model": WorldModelImportResponse}),
        ("/api/projects/{project_id}/obsidian/proposals", ["GET"], handlers["list_obsidian_proposals"], {"response_model": list[WorldEditProposalInfo]}),
        ("/api/projects/{project_id}/obsidian/proposals/{proposal_id}/approve", ["POST"], handlers["approve_obsidian_proposal"], {"response_model": WorldEditProposalInfo}),
        ("/api/projects/{project_id}/obsidian/proposals/{proposal_id}/reject", ["POST"], handlers["reject_obsidian_proposal"], {"response_model": WorldEditProposalInfo}),
        ("/api/projects/{project_id}/llm-kb/rebuild", ["POST"], handlers["rebuild_llm_kb"], {}),
        ("/api/projects/{project_id}/llm-kb/files", ["GET"], handlers["list_llm_kb_files"], {}),
        ("/api/projects/{project_id}/llm-kb/file/{file_key}", ["GET"], handlers["get_llm_kb_file"], {}),
        ("/api/projects/{project_id}/llm-kb/search", ["GET"], handlers["search_llm_kb"], {}),
        ("/api/projects/{project_id}/context-pack/{role}", ["GET"], handlers["get_context_pack"], {}),
        ("/api/projects/{project_id}/chapters", ["GET"], handlers["list_chapters"], {"response_model": list[ChapterInfo]}),
        ("/api/projects/{project_id}/chapters/{chapter_number}", ["GET"], handlers["get_chapter"], {"response_model": ChapterDetail}),
        ("/api/projects/{project_id}/chapters/{chapter_number}/ledger", ["GET"], get_chapter_observability_ledger, {"response_model": ChapterLedgerResponse}),
        ("/api/projects/{project_id}/chapters/{chapter_number}/candidate-draft", ["GET"], handlers["get_candidate_draft"], {"response_model": CandidateDraftDetail}),
        ("/api/projects/{project_id}/publishers/upload-jobs", ["POST"], handlers["create_project_chapter_upload_job"], {"response_model": PublisherUploadJobResponse}),
        ("/api/projects/{project_id}/chapters/{chapter_number}/review", ["GET"], handlers["get_chapter_review"], {"response_model": ChapterReviewDetail}),
        ("/api/prompt-traces/{trace_id}", ["GET"], get_prompt_trace_detail, {"response_model": PromptTraceDetailResponse}),
        ("/api/artifacts/read", ["GET"], read_artifact_preview, {"response_model": ArtifactReadResponse}),
        ("/api/tropes/templates", ["GET"], handlers["get_trope_templates"], {"response_model": list[TropeTemplateInfo]}),
        ("/api/tropes/templates/summary", ["GET"], handlers["get_trope_template_summary"], {"response_model": TropeRegistrySummaryResponse}),
        ("/api/tropes/templates/validate", ["POST"], handlers["validate_trope_templates"], {"response_model": TropeTemplateValidationResponse}),
        ("/api/projects/{project_id}/bands/{band_id}/experience", ["POST"], handlers["override_band_experience"], {"response_model": BandExperienceOverrideResponse}),
        ("/api/projects/{project_id}/chapters/{chapter_number}/review/approve", ["POST"], handlers["approve_chapter_review"], {"response_model": ChapterReviewApproveResponse}),
    ]

    for path, methods, endpoint, options in route_definitions:
        app.add_api_route(path, endpoint, methods=methods, **options)
    return handlers
