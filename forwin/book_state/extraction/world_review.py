"""Translate a chapter extraction and run world gate v6 on the delta.

Draft review and canon preparation share this sequence, so a draft that passes world
review meets the same gate at canon.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy.orm import Session

from forwin.book_state.reviewer import BookStateReviewGate, BookStateReviewVerdict
from forwin.naming.types import EntityAdmissionPlan
from forwin.protocol.book_state import ApprovedGraphDeltaSet

from .chapter_delta_schema import ChapterDeltaExtraction
from .translator import ChapterDeltaTranslator, TranslationResult


@dataclass(frozen=True)
class ChapterWorldReview:
    translation: TranslationResult
    # None exactly when translation failed: there is no delta to gate.
    gate: BookStateReviewVerdict | None

    @property
    def names(self) -> Mapping[str, str]:
        """Display names of the node, site and fact refs in translation and gate messages."""
        return self.translation.names


def review_chapter_extraction(
    session: Session,
    *,
    project_id: str,
    chapter_number: int,
    extraction: ChapterDeltaExtraction,
    admission_plan: EntityAdmissionPlan,
    chapter_summary: str,
    travel_tolerance: float,
) -> ChapterWorldReview:
    review_verdict_id = f"book_state_direct_review_{project_id}_{chapter_number}"
    translation = ChapterDeltaTranslator(session).translate(
        project_id=project_id,
        chapter_number=chapter_number,
        extraction=extraction,
        admission_plan=admission_plan,
        chapter_summary=chapter_summary,
        review_verdict_id=review_verdict_id,
    )
    if translation.delta is None:
        return ChapterWorldReview(translation=translation, gate=None)
    # Translation warnings never block; the committed delta keeps them for audit.
    delta = translation.delta.model_copy(update={"metadata": {
        **translation.delta.metadata,
        "translation_warnings": [
            {"code": issue.code, "message": issue.message} for issue in translation.issues
        ],
    }})
    changes = ApprovedGraphDeltaSet(
        project_id=project_id,
        chapter_number=chapter_number,
        graph_deltas=[delta],
        approved_by=["chapter_extraction"],
        review_verdict_id=review_verdict_id,
    )
    # Chapter canon is reviewed against the canon before the chapter, even
    # when a historical rewrite still holds the chapter's old deltas.
    gate = BookStateReviewGate(session).review(
        changes,
        base_chapter=max(chapter_number - 1, 0),
        travel_tolerance=travel_tolerance,
    )
    return ChapterWorldReview(translation=translation, gate=gate)


__all__ = ["ChapterWorldReview", "review_chapter_extraction"]
