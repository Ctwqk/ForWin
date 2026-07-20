from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from forwin.application.task_center import TaskCenterService
from forwin.config import InfrastructureConfig
from forwin.generation.pipeline import ChapterPipeline
from forwin.publisher_runtime.codex_intervention import build_codex_intervention_handler
from forwin.publishers import PublisherManager
from forwin.runtime.container import RuntimeContainer
from forwin.runtime.policy import RuntimePolicy

if TYPE_CHECKING:
    from forwin.application.project_control import ProjectControlApplicationService
    from forwin.application.projects import ProjectApplicationService
    from forwin.application.tasks import TaskApplicationService


logger = logging.getLogger(__name__)

GENERATION_TERMINAL_STATUSES = frozenset(
    {"completed", "partial_failed", "failed", "needs_review", "cancelled", "paused"}
)
GENERATION_TERMINAL_STAGE_BY_STATUS = {
    "completed": "completed",
    "partial_failed": "failed",
    "failed": "failed",
    "needs_review": "paused_for_review",
    "cancelled": "cancelled",
    "paused": "paused",
}
UPLOAD_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
GENERATION_STAGE_ORDER = (
    "queued",
    "planning_arc",
    "creating_project",
    "resolving_arc_envelope",
    "assembling_context",
    "writing_chapter",
    "chapter_failed",
    "continuity_review",
    "repairing_chapter",
    "repair_review",
    "applying_canon",
    "running_post_acceptance",
    "paused_for_review",
    "completed",
    "failed",
    "terminating",
    "cancelled",
)


@dataclass(slots=True)
class HttpRuntime:
    config: InfrastructureConfig | None = None
    engine: Engine | None = None
    session_factory: sessionmaker[Session] | None = None
    pipeline: ChapterPipeline | None = None
    container: RuntimeContainer | None = None
    publisher_manager: PublisherManager | None = None
    task_center_service: TaskCenterService | None = None
    task_application: TaskApplicationService | None = None
    project_control_application: ProjectControlApplicationService | None = None
    project_application: ProjectApplicationService | None = None
    automation_thread: threading.Thread | None = None
    automation_stop: threading.Event = field(default_factory=threading.Event)
    display_timezone: ZoneInfo = field(
        default_factory=lambda: ZoneInfo("America/Los_Angeles")
    )
    _pipeline_lock: threading.RLock = field(
        default_factory=threading.RLock,
        repr=False,
    )
    _publisher_lock: threading.RLock = field(
        default_factory=threading.RLock,
        repr=False,
    )
    def get_session(self) -> Session:
        if self.session_factory is None:
            raise RuntimeError("HTTP runtime session factory is unavailable")
        return self.session_factory()

    def build_genesis_service(self, *args, **kwargs):
        from forwin.http.request_support import _build_genesis_service

        return _build_genesis_service(self, *args, **kwargs)

    def close_genesis_service(self, service=None) -> None:
        from forwin.http.request_support import _close_genesis_service

        _close_genesis_service(self, service)

    def get_pipeline(self) -> ChapterPipeline:
        pipeline = self.pipeline
        if pipeline is not None:
            return pipeline
        with self._pipeline_lock:
            pipeline = self.pipeline
            if pipeline is not None:
                return pipeline
            if self.container is None:
                raise RuntimeError("HTTP runtime container is unavailable")
            if self.session_factory is None:
                raise RuntimeError("HTTP runtime session factory is unavailable")

            candidate = self.container.build_chapter_pipeline()
            with self.session_factory() as session:
                created = candidate.arc_envelope_manager.backfill_missing_resolutions(
                    session=session
                )
                if created:
                    session.commit()
                    logger.info("Backfilled %d active arc envelopes.", created)
                else:
                    session.rollback()
            self.pipeline = candidate
            return candidate

    def get_publisher_manager(self) -> PublisherManager:
        manager = self.publisher_manager
        if manager is not None:
            return manager
        with self._publisher_lock:
            manager = self.publisher_manager
            if manager is not None:
                return manager
            if self.config is None:
                raise RuntimeError("HTTP runtime config is unavailable")
            if self.session_factory is None:
                raise RuntimeError("HTTP runtime session factory is unavailable")

            candidate = PublisherManager(
                self.session_factory,
                extension_api_key=self.config.publisher_extension_api_key,
                preferred_client_id=self.config.publisher_preferred_client_id,
                strict_preferred_client=self.config.publisher_strict_preferred_client,
                publisher_session_secret=self.config.publisher_session_secret,
                publisher_session_encryption_required=(
                    self.config.publisher_session_encryption_required
                ),
                publisher_login_discord_webhook_url=(
                    self.config.publisher_login_discord_webhook_url
                ),
                codex_intervention_handler=build_codex_intervention_handler(
                    self.config
                ),
            )
            candidate.requeue_interrupted_upload_jobs()
            self.publisher_manager = candidate
            return candidate

    def startup(self) -> None:
        from forwin.http.automation import start_automation_scheduler

        if self.config is None:
            self.config = InfrastructureConfig.from_env()
        database_url = os.environ.get("FORWIN_DATABASE_URL", self.config.database_url)
        if database_url != self.config.database_url:
            self.config = self.config.model_copy(update={"database_url": database_url})
        if self.container is None:
            self.container = RuntimeContainer.for_api(
                self.config,
                policy=RuntimePolicy.for_profile("standard"),
            )
        core = self.container.core_services()
        if self.engine is None:
            self.engine = core.engine
        if self.session_factory is None:
            self.session_factory = core.session_factory
        start_automation_scheduler(self)

    def shutdown(self) -> None:
        from forwin.http.automation import stop_automation_scheduler

        stop_automation_scheduler(self)
        container = self.container
        if container is not None:
            container.close()
        else:
            pipeline_client = getattr(self.pipeline, "llm_client", None)
            if pipeline_client is not None:
                try:
                    pipeline_client.close()
                except Exception:  # noqa: BLE001
                    logger.debug("Ignoring pipeline LLM shutdown error.", exc_info=True)
            if self.engine is not None:
                try:
                    self.engine.dispose()
                except Exception:  # noqa: BLE001
                    logger.debug("Ignoring HTTP engine shutdown error.", exc_info=True)
        self.pipeline = None
        self.container = None
        self.publisher_manager = None
        self.task_center_service = None
        self.session_factory = None
        self.engine = None


__all__ = [
    "GENERATION_STAGE_ORDER",
    "GENERATION_TERMINAL_STAGE_BY_STATUS",
    "GENERATION_TERMINAL_STATUSES",
    "HttpRuntime",
    "UPLOAD_TERMINAL_STATUSES",
]
