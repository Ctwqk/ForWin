from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Callable

from fastapi import HTTPException
from sqlalchemy.exc import OperationalError

from forwin.api_schemas import (
    ActiveGenerationTaskCheckResponse,
    BulkDeleteResponse,
    TaskBulkDeleteRequest,
    TaskMutationResponse,
)
from forwin.governance import DecisionEventType

logger = logging.getLogger(__name__)


def _is_retryable_db_error(exc: OperationalError) -> bool:
    orig = getattr(exc, "orig", None)
    sqlstate = str(getattr(orig, "sqlstate", "") or getattr(orig, "pgcode", "") or "").strip()
    if sqlstate in {"40001", "40P01", "55P03", "57014", "08000", "08003", "08006", "08001"}:
        return True
    message = str(exc).lower()
    return (
        "database is locked" in message
        or "database table is locked" in message
        or "deadlock detected" in message
        or "could not serialize access" in message
        or "lock timeout" in message
        or "connection refused" in message
        or "connection not open" in message
        or "server closed the connection" in message
        or "terminating connection" in message
    )


@dataclass(frozen=True)
class TaskRouteDeps:
    get_session: Callable[[], Any]
    get_publisher_manager: Callable[[], Any]
    list_generation_tasks: Callable[[int], list[tuple[str, dict[str, Any]]]]
    serialize_task: Callable[[str, dict[str, Any]], Any]
    get_generation_task_or_404: Callable[[str], dict[str, Any]]
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


def build_handlers(
    *,
    deps: TaskRouteDeps | None = None,
    **legacy_deps: Any,
) -> dict[str, Callable[..., Any]]:
    if deps is None:
        deps = TaskRouteDeps(**legacy_deps)
    elif legacy_deps:
        raise TypeError("build_handlers accepts either deps=TaskRouteDeps or legacy keyword dependencies, not both")

    def active_generation_task_check(project_id: str = "") -> ActiveGenerationTaskCheckResponse:
        normalized_project_id = str(project_id or "").strip()
        active_ids: list[str] = []
        for task_id, task in deps.list_generation_tasks(200):
            if str(task.get("task_kind", "generation")) != "generation":
                continue
            if task.get("deleted"):
                continue
            if normalized_project_id and str(task.get("project_id", "") or "").strip() != normalized_project_id:
                continue
            if deps.task_is_terminal(str(task.get("status", "")).strip()):
                continue
            active_ids.append(task_id)
        return ActiveGenerationTaskCheckResponse(
            has_active_generation_task=bool(active_ids),
            active_task_ids=active_ids,
            active_count=len(active_ids),
            safe_to_restart=not active_ids,
            message=(
                "存在 active generation task，重启前请等待、暂停或终止。"
                if active_ids
                else "当前没有 active generation task，可以安全重启。"
            ),
        )

    def get_task(task_id: str):
        task = deps.get_generation_task_or_404(task_id)
        return deps.serialize_task(task_id, task)

    def list_tasks(limit: int = 30):
        return [deps.serialize_task(task_id, task) for task_id, task in deps.list_generation_tasks(limit)]

    def list_task_center_items(limit: int = 50):
        normalized_limit = max(1, min(int(limit or 50), 100))
        generation_items = [
            deps.serialize_generation_task_center_item(task_id, task)
            for task_id, task in deps.list_generation_tasks(normalized_limit)
        ]
        project_items = deps.list_project_backed_task_items(normalized_limit)
        publisher_manager = deps.get_publisher_manager()
        upload_items = [
            deps.serialize_upload_task_center_item(item)
            for item in publisher_manager.list_upload_jobs(
                limit=normalized_limit,
                include_deleted=False,
            )
        ]
        combined = generation_items + project_items + upload_items
        combined.sort(key=lambda item: item.updated_at or item.created_at, reverse=True)
        return combined[:normalized_limit]

    def get_task_center_item(task_kind: str, task_id: str):
        normalized_kind = str(task_kind or "").strip()
        if normalized_kind == "generation":
            project_task_id = deps.parse_project_task_id(task_id)
            if project_task_id:
                return deps.get_project_backed_task_item_or_404(task_id)
            task = deps.get_generation_task_or_404(task_id)
            return deps.serialize_generation_task_center_item(task_id, task)
        if normalized_kind == "upload":
            publisher_manager = deps.get_publisher_manager()
            try:
                payload = publisher_manager.get_upload_job(task_id)
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from exc
            return deps.serialize_upload_task_center_item(payload)
        raise HTTPException(404, "任务类型不存在")

    def terminate_task(task_id: str) -> TaskMutationResponse:
        task = deps.get_generation_task_or_404(task_id)
        if not deps.task_is_terminable(task):
            raise HTTPException(400, "当前任务状态不支持终止")
        project_id = str(task.get("project_id", "") or "").strip()
        deps.update_task(
            task_id,
            cancel_requested=True,
            status="terminating",
            current_stage="terminating",
            message="已请求终止生成任务，系统会在下一个安全检查点停止。",
        )
        if project_id:
            try:
                with deps.get_session() as session:
                    parent = deps.latest_related_decision_event(
                        session,
                        project_id=project_id,
                        related_object_type="generation_task",
                        related_object_id=task_id,
                    )
                    deps.log_decision_event(
                        session,
                        project_id=project_id,
                        task_id=task_id,
                        scope="task",
                        event_family="audit_action",
                        event_type=DecisionEventType.TERMINATE_REQUESTED,
                        actor_type="manual_ui",
                        summary="已请求终止生成任务。",
                        related_object_type="generation_task",
                        related_object_id=task_id,
                        parent_event_id=str(parent.id if parent is not None else ""),
                        causal_root_id=str(parent.causal_root_id if parent is not None else ""),
                    )
                    session.commit()
            except OperationalError as exc:
                if not _is_retryable_db_error(exc):
                    raise
                logger.warning("Terminate audit event skipped because database is busy: %s", exc)
        updated = deps.get_generation_task_or_404(task_id)
        return TaskMutationResponse(
            ok=True,
            task_kind="generation",
            task_id=task_id,
            status=str(updated.get("status", "")),
            message=str(updated.get("message", "")),
        )

    def pause_task(task_id: str) -> TaskMutationResponse:
        task = deps.get_generation_task_or_404(task_id)
        if not deps.task_is_pausable(task):
            raise HTTPException(400, "当前任务状态不支持安全暂停")
        project_id = str(task.get("project_id", "") or "").strip()
        deps.update_task(
            task_id,
            pause_requested=True,
            message="已请求安全暂停，系统会在下一个安全检查点保存进度并暂停。",
        )
        if project_id:
            try:
                with deps.get_session() as session:
                    parent = deps.latest_related_decision_event(
                        session,
                        project_id=project_id,
                        related_object_type="generation_task",
                        related_object_id=task_id,
                    )
                    deps.log_decision_event(
                        session,
                        project_id=project_id,
                        task_id=task_id,
                        scope="task",
                        event_family="audit_action",
                        event_type=DecisionEventType.PAUSE_REQUESTED,
                        actor_type="manual_ui",
                        summary="已请求安全暂停生成任务。",
                        related_object_type="generation_task",
                        related_object_id=task_id,
                        parent_event_id=str(parent.id if parent is not None else ""),
                        causal_root_id=str(parent.causal_root_id if parent is not None else ""),
                    )
                    session.commit()
            except OperationalError as exc:
                if not _is_retryable_db_error(exc):
                    raise
                logger.warning("Pause audit event skipped because database is busy: %s", exc)
        updated = deps.get_generation_task_or_404(task_id)
        return TaskMutationResponse(
            ok=True,
            task_kind="generation",
            task_id=task_id,
            status=str(updated.get("status", "")),
            message=str(updated.get("message", "")),
        )

    def delete_task(task_id: str) -> TaskMutationResponse:
        task = deps.get_generation_task_or_404(task_id)
        if not deps.task_is_deletable(task):
            raise HTTPException(400, "只有终态任务可以删除")
        deps.update_task(task_id, deleted=True, message="任务已删除。")
        return TaskMutationResponse(
            ok=True,
            task_kind="generation",
            task_id=task_id,
            status=str(task.get("status", "")),
            message="任务已删除。",
        )

    def bulk_delete_tasks(req: TaskBulkDeleteRequest) -> BulkDeleteResponse:
        deleted_ids: list[str] = []
        skipped_ids: list[str] = []
        seen: set[str] = set()
        publisher_manager = deps.get_publisher_manager()

        for item in req.items:
            task_kind = str(item.task_kind or "").strip()
            task_id = str(item.task_id or "").strip()
            key = f"{task_kind}:{task_id}"
            if not task_kind or not task_id or key in seen:
                continue
            seen.add(key)
            if task_kind == "generation":
                try:
                    task = deps.get_generation_task_or_404(task_id)
                except HTTPException:
                    skipped_ids.append(key)
                    continue
                if not deps.task_is_deletable(task):
                    skipped_ids.append(key)
                    continue
                deps.update_task(task_id, deleted=True, message="任务已删除。")
                deleted_ids.append(key)
                continue
            if task_kind == "upload":
                try:
                    publisher_manager.delete_upload_job(task_id)
                except ValueError:
                    skipped_ids.append(key)
                    continue
                deleted_ids.append(key)
                continue
            skipped_ids.append(key)

        return BulkDeleteResponse(
            ok=True,
            deleted_count=len(deleted_ids),
            skipped_count=len(skipped_ids),
            deleted_ids=deleted_ids,
            skipped_ids=skipped_ids,
            message=f"已删除 {len(deleted_ids)} 条任务，跳过 {len(skipped_ids)} 条。",
        )

    return {
        "active_generation_task_check": active_generation_task_check,
        "get_task": get_task,
        "list_tasks": list_tasks,
        "list_task_center_items": list_task_center_items,
        "get_task_center_item": get_task_center_item,
        "terminate_task": terminate_task,
        "pause_task": pause_task,
        "delete_task": delete_task,
        "bulk_delete_tasks": bulk_delete_tasks,
    }
