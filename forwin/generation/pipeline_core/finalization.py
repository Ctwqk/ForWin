from __future__ import annotations

import json

from forwin.generation.pipeline_core.common import logger
from forwin.generation.pipeline_core.result import RunResult
from forwin.models.draft import ChapterReview
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput


def _load_review_verdict(review: ChapterReview) -> ReviewVerdict:
    meta = (
        json.loads(review.review_meta_json or "{}")
        if getattr(review, "review_meta_json", "")
        else {}
    )
    if not isinstance(meta, dict):
        meta = {}
    return ReviewVerdict.model_validate(
        {
            "verdict": review.verdict,
            "issues": json.loads(review.issues_json or "[]"),
            **meta,
        }
    )


class FinalizationStage:
    """Owns the finalization stage behavior."""

    def _abort_requested(self) -> bool:
        try:
            return bool(self.should_abort and self.should_abort())
        except Exception:  # noqa: BLE001
            logger.debug("Ignoring abort predicate failure.", exc_info=True)
            return False

    def _pause_requested(self) -> bool:
        try:
            return bool(self.should_pause and self.should_pause())
        except Exception:  # noqa: BLE001
            logger.debug("Ignoring pause predicate failure.", exc_info=True)
            return False

    def _paused_result(
        self,
        project_id: str,
        requested_chapters: int,
        *,
        completed_chapters: list[int] | None = None,
        failed_chapters: list[int] | None = None,
        paused_chapters: list[int] | None = None,
        frozen_artifacts: list[str] | None = None,
        current_chapter: int = 0,
    ) -> RunResult:
        self._emit_progress(
            "stage_changed",
            stage="paused",
            project_id=project_id,
            requested_chapters=requested_chapters,
            current_chapter=current_chapter,
            completed_chapters=completed_chapters or [],
            failed_chapters=failed_chapters or [],
            paused_chapters=paused_chapters or [],
            frozen_artifacts=frozen_artifacts or [],
        )
        return RunResult(
            project_id=project_id,
            requested_chapters=requested_chapters,
            completed_chapters=list(completed_chapters or []),
            failed_chapters=list(failed_chapters or []),
            paused_chapters=list(paused_chapters or []),
            frozen_artifacts=list(frozen_artifacts or []),
            paused=True,
        )

    def _cancelled_result(
        self,
        project_id: str,
        requested_chapters: int,
        *,
        completed_chapters: list[int] | None = None,
        failed_chapters: list[int] | None = None,
        paused_chapters: list[int] | None = None,
        frozen_artifacts: list[str] | None = None,
        current_chapter: int = 0,
    ) -> RunResult:
        self._emit_progress(
            "stage_changed",
            stage="cancelled",
            project_id=project_id,
            requested_chapters=requested_chapters,
            current_chapter=current_chapter,
            completed_chapters=completed_chapters or [],
            failed_chapters=failed_chapters or [],
            paused_chapters=paused_chapters or [],
            frozen_artifacts=frozen_artifacts or [],
        )
        return RunResult(
            project_id=project_id,
            requested_chapters=requested_chapters,
            completed_chapters=list(completed_chapters or []),
            failed_chapters=list(failed_chapters or []),
            paused_chapters=list(paused_chapters or []),
            frozen_artifacts=list(frozen_artifacts or []),
            cancelled=True,
        )

    def _load_writer_output_from_meta(self, meta_path: str) -> WriterOutput:
        payload = self.artifact_store.read_json(meta_path)
        return WriterOutput.model_validate(payload)


    @staticmethod
    def _load_review_verdict(review: ChapterReview) -> ReviewVerdict:
        return _load_review_verdict(review)


__all__ = ["FinalizationStage"]
