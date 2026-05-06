from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from forwin.models import ChapterPlan, ProvisionalBandExecution, new_id

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ProvisionalBandPreview:
    band_id: str
    artifact_path: str
    aggregate_verdict: str
    preview_chapter_count: int
    total_char_count: int
    issue_count: int
    failure_count: int
    chapter_numbers: list[int]
    summary_lines: list[str]


class ProvisionalPreviewService:
    def __init__(
        self,
        *,
        provisional_executor: Any | None = None,
        legacy_preview_enabled: bool = False,
    ) -> None:
        self.provisional_executor = provisional_executor
        self.legacy_preview_enabled = legacy_preview_enabled

    def execute(
        self,
        *,
        session: Session,
        project_id: str,
        arc_id: str,
        band_id: str,
        chapter_plans: list[ChapterPlan],
    ) -> ProvisionalBandPreview | None:
        if not self.legacy_preview_enabled:
            return None
        if self.provisional_executor is None or not chapter_plans:
            return None
        try:
            preview = self.provisional_executor(
                session=session,
                project_id=project_id,
                arc_id=arc_id,
                band_id=band_id,
                chapter_plans=chapter_plans,
            )
        except Exception:  # noqa: BLE001
            logger.warning("Provisional band execution failed for %s/%s.", project_id, band_id, exc_info=True)
            return None
        return self.coerce_preview(preview=preview, fallback_band_id=band_id)

    def persist_execution(
        self,
        *,
        session: Session,
        project_id: str,
        arc_id: str,
        preview: ProvisionalBandPreview | None,
    ) -> None:
        if preview is None:
            return
        session.add(
            ProvisionalBandExecution(
                id=new_id(),
                project_id=project_id,
                arc_id=arc_id,
                band_id=preview.band_id,
                chapter_numbers_json=json.dumps(preview.chapter_numbers, ensure_ascii=False),
                artifact_path=preview.artifact_path,
                aggregate_verdict=preview.aggregate_verdict,
                preview_char_count=preview.total_char_count,
                issue_count=preview.issue_count,
                failure_count=preview.failure_count,
            )
        )

    @staticmethod
    def coerce_preview(
        *,
        preview: Any,
        fallback_band_id: str,
    ) -> ProvisionalBandPreview | None:
        if preview is None:
            return None
        if isinstance(preview, ProvisionalBandPreview):
            return preview
        if isinstance(preview, dict):
            return ProvisionalBandPreview(
                band_id=str(preview.get("band_id") or fallback_band_id),
                artifact_path=str(preview.get("artifact_path") or ""),
                aggregate_verdict=str(preview.get("aggregate_verdict") or "warn"),
                preview_chapter_count=int(preview.get("preview_chapter_count") or 0),
                total_char_count=int(preview.get("total_char_count") or 0),
                issue_count=int(preview.get("issue_count") or 0),
                failure_count=int(preview.get("failure_count") or 0),
                chapter_numbers=[int(item) for item in (preview.get("chapter_numbers") or [])],
                summary_lines=[str(item) for item in (preview.get("summary_lines") or [])],
            )
        if all(hasattr(preview, name) for name in ("band_id", "artifact_path", "aggregate_verdict")):
            return ProvisionalBandPreview(
                band_id=str(getattr(preview, "band_id", "") or fallback_band_id),
                artifact_path=str(getattr(preview, "artifact_path", "") or ""),
                aggregate_verdict=str(getattr(preview, "aggregate_verdict", "") or "warn"),
                preview_chapter_count=int(getattr(preview, "preview_chapter_count", 0) or 0),
                total_char_count=int(getattr(preview, "total_char_count", 0) or 0),
                issue_count=int(getattr(preview, "issue_count", 0) or 0),
                failure_count=int(getattr(preview, "failure_count", 0) or 0),
                chapter_numbers=[int(item) for item in (getattr(preview, "chapter_numbers", []) or [])],
                summary_lines=[str(item) for item in (getattr(preview, "summary_lines", []) or [])],
            )
        return None
