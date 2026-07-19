from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import HTTPException

from forwin.api_schema import (
    ActiveGenerationTaskCheckResponse,
    BulkDeleteResponse,
    TaskBulkDeleteRequest,
    TaskMutationResponse,
)


@dataclass(frozen=True)
class TaskApplicationDeps:
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
    mutate_generation_task: Callable[[str, str], TaskMutationResponse]
    active_generation_task_ids: Callable[[str], list[str]] | None = None


def _build_operations(
    deps: TaskApplicationDeps,
) -> dict[str, Callable[..., Any]]:
    def active_generation_task_check(
        project_id: str = "",
    ) -> ActiveGenerationTaskCheckResponse:
        normalized_project_id = str(project_id or "").strip()
        if deps.active_generation_task_ids is not None:
            active_ids = deps.active_generation_task_ids(normalized_project_id)
        else:
            active_ids = []
            for task_id, task in deps.list_generation_tasks(200):
                if str(task.get("task_kind", "generation")) != "generation":
                    continue
                if task.get("deleted"):
                    continue
                if (
                    normalized_project_id
                    and str(task.get("project_id", "") or "").strip()
                    != normalized_project_id
                ):
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
        return [
            deps.serialize_task(task_id, task)
            for task_id, task in deps.list_generation_tasks(limit)
        ]

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
        return deps.mutate_generation_task(task_id, "terminate")

    def pause_task(task_id: str) -> TaskMutationResponse:
        return deps.mutate_generation_task(task_id, "pause")

    def delete_task(task_id: str) -> TaskMutationResponse:
        return deps.mutate_generation_task(task_id, "delete")

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
                    deps.mutate_generation_task(task_id, "delete")
                except HTTPException:
                    skipped_ids.append(key)
                    continue
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


class TaskApplicationService:
    def __init__(self, deps: TaskApplicationDeps) -> None:
        self._operations = _build_operations(deps)

    def active_generation_task_check(
        self,
        project_id: str = "",
    ) -> ActiveGenerationTaskCheckResponse:
        return self._operations["active_generation_task_check"](project_id)

    def get_task(self, task_id: str):
        return self._operations["get_task"](task_id)

    def list_tasks(self, limit: int = 30):
        return self._operations["list_tasks"](limit)

    def list_task_center_items(self, limit: int = 50):
        return self._operations["list_task_center_items"](limit)

    def get_task_center_item(self, task_kind: str, task_id: str):
        return self._operations["get_task_center_item"](task_kind, task_id)

    def terminate_task(self, task_id: str) -> TaskMutationResponse:
        return self._operations["terminate_task"](task_id)

    def pause_task(self, task_id: str) -> TaskMutationResponse:
        return self._operations["pause_task"](task_id)

    def delete_task(self, task_id: str) -> TaskMutationResponse:
        return self._operations["delete_task"](task_id)

    def bulk_delete_tasks(self, req: TaskBulkDeleteRequest) -> BulkDeleteResponse:
        return self._operations["bulk_delete_tasks"](req)


__all__ = ["TaskApplicationDeps", "TaskApplicationService"]
