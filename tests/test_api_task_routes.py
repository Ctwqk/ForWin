from __future__ import annotations

from types import SimpleNamespace

import pytest

from forwin.application.tasks import TaskApplicationDeps, TaskApplicationService
from forwin.api_schema import TaskMutationResponse
from forwin.http.adapters.api_task_routes import build_handlers


def test_build_handlers_rejects_flat_dependency_kwargs() -> None:
    with pytest.raises(TypeError):
        build_handlers(get_session=lambda: None)


def test_terminate_task_delegates_to_atomic_generation_mutation() -> None:
    tasks = {
        "task-1": {
            "task_kind": "generation",
            "status": "running",
            "project_id": "project-1",
            "cancel_requested": False,
        }
    }
    mutations: list[tuple[str, str]] = []

    def mutate_generation_task(task_id: str, action: str) -> TaskMutationResponse:
        mutations.append((task_id, action))
        return TaskMutationResponse(
            ok=True,
            task_kind="generation",
            task_id=task_id,
            status="terminating",
            message="已请求终止生成任务，系统会在下一个安全检查点停止。",
        )

    handlers = build_handlers(
        service=TaskApplicationService(
            TaskApplicationDeps(
                get_publisher_manager=lambda: SimpleNamespace(
                    list_upload_jobs=lambda **kwargs: []
                ),
                list_generation_tasks=lambda limit: list(tasks.items()),
                serialize_task=lambda task_id, task: task,
                get_generation_task_or_404=lambda task_id: tasks[task_id],
                serialize_generation_task_center_item=lambda task_id, task: task,
                serialize_upload_task_center_item=lambda payload: payload,
                list_project_backed_task_items=lambda limit: [],
                parse_project_task_id=lambda task_id: None,
                get_project_backed_task_item_or_404=lambda task_id: None,
                task_is_terminal=lambda status: status
                in {"completed", "failed", "cancelled"},
                mutate_generation_task=mutate_generation_task,
            )
        )
    )

    response = handlers["terminate_task"]("task-1")

    assert response.ok is True
    assert response.status == "terminating"
    assert mutations == [("task-1", "terminate")]
