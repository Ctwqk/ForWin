"""Durable semantic review evidence; unknown history never acquires fresh proof."""
from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from forwin.models.draft import ChapterReview
from forwin.retrieval.source_identity import CanonBaselineChanged, CanonReadBaseline


class ReviewedCanonBaseline(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    project_id: str = Field(min_length=1)
    book_revision: int = Field(ge=0)
    as_of_chapter: int = Field(ge=0)

    def read_baseline(self) -> CanonReadBaseline:
        return CanonReadBaseline(self.project_id, self.book_revision, self.as_of_chapter)


class CanonReviewEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    review_id: str
    draft_id: str
    baseline: ReviewedCanonBaseline


def candidate_review_evidence(session, candidate, *, for_update=False) -> CanonReviewEvidence:
    statement = select(ChapterReview).where(ChapterReview.id == candidate.review_id)
    if for_update:
        statement = statement.with_for_update()
    review = session.scalar(statement.execution_options(populate_existing=True))
    if review is None or review.draft_id != candidate.candidate_draft_id:
        raise CanonBaselineChanged("candidate review association changed")
    try:
        metadata = json.loads(review.review_meta_json or "{}")
        snapshot = json.loads(candidate.review_result_json or "{}")
        baseline = ReviewedCanonBaseline.model_validate(metadata["canon_read_baseline"])
        if snapshot.get("canon_read_baseline") != baseline.model_dump():
            raise CanonBaselineChanged("candidate reviewed baseline changed")
    except (ValueError, TypeError, KeyError, AttributeError, ValidationError) as exc:
        raise CanonBaselineChanged("review Canon baseline is unknown or invalid") from exc
    baseline.read_baseline().assert_current(
        session, project_id=candidate.project_id,
        as_of_chapter=max(0, candidate.chapter_number - 1),
    )
    return CanonReviewEvidence(review_id=review.id, draft_id=review.draft_id, baseline=baseline)
