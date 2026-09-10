"""The two repair stage notifications and its separate pause predicate."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from forwin.observability.pipeline_progress import PipelineProgressRecorder

logger = logging.getLogger(__name__)
RepairStage = Literal["repairing_chapter", "repair_review"]


@dataclass(frozen=True, slots=True)
class RepairControl:
    progress: PipelineProgressRecorder
    should_pause: Callable[[], bool] | None = None

    def paused(self) -> bool:
        try:
            return bool(self.should_pause and self.should_pause())
        except Exception:
            logger.debug("Ignoring pause predicate failure.", exc_info=True)
            return False

    def notify(
        self, stage: RepairStage, *, project_id: str, chapter_number: int
    ) -> None:
        self.progress.emit(
            "stage_changed",
            stage=stage,
            project_id=project_id,
            current_chapter=chapter_number,
        )
