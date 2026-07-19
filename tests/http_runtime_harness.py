from __future__ import annotations

from forwin.models.task import GenerationTask
from forwin.http import HttpRuntime, create_app
from forwin.http.generation import (
    _create_continue_generation_task,
    _project_has_active_generation_task,
)
from forwin.http.project_support import (
    _get_generation_task_or_404,
    _update_task,
)
from forwin.http.tasks import (
    _apply_generation_task_to_row,
    _coerce_task_datetime,
    _list_generation_tasks,
    _new_stage_history_entry,
    _serialize_generation_task_center_item,
    _task_is_pausable,
)
from forwin.http.request_support import _utcnow


class HttpRuntimeHarness:
    _RUNTIME_ATTRS = {
        "_SessionFactory": "session_factory",
        "_config": "config",
        "_engine": "engine",
        "_runtime_container": "container",
        "_pipeline": "pipeline",
    }

    def __init__(
        self,
        *,
        session_factory=None,
        config=None,
        engine=None,
        pipeline=None,
    ) -> None:
        object.__setattr__(
            self,
            "runtime",
            HttpRuntime(
                config=config,
                engine=engine,
                session_factory=session_factory,
                pipeline=pipeline,
            ),
        )
        app = create_app(self.runtime)
        object.__setattr__(self, "app", app)
        object.__setattr__(self, "handlers", app.state.forwin_handlers)
        object.__setattr__(
            self,
            "endpoints",
            {
                endpoint.__name__: endpoint
                for route in app.routes
                if (endpoint := getattr(route, "endpoint", None)) is not None
                and getattr(endpoint, "__name__", "")
            },
        )

    def __getattr__(self, name):
        runtime_name = self._RUNTIME_ATTRS.get(name)
        if runtime_name:
            return getattr(self.runtime, runtime_name)
        if name in self.handlers:
            return self.handlers[name]
        if name in self.endpoints:
            return self.endpoints[name]
        raise AttributeError(name)

    def __setattr__(self, name, value) -> None:
        runtime_name = self._RUNTIME_ATTRS.get(name)
        if runtime_name:
            setattr(self.runtime, runtime_name, value)
            return
        object.__setattr__(self, name, value)

    def _build_genesis_service(self, *args, **kwargs):
        return self.runtime.build_genesis_service(*args, **kwargs)

    def _get_session(self):
        return self.runtime.get_session()

    def _close_genesis_service(self, service=None) -> None:
        self.runtime.close_genesis_service(service)

    def _create_task_record(self, *args, **kwargs):
        message = str(kwargs.get("message", args[0] if args else "") or "")
        now = _utcnow()
        return {
            "task_kind": str(kwargs.get("task_kind", "generation") or "generation"),
            "status": "queued",
            "title": str(kwargs.get("title", "") or ""),
            "subtitle": str(kwargs.get("subtitle", "") or ""),
            "project_id": None,
            "extension_client_id": "",
            "error": None,
            "message": message,
            "current_stage": "queued",
            "stage_history": [
                _new_stage_history_entry("queued", now=now, message=message)
            ],
            "requested_chapters": int(kwargs.get("requested_chapters", 0) or 0),
            "current_chapter": 0,
            "completed_chapters": [],
            "failed_chapters": [],
            "paused_chapters": [],
            "frozen_artifacts": [],
            "cancel_requested": False,
            "pause_requested": False,
            "lease_owner": "",
            "lease_expires_at": None,
            "heartbeat_at": None,
            "resume_from_chapter": 0,
            "run_until_chapter": 0,
            "max_chapters": 0,
            "execution_payload": {},
            "deleted": False,
            "persistence_degraded": False,
            "persistence_error": None,
            "created_at": now,
            "updated_at": now,
        }

    def _persist_generation_task(self, task_id, task) -> None:
        with self.runtime.get_session() as session:
            row = session.get(GenerationTask, task_id) or GenerationTask(id=task_id)
            _apply_generation_task_to_row(row, task)
            session.add(row)
            session.commit()

    def _get_generation_task_or_404(self, task_id):
        return _get_generation_task_or_404(self.runtime, task_id)

    def _list_generation_tasks(self, limit):
        return _list_generation_tasks(self.runtime, limit)

    def _create_continue_generation_task(self, **kwargs):
        return _create_continue_generation_task(self.runtime, **kwargs)

    @staticmethod
    def _serialize_generation_task_center_item(task_id, task):
        return _serialize_generation_task_center_item(task_id, task)

    def _update_task(self, task_id, **changes) -> None:
        _update_task(self.runtime, task_id, **changes)

    @staticmethod
    def _coerce_task_datetime(value):
        return _coerce_task_datetime(value)

    @staticmethod
    def _task_is_pausable(task):
        return _task_is_pausable(task)

    def _project_has_active_generation_task(self, project_id, *, session=None):
        return _project_has_active_generation_task(
            self.runtime,
            project_id,
            session=session,
        )

    @staticmethod
    def _utcnow():
        return _utcnow()


__all__ = ["HttpRuntimeHarness"]
