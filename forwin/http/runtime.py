from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from forwin.application.task_center import TaskCenterService
from forwin.config import InfrastructureConfig
from forwin.generation.pipeline import ChapterPipeline
from forwin.models.base import get_session_factory
from forwin.publisher_runtime.codex_intervention import build_codex_intervention_handler
from forwin.publishers import PublisherManager
from forwin.runtime.container import RuntimeContainer
from forwin.runtime.policy import RuntimePolicy
from forwin.runtime.services import RuntimeServices

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
    "running_scenario_rehearsal",
    "scenario_rehearsal_patch_required",
    "scenario_rehearsal_blocked",
    "running_provisional_preview",
    "provisional_failed",
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
    services: RuntimeServices | None = None
    publisher_manager: PublisherManager | None = None
    task_center_service: TaskCenterService | None = None
    task_application: TaskApplicationService | None = None
    project_control_application: ProjectControlApplicationService | None = None
    project_application: ProjectApplicationService | None = None
    tasks: dict[str, dict] = field(default_factory=dict)
    tasks_lock: threading.Lock = field(default_factory=threading.Lock)
    automation_thread: threading.Thread | None = None
    automation_stop: threading.Event = field(default_factory=threading.Event)
    last_generation_task_db_prune_at: datetime | None = None
    task_retention_seconds: int = 6 * 60 * 60
    max_tasks: int = 256
    task_db_prune_interval_seconds: int = 60
    display_timezone: ZoneInfo = field(
        default_factory=lambda: ZoneInfo("America/Los_Angeles")
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

    def startup(self) -> None:
        from forwin.http.automation import start_automation_scheduler
        from forwin.http.tasks import recover_interrupted_generation_tasks

        if self.config is None:
            self.config = InfrastructureConfig.from_env()
        database_url = os.environ.get("FORWIN_DATABASE_URL", self.config.database_url)
        if database_url != self.config.database_url:
            self.config = self.config.model_copy(update={"database_url": database_url})
        if self.container is None:
            self.container = RuntimeContainer.from_config(
                self.config,
                policy=RuntimePolicy.for_profile("standard"),
                role="api",
            )
        if self.services is None:
            self.services = self.container.services()
        if self.engine is None:
            self.engine = self.services.engine
        if self.session_factory is None:
            self.session_factory = self.services.session_factory
        if self.session_factory is None and self.engine is not None:
            self.session_factory = get_session_factory(self.engine)
        if self.pipeline is None:
            self.pipeline = self.container.build_chapter_pipeline()
            with self.session_factory() as session:
                created = self.pipeline.arc_envelope_manager.backfill_missing_resolutions(
                    session=session
                )
                if created:
                    session.commit()
                    logger.info("Backfilled %d active arc envelopes.", created)
                else:
                    session.rollback()
        recovered = recover_interrupted_generation_tasks(self)
        if recovered:
            logger.info("Recovered %d interrupted generation tasks.", len(recovered))
        if self.publisher_manager is None:
            self.publisher_manager = PublisherManager(
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
                codex_intervention_handler=build_codex_intervention_handler(self.config),
            )
        self.publisher_manager.requeue_interrupted_upload_jobs()
        start_automation_scheduler(self)

    def shutdown(self) -> None:
        from forwin.http.automation import stop_automation_scheduler

        stop_automation_scheduler(self)
        pipeline = self.pipeline
        pipeline_client = getattr(pipeline, "llm_client", None)
        if pipeline is not None:
            try:
                pipeline_client.close()
            except Exception:  # noqa: BLE001
                logger.debug("Ignoring pipeline LLM shutdown error.", exc_info=True)
        services = self.services
        if services is not None and services.llm_client is not pipeline_client:
            try:
                services.llm_client.close()
            except Exception:  # noqa: BLE001
                logger.debug("Ignoring runtime LLM shutdown error.", exc_info=True)
        engine = self.engine
        if engine is not None:
            try:
                engine.dispose()
            except Exception:  # noqa: BLE001
                logger.debug("Ignoring HTTP engine shutdown error.", exc_info=True)
        self.pipeline = None
        self.container = None
        self.services = None
        self.publisher_manager = None
        self.task_center_service = None
        self.session_factory = None
        self.engine = None
        self.last_generation_task_db_prune_at = None
        with self.tasks_lock:
            self.tasks.clear()


__all__ = [
    "GENERATION_STAGE_ORDER",
    "GENERATION_TERMINAL_STAGE_BY_STATUS",
    "GENERATION_TERMINAL_STATUSES",
    "HttpRuntime",
    "UPLOAD_TERMINAL_STATUSES",
]
