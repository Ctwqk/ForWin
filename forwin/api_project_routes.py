from __future__ import annotations

from typing import Any, Callable

from fastapi import HTTPException

from forwin import api_project_ops
from forwin.api_project_payloads import normalize_project_automation
from forwin.api_schemas import (
    BookGenesisPatchRequest,
    BookGenesisNameGenerateRequest,
    BookGenesisRefineRequest,
    BookGenesisStageRunRequest,
    ChapterReviewApproveRequest,
    ChapterReviewRetryRequest,
    ProjectAutomationUpdateResponse,
    ProjectAutomationUpdateRequest,
    ProjectBulkDeleteRequest,
    ProjectChapterPublishRequest,
    ProjectContinueGenerationRequest,
    ProjectCreateRequest,
    ProjectExtendGenerationRequest,
)
from forwin.models.project import Project


def _update_project_automation(
    project_id: str,
    req: ProjectAutomationUpdateRequest,
    *,
    get_session: Callable[[], Any],
    persist_project_automation: Callable[..., Any],
) -> ProjectAutomationUpdateResponse:
    session = get_session()
    try:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        current = normalize_project_automation(project.automation_json)
        payload = current.model_dump(mode="json")
        payload.update(
            {
                "enabled": bool(req.enabled),
                "daily_start_time": req.daily_start_time,
                "daily_chapter_quota": req.daily_chapter_quota,
                "daily_plan_quota": req.daily_plan_quota,
                "daily_write_quota": req.daily_write_quota,
                "daily_review_quota": req.daily_review_quota,
                "daily_publish_quota": req.daily_publish_quota,
                "stop_when_review_pending": bool(req.stop_when_review_pending),
                "auto_publish": bool(req.auto_publish),
            }
        )
        if req.publish is not None:
            payload["publish"] = req.publish.model_dump(mode="json")
        if req.publish_bindings is not None:
            payload["publish_bindings"] = [
                binding.model_dump(mode="json")
                for binding in req.publish_bindings
            ]
        updated = normalize_project_automation(payload)
        stored = persist_project_automation(session, project, updated)
        session.commit()
        return ProjectAutomationUpdateResponse(
            ok=True,
            project_id=project_id,
            automation=stored,
            message="书本自动化设置已保存。",
        )
    finally:
        session.close()


def build_handlers(
    *,
    get_session: Callable[[], Any],
    get_config: Callable[[], Any],
    get_runtime_settings: Callable[[], Any],
    get_orchestrator: Callable[[], Any],
    get_publisher_manager: Callable[[], Any],
    display_datetime: Callable[[Any], str],
    build_genesis_service: Callable[..., Any],
    close_genesis_service: Callable[..., None],
    require_genesis_project: Callable[[Any], None],
    active_genesis_revision: Callable[..., Any],
    genesis_patch_payload: Callable[[Any], dict[str, Any]],
    delete_project_impl: Callable[..., None],
    project_delete_blockers: Callable[..., list[str]],
    project_delete_conflict_message: Callable[[list[str]], str],
    saved_runtime_config_or_default: Callable[..., Any],
    project_has_active_generation_task: Callable[..., bool],
    generation_task_conflict_message: Callable[[str], str],
    create_continue_generation_task: Callable[..., str],
    persist_project_automation: Callable[..., Any],
    resolve_project_governance: Callable[..., Any],
    governance_request_payload: Callable[[object], dict[str, object]],
    log_decision_event: Callable[..., Any],
    serialize_task: Callable[[str, dict[str, Any]], Any],
    get_generation_task_or_404: Callable[[str], dict[str, Any]],
    active_generation_task_error_cls: type[Exception],
    require_reason: Callable[[str], str],
    decision_refs_for_chapter_review: Callable[..., list[Any]],
    update_task: Callable[..., None],
) -> dict[str, Callable[..., Any]]:
    def list_projects():
        return api_project_ops.list_projects(
            get_session=get_session,
            config=get_config(),
            display_datetime=display_datetime,
        )

    def create_project(req: ProjectCreateRequest):
        return api_project_ops.create_project(
            req,
            get_session=get_session,
            config=get_config(),
            build_genesis_service=build_genesis_service,
            close_genesis_service=close_genesis_service,
            log_decision_event=log_decision_event,
        )

    def delete_project(project_id: str):
        return api_project_ops.delete_project(
            project_id,
            get_session=get_session,
            config=get_config(),
            delete_project_impl=delete_project_impl,
            project_delete_blockers=project_delete_blockers,
            project_delete_conflict_message=project_delete_conflict_message,
        )

    def bulk_delete_projects(req: ProjectBulkDeleteRequest):
        return api_project_ops.bulk_delete_projects(
            req,
            get_session=get_session,
            config=get_config(),
            delete_project_impl=delete_project_impl,
            project_delete_blockers=project_delete_blockers,
        )

    def get_project(project_id: str):
        return api_project_ops.get_project(
            project_id,
            get_session=get_session,
            config=get_config(),
            display_datetime=display_datetime,
        )

    def get_project_genesis(project_id: str):
        return api_project_ops.get_project_genesis(
            project_id,
            get_session=get_session,
            build_genesis_service=build_genesis_service,
            close_genesis_service=close_genesis_service,
            require_genesis_project=require_genesis_project,
        )

    def patch_project_genesis(project_id: str, req: BookGenesisPatchRequest):
        return api_project_ops.patch_project_genesis(
            project_id,
            req,
            get_session=get_session,
            build_genesis_service=build_genesis_service,
            close_genesis_service=close_genesis_service,
            require_genesis_project=require_genesis_project,
            active_genesis_revision=active_genesis_revision,
            genesis_patch_payload=genesis_patch_payload,
        )

    def generate_project_genesis_stage(
        project_id: str,
        stage_key: str,
        req: BookGenesisStageRunRequest | None = None,
    ):
        return api_project_ops.generate_project_genesis_stage(
            project_id,
            stage_key,
            req,
            get_session=get_session,
            build_genesis_service=build_genesis_service,
            close_genesis_service=close_genesis_service,
            require_genesis_project=require_genesis_project,
            active_genesis_revision=active_genesis_revision,
        )

    def lock_project_genesis_stage(project_id: str, stage_key: str):
        return api_project_ops.lock_project_genesis_stage(
            project_id,
            stage_key,
            get_session=get_session,
            build_genesis_service=build_genesis_service,
            close_genesis_service=close_genesis_service,
            require_genesis_project=require_genesis_project,
            active_genesis_revision=active_genesis_revision,
        )

    def rerun_project_genesis_stage(
        project_id: str,
        stage_key: str,
        req: BookGenesisStageRunRequest | None = None,
    ):
        return api_project_ops.rerun_project_genesis_stage(
            project_id,
            stage_key,
            req,
            get_session=get_session,
            build_genesis_service=build_genesis_service,
            close_genesis_service=close_genesis_service,
            require_genesis_project=require_genesis_project,
            active_genesis_revision=active_genesis_revision,
        )

    def refine_project_genesis_stage(project_id: str, stage_key: str, req: BookGenesisRefineRequest):
        return api_project_ops.refine_project_genesis_stage(
            project_id,
            stage_key,
            req,
            get_session=get_session,
            build_genesis_service=build_genesis_service,
            close_genesis_service=close_genesis_service,
            require_genesis_project=require_genesis_project,
            active_genesis_revision=active_genesis_revision,
        )

    def generate_project_genesis_name(project_id: str, req: BookGenesisNameGenerateRequest):
        return api_project_ops.generate_project_genesis_name(
            project_id,
            req,
            get_session=get_session,
            build_genesis_service=build_genesis_service,
            close_genesis_service=close_genesis_service,
            require_genesis_project=require_genesis_project,
            active_genesis_revision=active_genesis_revision,
        )

    def start_project_writing(project_id: str):
        return api_project_ops.start_project_writing(
            project_id,
            get_session=get_session,
            config=get_config(),
            saved_runtime_config_or_default=saved_runtime_config_or_default,
            build_genesis_service=build_genesis_service,
            close_genesis_service=close_genesis_service,
            require_genesis_project=require_genesis_project,
            active_genesis_revision=active_genesis_revision,
            project_has_active_generation_task=project_has_active_generation_task,
            generation_task_conflict_message=generation_task_conflict_message,
            create_continue_generation_task=create_continue_generation_task,
        )

    def continue_project_generation(
        project_id: str,
        req: ProjectContinueGenerationRequest | None = None,
    ):
        return api_project_ops.continue_project_generation(
            project_id,
            req,
            get_session=get_session,
            config=get_config(),
            runtime_settings=get_runtime_settings(),
            display_datetime=display_datetime,
            active_generation_task_error_cls=active_generation_task_error_cls,
            resolve_project_governance=resolve_project_governance,
            governance_request_payload=governance_request_payload,
            project_has_active_generation_task=project_has_active_generation_task,
            generation_task_conflict_message=generation_task_conflict_message,
            log_decision_event=log_decision_event,
            create_continue_generation_task=create_continue_generation_task,
            serialize_task=serialize_task,
            get_generation_task_or_404=get_generation_task_or_404,
        )

    def extend_project_generation(
        project_id: str,
        req: ProjectExtendGenerationRequest,
    ):
        return api_project_ops.extend_project_generation(
            project_id,
            req,
            get_session=get_session,
            display_datetime=display_datetime,
            project_has_active_generation_task=project_has_active_generation_task,
            generation_task_conflict_message=generation_task_conflict_message,
        )

    def update_project_automation(project_id: str, req: ProjectAutomationUpdateRequest):
        return _update_project_automation(
            project_id,
            req,
            get_session=get_session,
            persist_project_automation=persist_project_automation,
        )

    def list_chapters(project_id: str):
        return api_project_ops.list_chapters(
            project_id,
            get_session=get_session,
        )

    def list_chapter_page(project_id: str, offset: int = 0, limit: int = 60):
        return api_project_ops.list_chapter_page(
            project_id,
            offset=offset,
            limit=limit,
            get_session=get_session,
        )

    def get_chapter(project_id: str, chapter_number: int):
        return api_project_ops.get_chapter(
            project_id,
            chapter_number,
            get_session=get_session,
        )

    def create_project_chapter_upload_job(project_id: str, req: ProjectChapterPublishRequest):
        return api_project_ops.create_project_chapter_upload_job(
            project_id,
            req,
            get_session=get_session,
            publisher_manager=get_publisher_manager(),
        )

    def get_chapter_review(project_id: str, chapter_number: int):
        return api_project_ops.get_chapter_review(
            project_id,
            chapter_number,
            get_session=get_session,
            decision_refs_for_chapter_review=decision_refs_for_chapter_review,
        )

    def get_candidate_draft(project_id: str, chapter_number: int):
        return api_project_ops.get_candidate_draft(
            project_id,
            chapter_number,
            get_session=get_session,
            decision_refs_for_chapter_review=decision_refs_for_chapter_review,
        )

    def approve_chapter_review(
        project_id: str,
        chapter_number: int,
        req: ChapterReviewApproveRequest,
    ):
        return api_project_ops.approve_chapter_review(
            project_id,
            chapter_number,
            req,
            config=get_config(),
            orchestrator=get_orchestrator(),
            runtime_settings=get_runtime_settings(),
            get_session=get_session,
            display_datetime=display_datetime,
            active_generation_task_error_cls=active_generation_task_error_cls,
            require_reason=require_reason,
            resolve_project_governance=resolve_project_governance,
            project_has_active_generation_task=project_has_active_generation_task,
            generation_task_conflict_message=generation_task_conflict_message,
            log_decision_event=log_decision_event,
            create_continue_generation_task=create_continue_generation_task,
            update_task=update_task,
        )

    def retry_chapter_review(
        project_id: str,
        chapter_number: int,
        req: ChapterReviewRetryRequest,
    ):
        return api_project_ops.retry_chapter_review(
            project_id,
            chapter_number,
            req,
            config=get_config(),
            runtime_settings=get_runtime_settings(),
            get_session=get_session,
            active_generation_task_error_cls=active_generation_task_error_cls,
            require_reason=require_reason,
            resolve_project_governance=resolve_project_governance,
            project_has_active_generation_task=project_has_active_generation_task,
            generation_task_conflict_message=generation_task_conflict_message,
            log_decision_event=log_decision_event,
            create_continue_generation_task=create_continue_generation_task,
        )

    return {
        "list_projects": list_projects,
        "create_project": create_project,
        "delete_project": delete_project,
        "bulk_delete_projects": bulk_delete_projects,
        "get_project": get_project,
        "get_project_genesis": get_project_genesis,
        "patch_project_genesis": patch_project_genesis,
        "generate_project_genesis_stage": generate_project_genesis_stage,
        "lock_project_genesis_stage": lock_project_genesis_stage,
        "rerun_project_genesis_stage": rerun_project_genesis_stage,
        "refine_project_genesis_stage": refine_project_genesis_stage,
        "generate_project_genesis_name": generate_project_genesis_name,
        "start_project_writing": start_project_writing,
        "continue_project_generation": continue_project_generation,
        "extend_project_generation": extend_project_generation,
        "update_project_automation": update_project_automation,
        "list_chapters": list_chapters,
        "list_chapter_page": list_chapter_page,
        "get_chapter": get_chapter,
        "create_project_chapter_upload_job": create_project_chapter_upload_job,
        "get_chapter_review": get_chapter_review,
        "get_candidate_draft": get_candidate_draft,
        "approve_chapter_review": approve_chapter_review,
        "retry_chapter_review": retry_chapter_review,
    }
