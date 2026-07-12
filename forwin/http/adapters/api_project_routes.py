from __future__ import annotations

from collections.abc import Callable
from typing import Any

from forwin.application.projects import ProjectApplicationService


def build_handlers(
    *,
    service: ProjectApplicationService,
) -> dict[str, Callable[..., Any]]:
    return {
        "list_projects": service.list_projects,
        "create_project": service.create_project,
        "delete_project": service.delete_project,
        "bulk_delete_projects": service.bulk_delete_projects,
        "get_project": service.get_project,
        "get_project_policy": service.get_project_policy,
        "update_project_policy": service.update_project_policy,
        "get_project_genesis": service.get_project_genesis,
        "patch_project_genesis": service.patch_project_genesis,
        "generate_project_genesis_stage": service.generate_project_genesis_stage,
        "lock_project_genesis_stage": service.lock_project_genesis_stage,
        "rerun_project_genesis_stage": service.rerun_project_genesis_stage,
        "refine_project_genesis_stage": service.refine_project_genesis_stage,
        "generate_project_genesis_name": service.generate_project_genesis_name,
        "start_project_writing": service.start_project_writing,
        "continue_project_generation": service.continue_project_generation,
        "extend_project_generation": service.extend_project_generation,
        "update_project_automation": service.update_project_automation,
        "list_chapters": service.list_chapters,
        "list_chapter_page": service.list_chapter_page,
        "get_chapter": service.get_chapter,
        "create_project_chapter_upload_job": service.create_project_chapter_upload_job,
        "get_chapter_review": service.get_chapter_review,
        "get_candidate_draft": service.get_candidate_draft,
        "approve_chapter_review": service.approve_chapter_review,
        "retry_chapter_review": service.retry_chapter_review,
    }


__all__ = ["build_handlers"]
