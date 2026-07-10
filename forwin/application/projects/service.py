from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from forwin.api_schema import (
    BookGenesisNameGenerateRequest,
    BookGenesisPatchRequest,
    BookGenesisRefineRequest,
    BookGenesisStageRunRequest,
    ChapterReviewApproveRequest,
    ChapterReviewRetryRequest,
    ProjectAutomationUpdateRequest,
    ProjectBulkDeleteRequest,
    ProjectChapterPublishRequest,
    ProjectContinueGenerationRequest,
    ProjectCreateRequest,
    ProjectExtendGenerationRequest,
    RuntimePolicyUpdateRequest,
    StartWritingRequest,
)
from forwin.application import runtime_policy

from . import chapters, generation, genesis, lifecycle, reviews


@dataclass(frozen=True, slots=True)
class ProjectApplicationDeps:
    get_session: Callable[[], Any]
    get_config: Callable[[], Any]
    get_pipeline: Callable[[], Any]
    get_publisher_manager: Callable[[], Any]
    display_datetime: Callable[[Any], str]
    build_genesis_service: Callable[..., Any]
    close_genesis_service: Callable[..., None]
    require_genesis_project: Callable[[Any], None]
    active_genesis_revision: Callable[..., Any]
    genesis_patch_payload: Callable[[Any], dict[str, Any]]
    delete_project_impl: Callable[..., None]
    project_delete_blockers: Callable[..., list[str]]
    project_delete_conflict_message: Callable[[list[str]], str]
    project_has_active_generation_task: Callable[..., bool]
    generation_task_conflict_message: Callable[[str], str]
    create_continue_generation_task: Callable[..., str]
    persist_project_automation: Callable[..., Any]
    log_decision_event: Callable[..., Any]
    serialize_task: Callable[[str, dict[str, Any]], Any]
    get_generation_task_or_404: Callable[[str], dict[str, Any]]
    active_generation_task_error_cls: type[Exception]
    require_reason: Callable[..., str]
    decision_refs_for_chapter_review: Callable[..., list[Any]]
    update_task: Callable[..., None]


class ProjectApplicationService:
    """Single application boundary for project, Genesis, chapter, and review actions."""

    def __init__(self, deps: ProjectApplicationDeps) -> None:
        self.deps = deps

    def list_projects(self):
        deps = self.deps
        return lifecycle.list_projects(
            get_session=deps.get_session,
            config=deps.get_config(),
            display_datetime=deps.display_datetime,
        )

    def create_project(self, req: ProjectCreateRequest):
        deps = self.deps
        return lifecycle.create_project(
            req,
            get_session=deps.get_session,
            config=deps.get_config(),
            build_genesis_service=deps.build_genesis_service,
            close_genesis_service=deps.close_genesis_service,
            log_decision_event=deps.log_decision_event,
        )

    def delete_project(self, project_id: str):
        deps = self.deps
        return lifecycle.delete_project(
            project_id,
            get_session=deps.get_session,
            config=deps.get_config(),
            delete_project_impl=deps.delete_project_impl,
            project_delete_blockers=deps.project_delete_blockers,
            project_delete_conflict_message=deps.project_delete_conflict_message,
        )

    def bulk_delete_projects(self, req: ProjectBulkDeleteRequest):
        deps = self.deps
        return lifecycle.bulk_delete_projects(
            req,
            get_session=deps.get_session,
            config=deps.get_config(),
            delete_project_impl=deps.delete_project_impl,
            project_delete_blockers=deps.project_delete_blockers,
        )

    def get_project(self, project_id: str):
        deps = self.deps
        return lifecycle.get_project(
            project_id,
            get_session=deps.get_session,
            config=deps.get_config(),
            display_datetime=deps.display_datetime,
        )

    def get_project_policy(self, project_id: str):
        return runtime_policy.get_project_policy(
            project_id,
            session_factory=self.deps.get_session,
        )

    def update_project_policy(
        self,
        project_id: str,
        req: RuntimePolicyUpdateRequest,
    ):
        return runtime_policy.update_project_policy(
            project_id,
            req,
            session_factory=self.deps.get_session,
        )

    def get_project_genesis(self, project_id: str):
        deps = self.deps
        return genesis.get_project_genesis(
            project_id,
            get_session=deps.get_session,
            build_genesis_service=deps.build_genesis_service,
            close_genesis_service=deps.close_genesis_service,
            require_genesis_project=deps.require_genesis_project,
        )

    def patch_project_genesis(
        self,
        project_id: str,
        req: BookGenesisPatchRequest,
    ):
        deps = self.deps
        return genesis.patch_project_genesis(
            project_id,
            req,
            get_session=deps.get_session,
            build_genesis_service=deps.build_genesis_service,
            close_genesis_service=deps.close_genesis_service,
            require_genesis_project=deps.require_genesis_project,
            active_genesis_revision=deps.active_genesis_revision,
            genesis_patch_payload=deps.genesis_patch_payload,
        )

    def generate_project_genesis_stage(
        self,
        project_id: str,
        stage_key: str,
        req: BookGenesisStageRunRequest | None = None,
    ):
        deps = self.deps
        return genesis.generate_project_genesis_stage(
            project_id,
            stage_key,
            req,
            get_session=deps.get_session,
            build_genesis_service=deps.build_genesis_service,
            close_genesis_service=deps.close_genesis_service,
            require_genesis_project=deps.require_genesis_project,
            active_genesis_revision=deps.active_genesis_revision,
        )

    def lock_project_genesis_stage(self, project_id: str, stage_key: str):
        deps = self.deps
        return genesis.lock_project_genesis_stage(
            project_id,
            stage_key,
            get_session=deps.get_session,
            build_genesis_service=deps.build_genesis_service,
            close_genesis_service=deps.close_genesis_service,
            require_genesis_project=deps.require_genesis_project,
            active_genesis_revision=deps.active_genesis_revision,
        )

    def rerun_project_genesis_stage(
        self,
        project_id: str,
        stage_key: str,
        req: BookGenesisStageRunRequest | None = None,
    ):
        deps = self.deps
        return genesis.rerun_project_genesis_stage(
            project_id,
            stage_key,
            req,
            get_session=deps.get_session,
            build_genesis_service=deps.build_genesis_service,
            close_genesis_service=deps.close_genesis_service,
            require_genesis_project=deps.require_genesis_project,
            active_genesis_revision=deps.active_genesis_revision,
        )

    def refine_project_genesis_stage(
        self,
        project_id: str,
        stage_key: str,
        req: BookGenesisRefineRequest,
    ):
        deps = self.deps
        return genesis.refine_project_genesis_stage(
            project_id,
            stage_key,
            req,
            get_session=deps.get_session,
            build_genesis_service=deps.build_genesis_service,
            close_genesis_service=deps.close_genesis_service,
            require_genesis_project=deps.require_genesis_project,
            active_genesis_revision=deps.active_genesis_revision,
        )

    def generate_project_genesis_name(
        self,
        project_id: str,
        req: BookGenesisNameGenerateRequest,
    ):
        deps = self.deps
        return genesis.generate_project_genesis_name(
            project_id,
            req,
            get_session=deps.get_session,
            build_genesis_service=deps.build_genesis_service,
            close_genesis_service=deps.close_genesis_service,
            require_genesis_project=deps.require_genesis_project,
            active_genesis_revision=deps.active_genesis_revision,
        )

    def start_project_writing(
        self,
        project_id: str,
        req: StartWritingRequest | None = None,
    ):
        deps = self.deps
        return genesis.start_project_writing(
            project_id,
            req,
            get_session=deps.get_session,
            config=deps.get_config(),
            build_genesis_service=deps.build_genesis_service,
            close_genesis_service=deps.close_genesis_service,
            require_genesis_project=deps.require_genesis_project,
            active_genesis_revision=deps.active_genesis_revision,
            project_has_active_generation_task=deps.project_has_active_generation_task,
            generation_task_conflict_message=deps.generation_task_conflict_message,
            create_continue_generation_task=deps.create_continue_generation_task,
        )

    def continue_project_generation(
        self,
        project_id: str,
        req: ProjectContinueGenerationRequest | None = None,
    ):
        deps = self.deps
        return generation.continue_project_generation(
            project_id,
            req,
            get_session=deps.get_session,
            config=deps.get_config(),
            display_datetime=deps.display_datetime,
            active_generation_task_error_cls=deps.active_generation_task_error_cls,
            project_has_active_generation_task=deps.project_has_active_generation_task,
            generation_task_conflict_message=deps.generation_task_conflict_message,
            log_decision_event=deps.log_decision_event,
            create_continue_generation_task=deps.create_continue_generation_task,
            serialize_task=deps.serialize_task,
            get_generation_task_or_404=deps.get_generation_task_or_404,
        )

    def extend_project_generation(
        self,
        project_id: str,
        req: ProjectExtendGenerationRequest,
    ):
        deps = self.deps
        return generation.extend_project_generation(
            project_id,
            req,
            get_session=deps.get_session,
            display_datetime=deps.display_datetime,
            project_has_active_generation_task=deps.project_has_active_generation_task,
            generation_task_conflict_message=deps.generation_task_conflict_message,
        )

    def update_project_automation(
        self,
        project_id: str,
        req: ProjectAutomationUpdateRequest,
    ):
        deps = self.deps
        return generation.update_project_automation(
            project_id,
            req,
            get_session=deps.get_session,
            persist_project_automation=deps.persist_project_automation,
        )

    def list_chapters(self, project_id: str):
        return chapters.list_chapters(
            project_id,
            get_session=self.deps.get_session,
        )

    def list_chapter_page(
        self,
        project_id: str,
        offset: int = 0,
        limit: int = 60,
    ):
        return chapters.list_chapter_page(
            project_id,
            offset=offset,
            limit=limit,
            get_session=self.deps.get_session,
        )

    def get_chapter(self, project_id: str, chapter_number: int):
        return chapters.get_chapter(
            project_id,
            chapter_number,
            get_session=self.deps.get_session,
        )

    def create_project_chapter_upload_job(
        self,
        project_id: str,
        req: ProjectChapterPublishRequest,
    ):
        deps = self.deps
        return chapters.create_project_chapter_upload_job(
            project_id,
            req,
            get_session=deps.get_session,
            publisher_manager=deps.get_publisher_manager(),
        )

    def get_chapter_review(self, project_id: str, chapter_number: int):
        deps = self.deps
        return reviews.get_chapter_review(
            project_id,
            chapter_number,
            get_session=deps.get_session,
            decision_refs_for_chapter_review=deps.decision_refs_for_chapter_review,
        )

    def get_candidate_draft(self, project_id: str, chapter_number: int):
        deps = self.deps
        return reviews.get_candidate_draft(
            project_id,
            chapter_number,
            get_session=deps.get_session,
            decision_refs_for_chapter_review=deps.decision_refs_for_chapter_review,
        )

    def approve_chapter_review(
        self,
        project_id: str,
        chapter_number: int,
        req: ChapterReviewApproveRequest,
    ):
        deps = self.deps
        return reviews.approve_chapter_review(
            project_id,
            chapter_number,
            req,
            config=deps.get_config(),
            pipeline=deps.get_pipeline(),
            get_session=deps.get_session,
            display_datetime=deps.display_datetime,
            active_generation_task_error_cls=deps.active_generation_task_error_cls,
            require_reason=deps.require_reason,
            project_has_active_generation_task=deps.project_has_active_generation_task,
            generation_task_conflict_message=deps.generation_task_conflict_message,
            log_decision_event=deps.log_decision_event,
            create_continue_generation_task=deps.create_continue_generation_task,
            update_task=deps.update_task,
        )

    def retry_chapter_review(
        self,
        project_id: str,
        chapter_number: int,
        req: ChapterReviewRetryRequest,
    ):
        deps = self.deps
        return reviews.retry_chapter_review(
            project_id,
            chapter_number,
            req,
            config=deps.get_config(),
            get_session=deps.get_session,
            active_generation_task_error_cls=deps.active_generation_task_error_cls,
            require_reason=deps.require_reason,
            project_has_active_generation_task=deps.project_has_active_generation_task,
            generation_task_conflict_message=deps.generation_task_conflict_message,
            log_decision_event=deps.log_decision_event,
            create_continue_generation_task=deps.create_continue_generation_task,
        )


__all__ = ["ProjectApplicationDeps", "ProjectApplicationService"]
