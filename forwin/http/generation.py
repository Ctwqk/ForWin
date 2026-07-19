"""ForWin Web API – FastAPI interface for the novel generation system."""

from __future__ import annotations

from sqlalchemy import select

from forwin.application.generation import (
    EnqueueGenerationCommand,
    GenerationApplicationService,
)
from forwin.audit.events import DecisionEventType
from forwin.models.publisher import (
    PublisherUploadJob,
)
from forwin.models.task import GenerationTask
import forwin.models.phase  # noqa: F401

from forwin.http.request_support import (
    _get_session,
)
from forwin.http.runtime import (
    GENERATION_TERMINAL_STATUSES,
    UPLOAD_TERMINAL_STATUSES,
    HttpRuntime,
)


def _active_generation_task_ids(
    runtime: HttpRuntime,
    project_id: str = "",
    *,
    session=None,
) -> list[str]:
    normalized_project_id = str(project_id or "").strip()

    def _query_active_ids(active_session) -> list[str]:
        criteria = [
            GenerationTask.deleted_at.is_(None),
            GenerationTask.task_kind == "generation",
            GenerationTask.status.notin_(tuple(GENERATION_TERMINAL_STATUSES)),
        ]
        if normalized_project_id:
            criteria.append(GenerationTask.project_id == normalized_project_id)
        return list(
            active_session.execute(
                select(GenerationTask.id)
                .where(*criteria)
                .order_by(
                    GenerationTask.updated_at.desc(),
                    GenerationTask.id.desc(),
                )
            )
            .scalars()
            .all()
        )

    if session is not None:
        return _query_active_ids(session)
    with _get_session(runtime) as managed_session:
        return _active_generation_task_ids(
            runtime,
            normalized_project_id,
            session=managed_session,
        )


def _project_has_active_generation_task(
    runtime: HttpRuntime,
    project_id: str,
    *,
    session=None,
) -> bool:
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id:
        return False
    return bool(
        _active_generation_task_ids(
            runtime,
            normalized_project_id,
            session=session,
        )
    )


def _project_has_active_upload_job(
    runtime: HttpRuntime,
    project_id: str,
    *,
    session=None,
) -> bool:
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id or runtime.session_factory is None:
        return False
    if session is not None:
        active_job_id = session.execute(
            select(PublisherUploadJob.id)
            .where(
                PublisherUploadJob.deleted_at.is_(None),
                PublisherUploadJob.project_id == normalized_project_id,
                PublisherUploadJob.status.notin_(
                    tuple(UPLOAD_TERMINAL_STATUSES)
                ),
            )
            .limit(1)
        ).scalar_one_or_none()
        return active_job_id is not None
    with _get_session(runtime) as managed_session:
        return _project_has_active_upload_job(
            runtime,
            normalized_project_id,
            session=managed_session,
        )


def _project_delete_conflict_message(blockers: list[str]) -> str:
    labels = {
        "generation": "生成任务",
        "upload": "发布任务",
    }
    blocker_text = "、".join(labels.get(item, item) for item in blockers)
    return f"项目存在运行中的{blocker_text}，请先终止后再删除。"


def _project_delete_blockers(
    runtime: HttpRuntime,
    project_id: str,
    *,
    session,
) -> list[str]:
    blockers: list[str] = []
    if _project_has_active_generation_task(runtime, project_id, session=session):
        blockers.append("generation")
    if _project_has_active_upload_job(runtime, project_id, session=session):
        blockers.append("upload")
    return blockers


def _create_continue_generation_task(
    runtime: HttpRuntime,
    *,
    project_id: str,
    requested_chapters: int,
    max_chapters: int | None = None,
    auto_continue: bool = True,
    run_until_chapter: int | None = None,
    title: str = "",
    subtitle: str = "",
    message: str = "",
) -> str:
    normalized_project_id = str(project_id or "").strip()
    return (
        _generation_application_service(runtime)
        .enqueue(
            EnqueueGenerationCommand(
                project_id=normalized_project_id,
                requested_chapters=int(requested_chapters or 0),
                max_chapters=int(max_chapters or 0),
                run_until_chapter=int(run_until_chapter or 0),
                auto_continue=bool(auto_continue),
                title=title or f"继续生成 {normalized_project_id}",
                subtitle=subtitle or f"项目 {normalized_project_id}",
                message=message or "准备继续后续章节。",
                root_event_type=DecisionEventType.CONTINUE_REQUESTED,
            )
        )
        .task_id
    )


def _generation_application_service(
    runtime: HttpRuntime,
) -> GenerationApplicationService:
    if runtime.container is not None:
        return runtime.container.build_generation_application_service()
    if runtime.session_factory is None or runtime.config is None:
        raise RuntimeError("generation application service is unavailable")
    return GenerationApplicationService(
        session_factory=runtime.session_factory,
        infrastructure=runtime.config,
    )


__all__ = [name for name in globals() if not name.startswith("__")]
