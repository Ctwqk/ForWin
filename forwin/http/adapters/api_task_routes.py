from __future__ import annotations

from collections.abc import Callable
from typing import Any

from forwin.application.tasks import TaskApplicationService


def build_handlers(
    *,
    service: TaskApplicationService,
) -> dict[str, Callable[..., Any]]:
    return {
        "active_generation_task_check": service.active_generation_task_check,
        "get_task": service.get_task,
        "list_tasks": service.list_tasks,
        "list_task_center_items": service.list_task_center_items,
        "get_task_center_item": service.get_task_center_item,
        "terminate_task": service.terminate_task,
        "pause_task": service.pause_task,
        "delete_task": service.delete_task,
        "bulk_delete_tasks": service.bulk_delete_tasks,
    }


__all__ = ["build_handlers"]
