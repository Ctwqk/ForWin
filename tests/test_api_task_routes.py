from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy.exc import OperationalError

from forwin.api_task_routes import build_handlers


def test_terminate_task_marks_cancel_even_when_audit_log_is_locked() -> None:
    tasks = {
        "task-1": {
            "task_kind": "generation",
            "status": "running",
            "project_id": "project-1",
            "cancel_requested": False,
        }
    }
    updates: list[dict] = []
    locked = OperationalError(
        "INSERT INTO decision_events",
        {},
        Exception("database is locked"),
    )

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def commit(self):
            raise AssertionError("commit should not be reached when audit logging is locked")

    def update_task(task_id: str, **kwargs) -> None:
        updates.append({"task_id": task_id, **kwargs})
        tasks[task_id].update(kwargs)

    handlers = build_handlers(
        get_session=lambda: Session(),
        get_publisher_manager=lambda: SimpleNamespace(list_upload_jobs=lambda **kwargs: []),
        list_generation_tasks=lambda limit: list(tasks.items()),
        serialize_task=lambda task_id, task: task,
        get_generation_task_or_404=lambda task_id: tasks[task_id],
        serialize_generation_task_center_item=lambda task_id, task: task,
        serialize_upload_task_center_item=lambda payload: payload,
        list_project_backed_task_items=lambda limit: [],
        parse_project_task_id=lambda task_id: None,
        get_project_backed_task_item_or_404=lambda task_id: None,
        task_is_terminal=lambda status: status in {"completed", "failed", "cancelled"},
        task_is_terminable=lambda task: not task.get("cancel_requested") and task.get("status") == "running",
        task_is_pausable=lambda task: False,
        task_is_deletable=lambda task: False,
        latest_related_decision_event=lambda *args, **kwargs: None,
        log_decision_event=lambda *args, **kwargs: (_ for _ in ()).throw(locked),
        update_task=update_task,
    )

    response = handlers["terminate_task"]("task-1")

    assert response.ok is True
    assert response.status == "terminating"
    assert updates
    assert tasks["task-1"]["cancel_requested"] is True
