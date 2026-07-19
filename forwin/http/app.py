from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from forwin.api_auth import basic_auth_enabled, make_basic_auth_middleware
from forwin.api_pages import render_home_page, render_publishers_page
from forwin.application.errors import ActiveGenerationTaskError
from forwin.application.projects import (
    ProjectApplicationDeps,
    ProjectApplicationService,
)
from forwin.application.project_control import (
    ProjectControlApplicationDeps,
    ProjectControlApplicationService,
)
from forwin.application.tasks import TaskApplicationDeps, TaskApplicationService
from forwin.http.adapters import api_observability_routes
from forwin.http.generation import (
    _active_generation_task_ids,
    _create_continue_generation_task,
    _project_delete_blockers,
    _project_delete_conflict_message,
    _project_has_active_generation_task,
)
from forwin.http.project_support import (
    _build_audit_insights,
    _build_causal_replay,
    _decision_refs_for_chapter_review,
    _delete_project,
    _get_generation_task_or_404,
    _latest_band_checkpoint_row,
    _latest_related_decision_event,
    _list_decision_event_rows,
    _log_decision_event,
    _persist_project_automation,
    _require_reason,
    _serialize_band_checkpoint,
    _serialize_constraint,
    _serialize_decision_event,
    _update_task,
    _validate_constraint_payload,
)
from forwin.http.request_support import (
    _active_genesis_revision,
    _display_datetime,
    _genesis_patch_payload,
    _get_session,
    _json_load_list,
    _json_load_object,
    _require_genesis_project,
)
from forwin.http.routes import (
    ApiRouteDeps,
    CoreDeps,
    ObservabilityDeps,
    ProjectControlDeps,
    ProjectDeps,
    PublisherDeps,
    TaskDeps,
    register_api_routes,
)
from forwin.http.runtime import HttpRuntime
from forwin.http.tasks import (
    _generation_task_conflict_message,
    _get_project_backed_task_item_or_404,
    _list_generation_tasks,
    _list_project_backed_task_items,
    _parse_project_task_id,
    _serialize_generation_task_center_item,
    _serialize_task,
    _serialize_upload_task_center_item,
    _task_is_deletable,
    _task_is_pausable,
    _task_is_terminal,
    _task_is_terminable,
)

logger = logging.getLogger(__name__)


def _display_for(runtime: HttpRuntime, value) -> str:
    return _display_datetime(value, display_timezone=runtime.display_timezone)


def _build_task_application(runtime: HttpRuntime) -> TaskApplicationService:
    return TaskApplicationService(
        TaskApplicationDeps(
            get_session=lambda: _get_session(runtime),
            get_publisher_manager=lambda: runtime.publisher_manager,
            list_generation_tasks=lambda limit: _list_generation_tasks(
                runtime, limit
            ),
            serialize_task=_serialize_task,
            get_generation_task_or_404=lambda task_id: (
                _get_generation_task_or_404(runtime, task_id)
            ),
            serialize_generation_task_center_item=(
                _serialize_generation_task_center_item
            ),
            serialize_upload_task_center_item=_serialize_upload_task_center_item,
            list_project_backed_task_items=lambda limit: (
                _list_project_backed_task_items(runtime, limit)
            ),
            parse_project_task_id=lambda task_id: _parse_project_task_id(
                runtime, task_id
            ),
            get_project_backed_task_item_or_404=lambda task_id: (
                _get_project_backed_task_item_or_404(runtime, task_id)
            ),
            task_is_terminal=_task_is_terminal,
            task_is_terminable=_task_is_terminable,
            task_is_pausable=_task_is_pausable,
            task_is_deletable=_task_is_deletable,
            latest_related_decision_event=_latest_related_decision_event,
            log_decision_event=_log_decision_event,
            update_task=lambda task_id, **changes: _update_task(
                runtime, task_id, **changes
            ),
            active_generation_task_ids=lambda project_id="": (
                _active_generation_task_ids(runtime, project_id)
            ),
        )
    )


def _build_project_application(runtime: HttpRuntime) -> ProjectApplicationService:
    return ProjectApplicationService(
        ProjectApplicationDeps(
            get_session=lambda: _get_session(runtime),
            get_config=lambda: runtime.config,
            get_pipeline=lambda: runtime.pipeline,
            get_publisher_manager=lambda: runtime.publisher_manager,
            display_datetime=lambda value: _display_for(runtime, value),
            build_genesis_service=runtime.build_genesis_service,
            close_genesis_service=runtime.close_genesis_service,
            require_genesis_project=_require_genesis_project,
            active_genesis_revision=_active_genesis_revision,
            genesis_patch_payload=_genesis_patch_payload,
            delete_project_impl=_delete_project,
            project_delete_blockers=lambda project_id, *, session: (
                _project_delete_blockers(runtime, project_id, session=session)
            ),
            project_delete_conflict_message=_project_delete_conflict_message,
            project_has_active_generation_task=lambda project_id, *, session=None: (
                _project_has_active_generation_task(
                    runtime, project_id, session=session
                )
            ),
            generation_task_conflict_message=_generation_task_conflict_message,
            create_continue_generation_task=lambda **kwargs: (
                _create_continue_generation_task(runtime, **kwargs)
            ),
            persist_project_automation=_persist_project_automation,
            log_decision_event=_log_decision_event,
            serialize_task=_serialize_task,
            get_generation_task_or_404=lambda task_id: (
                _get_generation_task_or_404(runtime, task_id)
            ),
            active_generation_task_error_cls=ActiveGenerationTaskError,
            require_reason=_require_reason,
            decision_refs_for_chapter_review=_decision_refs_for_chapter_review,
            update_task=lambda task_id, **changes: _update_task(
                runtime, task_id, **changes
            ),
        )
    )


def _build_project_control_application(
    runtime: HttpRuntime,
) -> ProjectControlApplicationService:
    return ProjectControlApplicationService(
        ProjectControlApplicationDeps(
            get_session=lambda: _get_session(runtime),
            get_pipeline=lambda: runtime.pipeline,
            display_datetime=lambda value: _display_for(runtime, value),
            require_reason=_require_reason,
            validate_constraint_payload=_validate_constraint_payload,
            serialize_band_checkpoint=_serialize_band_checkpoint,
            serialize_constraint=_serialize_constraint,
            list_decision_event_rows=_list_decision_event_rows,
            serialize_decision_event=_serialize_decision_event,
            build_causal_replay=_build_causal_replay,
            build_audit_insights=_build_audit_insights,
            latest_band_checkpoint_row=_latest_band_checkpoint_row,
            latest_related_decision_event=_latest_related_decision_event,
            log_decision_event=_log_decision_event,
            json_load_object=_json_load_object,
        )
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime: HttpRuntime = app.state.forwin_runtime
    runtime.startup()
    config = runtime.config
    if config is not None and str(config.http_bind or "").strip() in {
        "0.0.0.0",
        "::",
    } and not basic_auth_enabled(config):
        logger.warning(
            "ForWin is reachable beyond localhost and HTTP Basic Auth is disabled. "
            "This is acceptable only on a trusted LAN."
        )
    if runtime.engine is not None:
        logger.info(
            "ForWin API started. DB: %s",
            runtime.engine.url.render_as_string(hide_password=True),
        )
    try:
        yield
    finally:
        logger.info("ForWin API shutting down.")
        runtime.shutdown()


def create_app(runtime: HttpRuntime | None = None) -> FastAPI:
    runtime = runtime or HttpRuntime()
    app = FastAPI(
        title="ForWin – 长篇中文网文生成系统",
        version="0.5.0",
        lifespan=lifespan,
    )
    app.state.forwin_runtime = runtime
    runtime.task_application = _build_task_application(runtime)
    runtime.project_control_application = _build_project_control_application(runtime)
    runtime.project_application = _build_project_application(runtime)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def optional_basic_auth(request: Request, call_next):
        config = runtime.config
        if config is None or not basic_auth_enabled(config):
            return await call_next(request)
        return await make_basic_auth_middleware(config)(request, call_next)

    observability_handlers = api_observability_routes.build_handlers(
        get_config=lambda: runtime.config,
        get_session=lambda: _get_session(runtime),
        list_decision_event_rows=_list_decision_event_rows,
        serialize_decision_event=_serialize_decision_event,
        display_datetime=lambda value: _display_for(runtime, value),
        json_load_object=_json_load_object,
        json_load_list=_json_load_list,
    )

    def current_memory_index():
        broker = getattr(runtime.pipeline, "retrieval_broker", None)
        return getattr(broker, "memory_index", None)

    handlers = register_api_routes(
        app,
        deps=ApiRouteDeps(
            core=CoreDeps(
                get_config=lambda: runtime.config,
                get_session=lambda: _get_session(runtime),
                render_home_page=render_home_page,
                get_memory_index=current_memory_index,
            ),
            task=TaskDeps(
                service=runtime.task_application,
                get_task_timeline=observability_handlers["get_task_timeline"],
            ),
            project=ProjectDeps(service=runtime.project_application),
            project_control=ProjectControlDeps(
                service=runtime.project_control_application
            ),
            observability=ObservabilityDeps(
                get_chapter_observability_ledger=observability_handlers[
                    "get_chapter_observability_ledger"
                ],
                get_prompt_trace_detail=observability_handlers[
                    "get_prompt_trace_detail"
                ],
                read_artifact_preview=observability_handlers[
                    "read_artifact_preview"
                ],
                get_task_performance_report=observability_handlers[
                    "get_task_performance_report"
                ],
                get_project_performance_report=observability_handlers[
                    "get_project_performance_report"
                ],
                get_chapter_performance_report=observability_handlers[
                    "get_chapter_performance_report"
                ],
                get_slow_performance_spans=observability_handlers[
                    "get_slow_performance_spans"
                ],
                get_llm_performance_report=observability_handlers[
                    "get_llm_performance_report"
                ],
                get_db_performance_report=observability_handlers[
                    "get_db_performance_report"
                ],
            ),
            publisher=PublisherDeps(
                get_publisher_manager=lambda: runtime.publisher_manager,
                render_publishers_page=render_publishers_page,
            ),
        ),
    )
    app.state.forwin_handlers = handlers
    return app


__all__ = ["create_app", "lifespan"]
