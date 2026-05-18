from __future__ import annotations

import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from forwin.api_task_center_service import TaskCenterService
from forwin.config import Config
from forwin.orchestrator.loop import WritingOrchestrator
from forwin.publishers import PublisherManager
from forwin.runtime.container import RuntimeContainer
from forwin.runtime_settings import RuntimeSettingsStore

_config: Config | None = None
_engine = None
_SessionFactory = None
_orchestrator: WritingOrchestrator | None = None
_runtime_container: RuntimeContainer | None = None
_publisher_manager: PublisherManager | None = None
_runtime_settings: RuntimeSettingsStore | None = None
_task_center_service: TaskCenterService | None = None
_automation_scheduler_thread: threading.Thread | None = None
_automation_scheduler_stop = threading.Event()

# Runtime cache for live generation threads. Persistent task state is stored in DB.
_tasks: dict[str, dict] = {}
_tasks_lock = threading.Lock()
_TASK_RETENTION_SECONDS = 6 * 60 * 60
_MAX_TASKS = 256
_TASK_DB_PRUNE_INTERVAL_SECONDS = 60
_DISPLAY_TZ = ZoneInfo("America/Los_Angeles")
_last_generation_task_db_prune_at: datetime | None = None
_GENERATION_TERMINAL_STATUSES = {"completed", "partial_failed", "failed", "needs_review", "cancelled", "paused"}
_GENERATION_TERMINAL_STAGE_BY_STATUS = {
    "completed": "completed",
    "partial_failed": "failed",
    "failed": "failed",
    "needs_review": "paused_for_review",
    "cancelled": "cancelled",
    "paused": "paused",
}
_UPLOAD_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
_GENERATION_STAGE_ORDER = [
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
]

__all__ = [name for name in globals() if not name.startswith("__")]
